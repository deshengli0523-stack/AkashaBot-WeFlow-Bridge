import ast
import asyncio
import io
import importlib.util
import json
import os
import pathlib
import re
import runpy
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from unittest import mock

sys.dont_write_bytecode = True


ROOT = pathlib.Path(__file__).resolve().parents[2]
BRIDGE = ROOT / "bridge"
if str(BRIDGE) not in sys.path:
    sys.path.insert(0, str(BRIDGE))
LOG_SOURCE_FILES = (
    "bridge_core.py",
    "ob_protocol.py",
    "uia_fixed_sender.py",
)
TASK4_RUNTIME_SOURCE_FILES = (
    "main.py",
    "config.py",
    "uia_fixed_sender.py",
    "ob_protocol.py",
)
TASK4_REMOVED_SOURCE_FILES = ("senders.py", "uia_sender.py")
TASK4_LEGACY_MARKERS = (
    "senders",
    "uia_sender",
    "create_sender",
    "WeFlowApiSender",
    "UiaSender",
    "SEND_METHOD",
    "WE_FLOW_SEND_API",
    "use_enter_to_send",
)

SENSITIVE_EXACT_NAMES = {
    "base_name",
    "body",
    "buffer_key",
    "caption",
    "caption_text",
    "combined",
    "contact",
    "content",
    "data",
    "data_str",
    "e",
    "err",
    "error",
    "event",
    "exc",
    "exception",
    "ex",
    "failure",
    "file_path",
    "file_val",
    "filename",
    "filepath",
    "group_name",
    "group_raw",
    "image_path",
    "img_path",
    "message",
    "msg",
    "msgs",
    "nickname",
    "params",
    "path",
    "payload",
    "raw_message",
    "record",
    "recipient",
    "save_path",
    "seg_data",
    "sender_id",
    "sender_name",
    "session_id_data",
    "source_name",
    "talker",
    "talker_id",
    "talker_name",
    "text",
}
SENSITIVE_TERMINAL_TOKENS = {
    "body",
    "caption",
    "contact",
    "content",
    "error",
    "exception",
    "failure",
    "file",
    "filename",
    "filepath",
    "message",
    "msg",
    "name",
    "nickname",
    "path",
    "recipient",
    "sender",
    "source",
    "talker",
    "text",
}
SAFE_METADATA_SUFFIXES = {
    "attempt",
    "code",
    "connected",
    "count",
    "height",
    "kind",
    "length",
    "mode",
    "ready",
    "seconds",
    "size",
    "status",
    "type",
    "version",
    "width",
}
SAFE_METADATA_TERMINALS = {"control_type_name"}
SAFE_LOG_WRAPPERS = {
    "bool",
    "chat_record",
    "len",
    "type",
}
LOG_METHODS = {"critical", "debug", "error", "exception", "info", "warning"}
LOG_RECEIVERS = {"log", "logger", "logging"}


def _call_name(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _normalized_identifier(value):
    if not isinstance(value, str):
        return ""
    with_word_boundaries = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", value)
    return re.sub(r"[^a-zA-Z0-9]+", "_", with_word_boundaries).strip("_").lower()


def _is_sensitive_terminal(value):
    if isinstance(value, str) and value.startswith("__") and value.endswith("__"):
        return False
    normalized = _normalized_identifier(value)
    if not normalized or normalized in SAFE_METADATA_TERMINALS:
        return False
    if normalized in SENSITIVE_EXACT_NAMES:
        return True
    tokens = normalized.split("_")
    if tokens[-1] in SAFE_METADATA_SUFFIXES:
        return False
    return bool(set(tokens) & SENSITIVE_TERMINAL_TOKENS)


def _is_logger_receiver(node):
    terminal = _call_name(node)
    normalized = _normalized_identifier(terminal)
    if normalized in LOG_RECEIVERS:
        return True
    if normalized.endswith("_log") or normalized.endswith("_logger"):
        return True
    if isinstance(node, ast.Call):
        return _is_logger_receiver(node.func)
    if isinstance(node, ast.Subscript):
        return _is_logger_receiver(node.value)
    return False


def _unsafe_log_values(node):
    if isinstance(node, ast.Call):
        if _call_name(node.func) in SAFE_LOG_WRAPPERS:
            return []
        if (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and _is_sensitive_terminal(node.args[0].value)
        ):
            return [f"get({node.args[0].value!r})"]
    if (
        isinstance(node, ast.Subscript)
        and isinstance(node.slice, ast.Constant)
        and _is_sensitive_terminal(node.slice.value)
    ):
        return [f"subscript[{node.slice.value!r}]"]
    if isinstance(node, ast.Attribute) and _is_sensitive_terminal(node.attr):
        return [f"attribute.{node.attr}"]
    if isinstance(node, ast.Name) and _is_sensitive_terminal(node.id):
        return [node.id]

    unsafe = []
    for child in ast.iter_child_nodes(node):
        unsafe.extend(_unsafe_log_values(child))
    return unsafe


def _unsafe_logging_calls(source, filename="<source>"):
    tree = ast.parse(source, filename=filename)
    findings = []
    for call in ast.walk(tree):
        if not (
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and call.func.attr in LOG_METHODS
            and _is_logger_receiver(call.func.value)
        ):
            continue
        unsafe = []
        for argument in call.args:
            unsafe.extend(_unsafe_log_values(argument))
        for keyword in call.keywords:
            unsafe.extend(_unsafe_log_values(keyword.value))
        if unsafe:
            findings.append((unsafe, ast.unparse(call)))
    return findings


class BridgeRuntimeTests(unittest.TestCase):
    @classmethod
    def tearDownClass(cls):
        cache_dir = pathlib.Path(__file__).parent / "__pycache__"
        if cache_dir.is_dir():
            shutil.rmtree(cache_dir)

    @staticmethod
    def _load_ob_protocol(module_name, state_module, config_module, requests_module):
        spec = importlib.util.spec_from_file_location(
            module_name,
            BRIDGE / "ob_protocol.py",
        )
        protocol = importlib.util.module_from_spec(spec)
        with mock.patch.dict(
            sys.modules,
            {
                "state": state_module,
                "config": config_module,
                "requests": requests_module,
            },
        ):
            spec.loader.exec_module(protocol)
        return protocol

    @staticmethod
    def _load_bridge_core(
        module_name,
        *,
        state_module,
        config_module,
        requests_module,
    ):
        ob_protocol_module = types.ModuleType("ob_protocol")
        ob_protocol_module.push_event = lambda _event: 1
        ob_protocol_module.make_message_event = (
            lambda message_type, user_id, message, **kwargs: {
                "message_type": message_type,
                "user_id": user_id,
                "message": message,
                **kwargs,
            }
        )
        spec = importlib.util.spec_from_file_location(
            module_name,
            BRIDGE / "bridge_core.py",
        )
        module = importlib.util.module_from_spec(spec)
        with mock.patch.dict(
            sys.modules,
            {
                "state": state_module,
                "config": config_module,
                "ob_protocol": ob_protocol_module,
                "requests": requests_module,
            },
        ):
            spec.loader.exec_module(module)
        return module

    def test_inbound_video_uses_media_processing_thread(self):
        state_module = types.ModuleType("state")
        state_module.group_reply_mode = "all"
        config_module = types.ModuleType("config")
        config_module.BOT_NICKNAMES = []
        config_module.BOT_WXID = ""
        config_module.BUFFER_SECONDS = 5
        requests_module = types.ModuleType("requests")
        bridge_core = self._load_bridge_core(
            "bridge_video_dispatch_test",
            state_module=state_module,
            config_module=config_module,
            requests_module=requests_module,
        )
        created = []

        class FakeThread:
            def __init__(self, *, target, args, daemon):
                created.append((target, args, daemon))

            def start(self):
                return None

        bridge = bridge_core.WeFlowBridge(sender=None)
        with mock.patch.object(bridge_core.threading, "Thread", FakeThread):
            bridge.add_to_buffer(
                {
                    "content": "[视频]",
                    "sourceName": "private-contact",
                    "sessionId": "wxid-contact",
                    "sessionType": "private",
                    "rawid": "video-server-id",
                    "timestamp": 100,
                }
            )
        self.assertEqual(1, len(created))
        self.assertEqual("process_video_message", created[0][0].__name__)
        self.assertEqual("video-server-id", created[0][1][0]["rawid"])
        self.assertTrue(created[0][2])
        self.assertEqual({}, bridge.pending_buffers)

    def test_video_fetch_matches_server_id_and_caption_uses_video_url(self):
        with tempfile.TemporaryDirectory(prefix="akasha-video-") as temp:
            state_module = types.ModuleType("state")
            state_module.group_reply_mode = "all"
            config_module = types.ModuleType("config")
            local_credential = "local-" + "weflow-" + "credential"
            visual_credential = "visual-" + "api-" + "credential"
            config_module.WE_FLOW_BASE_URL = "http://127.0.0.1:5031"
            config_module.ACCESS_TOKEN = local_credential
            config_module.ASTRBOT_ATTACHMENTS = temp
            config_module.VIDEO_CAPTION_MAX_MIB = 6
            config_module.IMAGE_CAPTION_PROVIDER = "openai"
            config_module.IMAGE_CAPTION_API_KEY = visual_credential
            config_module.IMAGE_CAPTION_API_BASE = (
                "https://dashscope.aliyuncs.com/compatible-mode/v1"
            )
            config_module.IMAGE_CAPTION_MODEL = "qwen3.7-plus"
            config_module.VIDEO_CAPTION_PROMPT = "describe this video"

            get_calls = []
            post_calls = []

            class FakeApiResponse:
                status_code = 200

                @staticmethod
                def json():
                    return {
                        "messages": [
                            {
                                "serverId": "wrong-id",
                                "mediaType": "video",
                                "mediaUrl": "/api/v1/media/wrong.mp4",
                            },
                            {
                                "serverId": "target-id",
                                "mediaType": "video",
                                "mediaUrl": "/api/v1/media/target.mp4",
                            },
                        ]
                    }

            class FakeDownloadResponse:
                status_code = 200
                headers = {
                    "Content-Type": "video/mp4",
                    "Content-Length": "12",
                }

                @staticmethod
                def iter_content(chunk_size):
                    self.assertEqual(1024 * 1024, chunk_size)
                    return iter((b"video-bytes",))

                @staticmethod
                def close():
                    return None

            class FakeCaptionResponse:
                status_code = 200

                @staticmethod
                def json():
                    return {
                        "choices": [
                            {"message": {"content": "一个人在海边挥手"}}
                        ]
                    }

            def fake_get(url, **kwargs):
                get_calls.append((url, kwargs))
                if url.endswith("/api/v1/messages"):
                    return FakeApiResponse()
                self.assertEqual(
                    "http://127.0.0.1:5031/api/v1/media/target.mp4",
                    url,
                )
                return FakeDownloadResponse()

            def fake_post(url, **kwargs):
                post_calls.append((url, kwargs))
                return FakeCaptionResponse()

            requests_module = types.ModuleType("requests")
            requests_module.get = fake_get
            requests_module.post = fake_post
            requests_module.Timeout = TimeoutError
            bridge_core = self._load_bridge_core(
                "bridge_video_caption_test",
                state_module=state_module,
                config_module=config_module,
                requests_module=requests_module,
            )

            video_path = bridge_core.WeFlowBridge(
                sender=None
            )._fetch_wechat_video(
                {
                    "sessionId": "wxid-contact",
                    "rawid": "target-id",
                    "timestamp": 100,
                }
            )
            self.assertIsNotNone(video_path)
            self.assertEqual(b"video-bytes", pathlib.Path(video_path).read_bytes())
            self.assertEqual(
                "Bearer " + local_credential,
                get_calls[1][1]["headers"]["Authorization"],
            )
            self.assertNotIn(local_credential, get_calls[1][0])

            caption = bridge_core.caption_video_via_openai(video_path)
            self.assertEqual("一个人在海边挥手", caption)
            payload = post_calls[0][1]["json"]
            self.assertEqual("qwen3.7-plus", payload["model"])
            video_item = payload["messages"][0]["content"][1]
            self.assertEqual("video_url", video_item["type"])
            self.assertEqual(2, video_item["fps"])
            self.assertTrue(
                video_item["video_url"]["url"].startswith(
                    "data:video/mp4;base64,"
                )
            )
            self.assertNotIn(
                local_credential,
                json.dumps(payload, ensure_ascii=False),
            )

    @staticmethod
    def _configure_verified_private_route(
        state_module,
        config_module,
        requests_module,
        *,
        target_id=7,
        routing_name="private-contact",
        session="private-session",
    ):
        expected_session = session
        route = {
            "ob_id": target_id,
            "identity_hmac": b"r" * 32,
            "routing_name": routing_name,
        }
        state_module.get_private_route = (
            lambda ob_id: route if int(ob_id) == target_id else None
        )
        state_module.private_route_matches = (
            lambda supplied_route, *, account, session: (
                supplied_route is route
                and account == "test-bot-account"
                and session == expected_session
            )
        )
        config_module.WE_FLOW_BASE_URL = "http://127.0.0.1:5031"
        config_module.ACCESS_TOKEN = "test-" + "contacts-" + "token"
        config_module.BOT_WXID = "test-bot-account"

        class FakeResponse:
            status_code = 200

            @staticmethod
            def json():
                return {
                    "success": True,
                    "count": 1,
                    "contacts": [
                        {
                            "username": session,
                            "displayName": routing_name,
                            "type": "friend",
                        }
                    ],
                }

        requests_module.get = lambda *_args, **_kwargs: FakeResponse()

    def test_config_and_log_paths_follow_environment(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            config_path = root / "data" / "bridge" / "config.json"
            log_dir = root / "data" / "logs"
            config_path.parent.mkdir(parents=True)
            log_dir.mkdir(parents=True)
            template = json.loads(
                (BRIDGE / "config.example.json").read_text(encoding="utf-8")
            )
            template["access_token"] = "test-token"
            template["web_port"] = "8766"
            template["video_caption_max_mib"] = 50
            config_path.write_text(json.dumps(template), encoding="utf-8")
            environment = os.environ.copy()
            environment["AKASHABOT_CONFIG_PATH"] = str(config_path)
            environment["AKASHABOT_LOG_DIR"] = str(log_dir)
            environment["PYTHONDONTWRITEBYTECODE"] = "1"
            result = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "import config; print(config.CONFIG_FILE); "
                    "print(config.BRIDGE_LOG_FILE); "
                    "print(type(config.WEB_PORT).__name__); "
                    "print(config.WEB_PORT); "
                    "print(config.VIDEO_CAPTION_MAX_MIB)",
                ],
                cwd=BRIDGE,
                env=environment,
                text=True,
                capture_output=True,
                check=True,
            )
            lines = result.stdout.strip().splitlines()
            self.assertEqual(pathlib.Path(lines[-5]), config_path)
            self.assertEqual(pathlib.Path(lines[-4]), log_dir / "bridge.log")
            self.assertEqual(lines[-3:], ["int", "8766", "6"])
            self.assertTrue((log_dir / "bridge.log").is_file())

    def test_state_pid_path_is_derived_from_environment_directory(self):
        source = (BRIDGE / "main.py").read_text(encoding="utf-8")
        tree = ast.parse(source, filename="main.py")
        entrypoint = next(
            node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "_run_main"
        )
        path_assignments = []
        assigned_names = []
        for statement in entrypoint.body:
            if not (
                isinstance(statement, ast.Assign)
                and len(statement.targets) == 1
                and isinstance(statement.targets[0], ast.Name)
                and statement.targets[0].id in {"STATE_DIR", "PID_FILE"}
            ):
                continue
            path_assignments.append(statement)
            assigned_names.append(statement.targets[0].id)
        self.assertEqual(assigned_names, ["STATE_DIR", "PID_FILE"])

        with tempfile.TemporaryDirectory() as temporary:
            state_dir = pathlib.Path(temporary) / "data" / "state"
            namespace = {"os": os, "__file__": str(BRIDGE / "main.py")}
            environment = {"AKASHABOT_STATE_DIR": str(state_dir)}
            with mock.patch.dict(os.environ, environment, clear=False):
                exec(
                    compile(
                        ast.Module(body=path_assignments, type_ignores=[]),
                        filename="main.py",
                        mode="exec",
                    ),
                    namespace,
                )
            self.assertEqual(pathlib.Path(namespace["STATE_DIR"]), state_dir)
            self.assertEqual(
                pathlib.Path(namespace["PID_FILE"]), state_dir / "bridge.pid"
            )

    def test_bridge_pid_record_rejects_pid_reuse_and_verifies_legacy_owner(self):
        state_module = types.ModuleType("state")
        config_module = types.ModuleType("config")
        config_module.WEB_PORT = 8766
        uia_module = types.ModuleType("uia_fixed_sender")
        uia_module.UiaFixedSender = object
        ob_client_module = types.ModuleType("ob_client")
        ob_client_module._run_ob_client = lambda *_args: None
        bridge_core_module = types.ModuleType("bridge_core")
        bridge_core_module.WeFlowBridge = object
        web_panel_module = types.ModuleType("web_panel")
        web_panel_module.WebHandler = object
        web_panel_module.PAGE = ""
        requests_module = types.ModuleType("requests")

        class FakeStatus:
            status_code = 200

            @staticmethod
            def json():
                return {
                    "running": True,
                    "ob_connected": False,
                    "weflow_connected": False,
                }

        requests_module.get = mock.Mock(return_value=FakeStatus())
        spec = importlib.util.spec_from_file_location(
            "bridge_pid_identity_test",
            BRIDGE / "main.py",
        )
        main_module = importlib.util.module_from_spec(spec)
        with mock.patch.dict(
            sys.modules,
            {
                "state": state_module,
                "config": config_module,
                "uia_fixed_sender": uia_module,
                "ob_client": ob_client_module,
                "bridge_core": bridge_core_module,
                "web_panel": web_panel_module,
                "requests": requests_module,
            },
        ):
            spec.loader.exec_module(main_module)

        with mock.patch.object(
            main_module,
            "_process_start_token",
            return_value=222,
        ):
            self.assertTrue(main_module._pid_record_is_live("123:222"))
            self.assertFalse(main_module._pid_record_is_live("123:111"))
        requests_module.get.reset_mock()
        with mock.patch.object(
            main_module,
            "_process_start_token",
            return_value=None,
        ):
            self.assertFalse(main_module._pid_record_is_live("123:222"))
            self.assertEqual(
                main_module._pid_record_status("123:222"),
                "unverifiable",
            )
        requests_module.get.assert_not_called()
        self.assertTrue(main_module._pid_record_is_live("123"))
        requests_module.get.assert_called_once()
        self.assertFalse(main_module._pid_record_is_live("not-a-pid"))
        self.assertFalse(main_module._pid_record_is_live("0"))
        self.assertFalse(main_module._pid_record_is_live("-1"))
        self.assertFalse(main_module._pid_record_is_live("123:0"))
        self.assertEqual(
            main_module._pid_record_status("not-a-pid"),
            "unverifiable",
        )

    def test_bridge_pid_claim_is_exclusive_and_release_requires_ownership(self):
        state_module = types.ModuleType("state")
        config_module = types.ModuleType("config")
        config_module.WEB_PORT = 8766
        uia_module = types.ModuleType("uia_fixed_sender")
        uia_module.UiaFixedSender = object
        ob_client_module = types.ModuleType("ob_client")
        ob_client_module._run_ob_client = lambda *_args: None
        bridge_core_module = types.ModuleType("bridge_core")
        bridge_core_module.WeFlowBridge = object
        web_panel_module = types.ModuleType("web_panel")
        web_panel_module.WebHandler = object
        web_panel_module.PAGE = ""
        requests_module = types.ModuleType("requests")
        requests_module.get = mock.Mock()

        spec = importlib.util.spec_from_file_location(
            "bridge_pid_ownership_test",
            BRIDGE / "main.py",
        )
        main_module = importlib.util.module_from_spec(spec)
        with mock.patch.dict(
            sys.modules,
            {
                "state": state_module,
                "config": config_module,
                "uia_fixed_sender": uia_module,
                "ob_client": ob_client_module,
                "bridge_core": bridge_core_module,
                "web_panel": web_panel_module,
                "requests": requests_module,
            },
        ):
            spec.loader.exec_module(main_module)

        with tempfile.TemporaryDirectory() as temporary:
            pid_path = pathlib.Path(temporary) / "bridge.pid"
            main_module._claim_pid_file(str(pid_path), "123:456")
            self.assertEqual(pid_path.read_text(encoding="utf-8"), "123:456")
            with self.assertRaises(FileExistsError):
                main_module._claim_pid_file(str(pid_path), "222:333")

            pid_path.write_text("222:333", encoding="utf-8")
            main_module._release_pid_file_if_owned(str(pid_path), "123:456")
            self.assertTrue(pid_path.is_file())
            main_module._release_pid_file_if_owned(str(pid_path), "222:333")
            self.assertFalse(pid_path.exists())

    def test_web_panel_reads_complete_structured_chat_records(self):
        private_body = "第一行\n第二行 <script>不能执行</script> " + ("长正文" * 2048)
        inbound = {
            "event": "inbound",
            "scope": "private",
            "contact": "完整联系人名称",
            "status": "received",
            "body": private_body,
        }
        outbound = {
            "event": "outbound",
            "scope": "group",
            "contact": "完整群聊名称",
            "sender": "完整群成员名称",
            "status": "sent",
            "body": "Bot 发出的完整正文",
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            config_path = root / "config.json"
            config_path.write_text("{}", encoding="utf-8")
            log_path = root / "bridge.log"
            log_path.write_text(
                "\n".join(
                    [
                        "12:00:00 [INFO] 普通运行日志",
                        "12:00:01 [INFO] CHAT "
                        + json.dumps(inbound, ensure_ascii=False, separators=(",", ":")),
                        "12:00:02 [ERROR] CHAT {malformed-json",
                        "12:00:03 [INFO] CHAT "
                        + json.dumps(outbound, ensure_ascii=False, separators=(",", ":")),
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            panel, _, _ = self._load_web_panel(
                {"schema_version": 1, "completed": False},
                config_path,
                log_path,
            )

            self.assertTrue(
                hasattr(panel, "_read_chat_records"),
                "Web panel has no structured chat history reader.",
            )
            records = panel._read_chat_records(log_path, limit=100)

        self.assertEqual(
            records,
            [
                {"time": "12:00:01", **inbound},
                {"time": "12:00:03", **outbound},
            ],
        )
        self.assertEqual(records[0]["contact"], "完整联系人名称")
        self.assertEqual(records[0]["body"], private_body)
        self.assertEqual(records[1]["contact"], "完整群聊名称")
        self.assertEqual(records[1]["sender"], "完整群成员名称")
        self.assertEqual(records[1]["body"], "Bot 发出的完整正文")

    def test_web_panel_chat_history_api_is_bounded_and_local_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            config_path = root / "config.json"
            config_path.write_text("{}", encoding="utf-8")
            log_path = root / "bridge.log"
            lines = []
            for index in range(205):
                record = {
                    "event": "inbound" if index % 2 == 0 else "outbound",
                    "scope": "private",
                    "contact": f"联系人-{index}",
                    "status": "received" if index % 2 == 0 else "sent",
                    "body": f"完整正文-{index}",
                }
                lines.append(
                    f"12:00:{index % 60:02d} [INFO] CHAT "
                    + json.dumps(record, ensure_ascii=False, separators=(",", ":"))
                )
            log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            panel, _, _ = self._load_web_panel(
                {"schema_version": 1, "completed": False},
                config_path,
                log_path,
            )
            source = (BRIDGE / "web_panel.py").read_text(encoding="utf-8")
            self.assertIn(
                '"/api/chat-history"',
                source,
                "Web panel has no chat history API.",
            )

            response = self._invoke_web_handler(
                panel, "do_GET", "/api/chat-history?limit=2"
            )
            too_large = self._invoke_web_handler(
                panel, "do_GET", "/api/chat-history?limit=201"
            )
            duplicate = self._invoke_web_handler(
                panel, "do_GET", "/api/chat-history?limit=2&limit=3"
            )

        self.assertEqual(response["code"], 200)
        self.assertEqual(
            [record["contact"] for record in response["data"]["records"]],
            ["联系人-203", "联系人-204"],
        )
        self.assertEqual(
            [record["body"] for record in response["data"]["records"]],
            ["完整正文-203", "完整正文-204"],
        )
        self.assertEqual(
            too_large,
            {
                "data": {"error": "E_CHAT_HISTORY_REQUEST"},
                "code": 400,
            },
        )
        self.assertEqual(
            duplicate,
            {
                "data": {"error": "E_CHAT_HISTORY_REQUEST"},
                "code": 400,
            },
        )
        self.assertNotIn("Access-Control-Allow-Origin", source)
        main_source = (BRIDGE / "main.py").read_text(encoding="utf-8")
        self.assertIn(
            '("127.0.0.1", int(config.WEB_PORT))',
            main_source,
        )
        self.assertIn("allow_reuse_address = True", main_source)

    def test_web_panel_chat_history_rejects_nonlocal_requests_and_fixed_read_errors(
        self,
    ):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            config_path = root / "config.json"
            config_path.write_text("{}", encoding="utf-8")
            missing_log = root / "missing" / "bridge.log"
            panel, _, _ = self._load_web_panel(
                {"schema_version": 1, "completed": False},
                config_path,
                missing_log,
            )

            bad_host = self._invoke_web_handler(
                panel,
                "do_GET",
                "/api/chat-history",
                request_headers={"Host": "attacker.example"},
            )
            bad_origin = self._invoke_web_handler(
                panel,
                "do_POST",
                "/pause",
                request_headers={
                    "Origin": "https://attacker.example",
                    "Sec-Fetch-Site": "cross-site",
                },
            )
            missing = self._invoke_web_handler(
                panel, "do_GET", "/api/chat-history"
            )

        self.assertEqual(
            bad_host, {"data": {"error": "E_LOCAL_ONLY"}, "code": 403}
        )
        self.assertEqual(
            bad_origin, {"data": {"error": "E_LOCAL_ONLY"}, "code": 403}
        )
        self.assertEqual(
            missing,
            {"data": {"error": "E_CHAT_HISTORY_READ"}, "code": 500},
        )
        self.assertNotIn(str(missing_log), json.dumps(missing))

    def test_web_panel_chat_history_scan_is_byte_bounded(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            config_path = root / "config.json"
            config_path.write_text("{}", encoding="utf-8")
            log_path = root / "bridge.log"
            log_path.write_text("", encoding="utf-8")
            panel, _, _ = self._load_web_panel(
                {"schema_version": 1, "completed": False},
                config_path,
                log_path,
            )

            class CountingStream(io.BytesIO):
                def __init__(self, value):
                    super().__init__(value)
                    self.bytes_read = 0

                def read(self, size=-1):
                    value = super().read(size)
                    self.bytes_read += len(value)
                    return value

            old_record = (
                b'00:00:00 [INFO] CHAT {"event":"inbound","scope":"private",'
                b'"contact":"old","status":"received","body":"old"}\n'
            )
            stream = CountingStream(
                old_record
                + (b"00:00:01 [INFO] ordinary runtime line\n" * 150000)
            )
            with mock.patch("builtins.open", return_value=stream):
                records = panel._read_chat_records("ignored", limit=100)

        self.assertEqual(records, [])
        self.assertLessEqual(
            stream.bytes_read,
            panel._CHAT_LOG_MAX_SCAN_BYTES,
        )

    def test_web_panel_chat_dom_uses_text_content_and_escapes_config_values(self):
        source = (BRIDGE / "web_panel.py").read_text(encoding="utf-8")
        self.assertIn("meta.textContent =", source)
        self.assertIn("body.textContent =", source)
        self.assertNotIn("body.innerHTML", source)
        self.assertNotIn("meta.innerHTML", source)
        self.assertIn("var safeVal = escapeHtml(val);", source)
        self.assertIn("setInterval(refreshChatHistory, 2000)", source)

    def _load_web_panel(self, calibration, config_path, log_path):
        state_module = types.ModuleType("state")
        state_module._ob_ws = None
        state_module._ob_ws_ready = types.SimpleNamespace(is_set=lambda: False)
        state_module.bridge_instance = None
        state_module.running = False
        state_module.paused = threading.Event()
        state_module.group_reply_mode = "mention"
        state_module.sender_instance = None
        state_module.send_preview = None

        def get_send_preview():
            if state_module.send_preview is None:
                return None
            return dict(state_module.send_preview)

        def cancel_current_preview(preview_id):
            preview = state_module.send_preview
            if (
                preview is None
                or preview.get("preview_id") != preview_id
                or preview.get("stage") == "submitting"
            ):
                return False
            state_module.send_preview = None
            return True

        state_module.get_send_preview = get_send_preview
        state_module.cancel_current_preview = cancel_current_preview

        config_module = types.ModuleType("config")
        config_module.CONFIG_FILE = str(config_path)
        config_module.BRIDGE_LOG_FILE = str(log_path)
        config_module.UIA_FIXED_CALIBRATION = calibration
        config_module.UIA_FIXED_PRE_PASTE_PREVIEW_DELAY = 1.0
        config_module.UIA_FIXED_PRE_SEND_DELAY = 5.0
        config_module.WEB_PORT = 8766

        spec = importlib.util.spec_from_file_location(
            "task5_web_panel_under_test",
            BRIDGE / "web_panel.py",
        )
        panel = importlib.util.module_from_spec(spec)
        with mock.patch.dict(
            sys.modules,
            {"state": state_module, "config": config_module},
        ):
            sys.path.insert(0, str(BRIDGE))
            try:
                spec.loader.exec_module(panel)
            finally:
                sys.path.remove(str(BRIDGE))
        return panel, state_module, config_module

    def _invoke_web_handler(
        self, panel, method, path, body=None, request_headers=None
    ):
        handler = object.__new__(panel.WebHandler)
        handler.path = path
        captured = {}
        handler.headers = {"Host": "127.0.0.1:8766"}
        if request_headers:
            handler.headers.update(request_headers)

        def capture(_handler, data, code=200):
            captured["data"] = data
            captured["code"] = code

        handler.send_json = types.MethodType(capture, handler)
        if body is not None:
            payload = json.dumps(body).encode("utf-8")
            handler.headers["Content-Length"] = str(len(payload))
            handler.rfile = io.BytesIO(payload)
        getattr(handler, method)()
        return captured

    def _load_main_runtime(self, request_get):
        state_module = types.ModuleType("state")
        state_module.running = False
        state_module.lifecycle_generation = 0
        state_module.run_lock = threading.Lock()
        state_module.paused = threading.Event()
        state_module.sender_instance = None
        state_module.bridge_thread = None
        state_module.bridge_instance = None
        state_module.bridge_lock = threading.Lock()
        state_module._ob_ws = None
        state_module._ob_ws_loop = None
        state_module._ob_ws_ready = threading.Event()

        def is_generation_running(generation):
            return bool(
                state_module.running
                and state_module.lifecycle_generation == generation
            )

        def deactivate_generation(generation):
            with state_module.run_lock:
                if not is_generation_running(generation):
                    return False
                state_module.running = False
                state_module.lifecycle_generation += 1
                return True

        state_module.is_generation_running = is_generation_running
        state_module.deactivate_generation = deactivate_generation

        config_module = types.ModuleType("config")
        config_module.UIA_FIXED_CALIBRATION = {}
        config_module.UIA_FIXED_PRE_PASTE_PREVIEW_DELAY = 1.0
        config_module.UIA_FIXED_PRE_SEND_DELAY = 5.0
        config_module.ACCESS_TOKEN = "fixture"
        config_module.WE_FLOW_BASE_URL = "http://127.0.0.1:5031"
        config_module.WEB_PORT = 8766

        class FakeSender:
            def __init__(self, *_args, **_kwargs):
                self.stopped = False

            def stop_pending(self):
                self.stopped = True

        sender_module = types.ModuleType("uia_fixed_sender")
        sender_module.UiaFixedSender = FakeSender

        ob_module = types.ModuleType("ob_client")
        ob_module._run_ob_client = lambda _generation: None

        class FakeBridge:
            instances = []

            def __init__(self, _sender, generation):
                self.generation = generation
                self._sse_session = None
                self.listen_calls = 0
                self.__class__.instances.append(self)

            def listen_sse(self):
                self.listen_calls += 1
                state_module.deactivate_generation(self.generation)

        bridge_module = types.ModuleType("bridge_core")
        bridge_module.WeFlowBridge = FakeBridge

        panel_module = types.ModuleType("web_panel")
        panel_module.WebHandler = object
        panel_module.PAGE = ""

        request_exception = type("RequestException", (Exception,), {})
        requests_module = types.ModuleType("requests")
        requests_module.get = request_get
        requests_module.exceptions = types.SimpleNamespace(
            RequestException=request_exception
        )

        spec = importlib.util.spec_from_file_location(
            "startup_readiness_main_under_test",
            BRIDGE / "main.py",
        )
        main_module = importlib.util.module_from_spec(spec)
        with mock.patch.dict(
            sys.modules,
            {
                "state": state_module,
                "config": config_module,
                "uia_fixed_sender": sender_module,
                "ob_client": ob_module,
                "bridge_core": bridge_module,
                "web_panel": panel_module,
                "requests": requests_module,
            },
        ):
            spec.loader.exec_module(main_module)
        return (
            main_module,
            state_module,
            config_module,
            requests_module,
            FakeBridge,
        )

    def test_bridge_retries_delayed_weflow_and_keeps_generation_active(self):
        calls = []
        responses = [
            "connection-error",
            types.SimpleNamespace(status_code=503),
            types.SimpleNamespace(status_code=200),
        ]

        def request_get(url, **kwargs):
            calls.append((url, kwargs))
            response = responses.pop(0)
            if response == "connection-error":
                raise request_exception("not ready")
            return response

        main_module, state_module, _, requests_module, fake_bridge = (
            self._load_main_runtime(request_get)
        )
        request_exception = requests_module.exceptions.RequestException
        state_module.running = True
        state_module.lifecycle_generation = 7

        with mock.patch.object(main_module.time, "sleep", return_value=None):
            main_module._bridge_loop(7)

        self.assertEqual(len(calls), 3)
        self.assertEqual(
            calls[-1],
            (
                "http://127.0.0.1:5031/api/v1/sessions",
                {
                    "params": {
                        "limit": 1,
                        "access_token": "fixture",
                    },
                    "timeout": 5,
                },
            ),
        )
        self.assertEqual(fake_bridge.instances[-1].listen_calls, 1)

    def test_bridge_restart_always_creates_a_new_onebot_thread(self):
        main_module, state_module, _, _, _ = self._load_main_runtime(
            lambda *_args, **_kwargs: types.SimpleNamespace(status_code=200)
        )
        created_threads = []

        class FakeThread:
            def __init__(self, *, target, args, daemon, name):
                self.target = target
                self.args = args
                self.daemon = daemon
                self.name = name
                created_threads.append(self)

            def start(self):
                return None

        with mock.patch.object(main_module.threading, "Thread", FakeThread):
            main_module._start_bridge()
            state_module.running = False
            main_module._start_bridge()

        onebot_threads = [
            thread for thread in created_threads if thread.name.startswith("ob11-client-")
        ]
        self.assertEqual(
            [thread.name for thread in onebot_threads],
            ["ob11-client-1", "ob11-client-2"],
        )
        self.assertEqual(
            [thread.args for thread in onebot_threads],
            [(1,), (2,)],
        )
        self.assertFalse(hasattr(state_module, "ob_client_started"))

    def test_bridge_invalid_weflow_token_remains_terminal(self):
        main_module, state_module, _, _, fake_bridge = self._load_main_runtime(
            lambda *_args, **_kwargs: types.SimpleNamespace(status_code=401)
        )
        state_module.running = True
        state_module.lifecycle_generation = 3

        main_module._bridge_loop(3)

        self.assertFalse(state_module.running)
        self.assertEqual(state_module.lifecycle_generation, 4)
        self.assertEqual(fake_bridge.instances[-1].listen_calls, 0)

    def test_bridge_stop_interrupts_weflow_readiness_retry(self):
        request_exception = None
        calls = []

        def request_get(*_args, **_kwargs):
            calls.append(True)
            raise request_exception("not ready")

        main_module, state_module, _, requests_module, fake_bridge = (
            self._load_main_runtime(request_get)
        )
        request_exception = requests_module.exceptions.RequestException
        state_module.running = True
        state_module.lifecycle_generation = 9

        def stop_generation(_seconds):
            state_module.running = False

        with mock.patch.object(
            main_module.time, "sleep", side_effect=stop_generation
        ):
            main_module._bridge_loop(9)

        self.assertEqual(len(calls), 1)
        self.assertEqual(fake_bridge.instances[-1].listen_calls, 0)

    def test_task5_config_template_uses_only_uncompleted_nested_calibration(self):
        template = json.loads(
            (BRIDGE / "config.example.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            template.get("uia_fixed_calibration"),
            {
                "schema_version": 1,
                "completed": False,
                "coordinate_space": "client_area_ratio",
                "points": {
                    "search_box": None,
                    "first_result": None,
                    "message_input": None,
                    "send_button": None,
                },
                "reference": None,
            },
        )
        self.assertEqual(template.get("uia_fixed_pre_paste_preview_delay"), 1.0)
        self.assertEqual(template.get("uia_fixed_pre_send_delay"), 5.0)
        legacy_keys = {
            "send_method",
            "weflow_send_api",
            "uia_fixed_search_x",
            "uia_fixed_search_y",
            "uia_fixed_first_result_x",
            "uia_fixed_first_result_y",
            "uia_fixed_input_x",
            "uia_fixed_input_y",
            "uia_fixed_send_x",
            "uia_fixed_send_y",
            "uia_fixed_search_delay",
            "uia_fixed_switch_delay",
            "uia_fixed_paste_delay",
            "uia_fixed_clear_input",
            "uia_fixed_use_enter_to_send",
        }
        self.assertTrue(legacy_keys.isdisjoint(template))

    def test_task5_sender_status_uses_full_calibration_validation(self):
        valid = {
            "schema_version": 1,
            "completed": True,
            "coordinate_space": "client_area_ratio",
            "points": {
                "search_box": {"x": 0.1, "y": 0.1},
                "first_result": {"x": 0.2, "y": 0.2},
                "message_input": {"x": 0.6, "y": 0.8},
                "send_button": {"x": 0.9, "y": 0.9},
            },
            "reference": {
                "client_width": 1200,
                "client_height": 800,
                "aspect_ratio": 1.5,
                "dpi": 96,
            },
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            config_path = root / "config.json"
            config_path.write_text("{}", encoding="utf-8")
            log_path = root / "bridge.log"
            log_path.write_text("", encoding="utf-8")
            panel, _, config_module = self._load_web_panel(
                valid, config_path, log_path
            )

            self.assertEqual(
                panel._sender_status(),
                {"sender_mode": "uia_fixed", "calibrated": True},
            )

            config_module.UIA_FIXED_CALIBRATION = {
                "schema_version": 1,
                "completed": True,
                "coordinate_space": "client_area_ratio",
                "points": {},
                "reference": None,
            }
            self.assertEqual(
                panel._sender_status(),
                {"sender_mode": "uia_fixed", "calibrated": False},
            )

    def test_task5_status_keeps_operations_without_embedding_chat_history(self):
        invalid = {"schema_version": 1, "completed": True}
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            config_path = root / "config.json"
            config_path.write_text("{}", encoding="utf-8")
            log_path = root / "bridge.log"
            log_path.write_text(
                'CHAT {"contact":"联系人甲","body":"不得出现在面板中的正文"}',
                encoding="utf-8",
            )
            panel, _, _ = self._load_web_panel(invalid, config_path, log_path)

            response = self._invoke_web_handler(panel, "do_GET", "/status")

        self.assertEqual(response["code"], 200)
        status = response["data"]
        self.assertEqual(status["sender_mode"], "uia_fixed")
        self.assertIs(status["calibrated"], False)
        self.assertIn("running", status)
        self.assertIn("paused", status)
        self.assertIn("ob_connected", status)
        self.assertIn("weflow_connected", status)
        self.assertNotIn("log", status)
        self.assertNotIn("chat_history", status)
        self.assertNotIn("联系人甲", json.dumps(status, ensure_ascii=False))
        self.assertNotIn(
            "不得出现在面板中的正文",
            json.dumps(status, ensure_ascii=False),
        )
        self.assertNotIn("send_method", status)
        self.assertNotIn("ob_url", status)
        serialized = json.dumps(status).lower()
        for private_name in (
            "uia_fixed_calibration",
            "points",
            "reference",
            "dpi",
            "client_width",
            "client_height",
            "aspect_ratio",
        ):
            self.assertNotIn(private_name, serialized)

    def test_text_preview_and_exact_cancel_are_exposed_in_control_panel(self):
        invalid = {"schema_version": 1, "completed": True}
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            config_path = root / "config.json"
            config_path.write_text("{}", encoding="utf-8")
            log_path = root / "bridge.log"
            log_path.write_text("", encoding="utf-8")
            panel, state_module, _ = self._load_web_panel(
                invalid, config_path, log_path
            )
            state_module.send_preview = {
                "preview_id": 41,
                "contact": "完整目标联系人",
                "content": "待审核正文",
                "message_type": "text",
                "stage": "before_paste",
                "remaining_seconds": 1.0,
            }

            status = self._invoke_web_handler(panel, "do_GET", "/status")
            stale = self._invoke_web_handler(
                panel,
                "do_POST",
                "/cancel-current",
                {"preview_id": 40},
            )
            cancelled = self._invoke_web_handler(
                panel,
                "do_POST",
                "/cancel-current",
                {"preview_id": 41},
            )

        self.assertEqual(status["data"]["send_preview"]["content"], "待审核正文")
        self.assertEqual(
            status["data"]["send_preview"]["contact"], "完整目标联系人"
        )
        self.assertEqual(
            stale, {"data": {"ok": True, "cancelled": False}, "code": 200}
        )
        self.assertEqual(
            cancelled,
            {"data": {"ok": True, "cancelled": True}, "code": 200},
        )
        source = (BRIDGE / "web_panel.py").read_text(encoding="utf-8")
        self.assertIn("textContent = preview.content", source)
        self.assertIn("preview.contact", source)
        self.assertIn("setInterval(refreshDashboard, 500)", source)

    def test_task5_config_get_and_post_protect_calibration_and_secrets(self):
        calibration = {
            "schema_version": 1,
            "completed": False,
            "coordinate_space": "client_area_ratio",
            "points": {
                "search_box": None,
                "first_result": None,
                "message_input": None,
                "send_button": None,
            },
            "reference": None,
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            config_path = root / "config.json"
            access_key = "access_" + "token"
            image_key = "image_caption_" + "api_key"
            original_access_value = "private-" + "token"
            original_image_value = "private-" + "image-key"
            replacement_access_value = "replacement-" + "token"
            replacement_image_value = "replacement-" + "image-key"
            config_path.write_text(
                json.dumps(
                    {
                        access_key: original_access_value,
                        image_key: original_image_value,
                        "buffer_seconds": 5,
                        "video_caption_max_mib": 50,
                        "uia_fixed_calibration": calibration,
                    }
                ),
                encoding="utf-8",
            )
            log_path = root / "bridge.log"
            log_path.write_text("", encoding="utf-8")
            panel, _, _ = self._load_web_panel(
                calibration, config_path, log_path
            )

            get_response = self._invoke_web_handler(
                panel, "do_GET", "/api/config"
            )
            self.assertNotIn("uia_fixed_calibration", get_response["data"])
            self.assertNotIn(access_key, get_response["data"])
            self.assertNotIn(image_key, get_response["data"])
            self.assertEqual(
                get_response["data"]["uia_fixed_pre_paste_preview_delay"],
                1.0,
            )
            self.assertEqual(
                get_response["data"]["uia_fixed_pre_send_delay"],
                5.0,
            )
            self.assertEqual(
                get_response["data"]["video_caption_max_mib"],
                6,
            )

            post_response = self._invoke_web_handler(
                panel,
                "do_POST",
                "/api/config",
                {
                    "buffer_seconds": 7,
                    access_key: "   ",
                    image_key: {"invalid": "value"},
                    "uia_fixed_calibration": {
                        "completed": True,
                        "points": {"private": "overwrite-attempt"},
                    },
                },
            )
            saved = json.loads(config_path.read_text(encoding="utf-8"))
            replacement_response = self._invoke_web_handler(
                panel,
                "do_POST",
                "/api/config",
                {
                    access_key: replacement_access_value,
                    image_key: replacement_image_value,
                    "video_caption_max_mib": 4,
                },
            )
            replaced = json.loads(config_path.read_text(encoding="utf-8"))
            refreshed_response = self._invoke_web_handler(
                panel, "do_GET", "/api/config"
            )

        self.assertEqual(post_response, {"data": {"ok": True}, "code": 200})
        self.assertEqual(
            replacement_response, {"data": {"ok": True}, "code": 200}
        )
        self.assertEqual(saved["buffer_seconds"], 7)
        self.assertEqual(saved["uia_fixed_calibration"], calibration)
        self.assertEqual(saved[access_key], original_access_value)
        self.assertEqual(saved[image_key], original_image_value)
        self.assertEqual(replaced[access_key], replacement_access_value)
        self.assertEqual(replaced[image_key], replacement_image_value)
        self.assertEqual(replaced["video_caption_max_mib"], 4)
        self.assertEqual(
            refreshed_response["data"]["video_caption_max_mib"],
            4,
        )
        self.assertEqual(replaced["uia_fixed_calibration"], calibration)

    def test_task5_config_errors_return_fixed_codes_without_local_paths(self):
        calibration = {"schema_version": 1, "completed": False}
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            missing_config = root / "private-directory" / "config.json"
            log_path = root / "bridge.log"
            log_path.write_text("", encoding="utf-8")
            panel, _, _ = self._load_web_panel(
                calibration, missing_config, log_path
            )

            get_response = self._invoke_web_handler(
                panel, "do_GET", "/api/config"
            )
            post_response = self._invoke_web_handler(
                panel,
                "do_POST",
                "/api/config",
                {"buffer_seconds": 7},
            )

        self.assertEqual(
            get_response,
            {"data": {"error": "E_CONFIG_READ"}, "code": 500},
        )
        self.assertEqual(
            post_response,
            {
                "data": {"ok": False, "error": "E_CONFIG_SAVE"},
                "code": 500,
            },
        )
        responses = json.dumps(
            [get_response, post_response], ensure_ascii=False
        )
        self.assertNotIn(str(missing_config), responses)

    def test_task5_web_ui_has_no_sender_selector_or_send_api_field(self):
        source = (BRIDGE / "web_panel.py").read_text(encoding="utf-8")
        self.assertNotIn("{key:'send_method'", source)
        self.assertNotIn("{key:'weflow_send_api'", source)
        self.assertNotIn("cfg_send_method", source)
        self.assertNotIn("s.send_method", source)
        self.assertIn("s.sender_mode", source)
        self.assertIn(
            "key === 'access_token' || key === 'image_caption_api_key'",
            source,
        )

    def test_task4_runtime_scope_uses_only_direct_uia_fixed_sender(self):
        main_source = (BRIDGE / "main.py").read_text(encoding="utf-8")
        config_source = (BRIDGE / "config.py").read_text(encoding="utf-8")

        self.assertIn(
            "from uia_fixed_sender import UiaFixedSender",
            main_source,
        )
        self.assertIn(
            "sender = UiaFixedSender(",
            main_source,
        )
        self.assertIn("state.sender_instance = sender", main_source)
        self.assertIn(
            "pre_paste_preview_delay=config.UIA_FIXED_PRE_PASTE_PREVIEW_DELAY",
            main_source,
        )
        self.assertIn(
            "pre_send_delay=config.UIA_FIXED_PRE_SEND_DELAY",
            main_source,
        )
        self.assertIn("sender_mode=uia_fixed", main_source)
        self.assertIn(
            'UIA_FIXED_CALIBRATION = config.get("uia_fixed_calibration")',
            config_source,
        )
        config_tree = ast.parse(config_source, filename="config.py")
        sensitive_filter_assignment = next(
            node
            for node in config_tree.body
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name)
                and target.id == "_sensitive_value_filter"
                for target in node.targets
            )
        )
        self.assertNotIn(
            "UIA_FIXED_CALIBRATION",
            ast.unparse(sensitive_filter_assignment.value),
        )

        for name in TASK4_RUNTIME_SOURCE_FILES:
            source = (BRIDGE / name).read_text(encoding="utf-8")
            for marker in TASK4_LEGACY_MARKERS:
                with self.subTest(file=name, marker=marker):
                    self.assertNotIn(marker, source)

        for name in TASK4_REMOVED_SOURCE_FILES:
            with self.subTest(file=name):
                self.assertFalse((BRIDGE / name).exists())

    def test_ob_client_awaits_api_requests_in_websocket_arrival_order(self):
        source = (BRIDGE / "ob_client.py").read_text(encoding="utf-8")
        tree = ast.parse(source, filename="ob_client.py")
        awaited_handlers = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Await)
            and isinstance(node.value, ast.Call)
            and _call_name(node.value.func) == "_handle_ob_api"
        ]
        detached_handlers = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and _call_name(node.func) == "create_task"
            and node.args
            and isinstance(node.args[0], ast.Call)
            and _call_name(node.args[0].func) == "_handle_ob_api"
        ]
        self.assertEqual(len(awaited_handlers), 1)
        self.assertEqual(detached_handlers, [])

    def test_task4_dependencies_keep_only_supported_sender_runtime_packages(self):
        expected_requirements = {
            "requests>=2.31.0",
            "pyperclip>=1.8.2",
            "Pillow>=10.0.0",
            "websockets>=12.0",
        }
        expected_lock = {
            "requests==2.34.2",
            "pyperclip==1.11.0",
            "Pillow==12.2.0",
            "websockets==16.0",
        }

        requirements = set(
            (BRIDGE / "requirements.txt").read_text(encoding="utf-8").splitlines()
        )
        lock = set(
            (BRIDGE / "requirements.lock").read_text(encoding="utf-8").splitlines()
        )
        self.assertEqual(requirements, expected_requirements)
        self.assertEqual(lock, expected_lock)

    def test_ob_sender_success_logs_require_literal_true_for_text_image_and_face(self):
        class FakeWebSocket:
            async def send(self, _payload):
                return None

        class FakeSender:
            def __init__(self, result):
                self.result = result

            def send_text(self, _contact, _text):
                return self.result

            def send_image(self, _contact, _image_path):
                return self.result

        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            image_path = root / "task4-image.png"
            image_path.write_bytes(b"not-opened-by-fake-sender")

            state_module = types.ModuleType("state")
            state_module._ob_ws = FakeWebSocket()
            state_module._ob_id_to_contact = {7: "private-contact"}
            config_module = types.ModuleType("config")
            config_module.ASTRBOT_ATTACHMENTS = str(root)
            requests_module = types.ModuleType("requests")
            self._configure_verified_private_route(
                state_module,
                config_module,
                requests_module,
            )

            spec = importlib.util.spec_from_file_location(
                "task4_ob_protocol_under_test",
                BRIDGE / "ob_protocol.py",
            )
            protocol = importlib.util.module_from_spec(spec)
            with mock.patch.dict(
                sys.modules,
                {
                    "state": state_module,
                    "config": config_module,
                    "requests": requests_module,
                },
            ):
                spec.loader.exec_module(protocol)
            messages = {
                "文字": [{"type": "text", "data": {"text": "private-body"}}],
                "图片": [{"type": "image", "data": {"file": image_path.name}}],
                "表情": [{"type": "face", "data": {"id": 1}}],
            }
            for result in (False, None, 1):
                for label, message in messages.items():
                    with self.subTest(result=result, segment=label):
                        if label == "图片":
                            image_path.write_bytes(b"not-opened-by-fake-sender")
                        state_module.sender_instance = FakeSender(result)
                        request = {
                            "action": "send_private_msg",
                            "params": {"user_id": 7, "message": message},
                            "echo": "task4",
                        }
                        with self.assertLogs("ob11-bridge", level="INFO") as logs:
                            asyncio.run(protocol._handle_ob_api(request))

                        output = "\n".join(logs.output)
                        self.assertIn('"event":"outbound"', output)
                        self.assertIn('"status":"failed"', output)
                        self.assertIn('"contact":"private-contact"', output)
                        if label == "文字":
                            expected_body = "private-body"
                        elif label == "图片":
                            expected_body = "[图片]"
                        else:
                            expected_body = "[表情]"
                        self.assertIn(
                            f'"body":"{expected_body}"',
                            output,
                        )
                        self.assertNotIn(image_path.name, output)

            for label, message in messages.items():
                with self.subTest(result=True, segment=label):
                    if label == "图片":
                        image_path.write_bytes(b"not-opened-by-fake-sender")
                    state_module.sender_instance = FakeSender(True)
                    request = {
                        "action": "send_private_msg",
                        "params": {"user_id": 7, "message": message},
                        "echo": "task4",
                    }
                    with self.assertLogs("ob11-bridge", level="INFO") as logs:
                        asyncio.run(protocol._handle_ob_api(request))

                    output = "\n".join(logs.output)
                    self.assertIn('"event":"outbound"', output)
                    self.assertIn('"status":"sent"', output)
                    self.assertIn('"contact":"private-contact"', output)
                    if label == "文字":
                        expected_body = "private-body"
                    elif label == "图片":
                        expected_body = "[图片]"
                    else:
                        expected_body = "[表情]"
                    self.assertIn(
                        f'"body":"{expected_body}"',
                        output,
                    )
                    self.assertNotIn(image_path.name, output)

    def test_ob_sender_exceptions_log_one_failed_chat_record_without_details(self):
        class FakeWebSocket:
            async def send(self, _payload):
                return None

        class RaisingSender:
            def send_text(self, _contact, _text):
                raise RuntimeError("private sender detail")

            def send_image(self, _contact, _image_path):
                raise RuntimeError("private image sender detail")

        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            image_path = root / "task4-exception-image.png"
            image_path.write_bytes(b"not-opened-by-raising-sender")

            state_module = types.ModuleType("state")
            state_module._ob_ws = FakeWebSocket()
            state_module._ob_id_to_contact = {7: "private-contact"}
            state_module.sender_instance = RaisingSender()
            config_module = types.ModuleType("config")
            config_module.ASTRBOT_ATTACHMENTS = str(root)
            requests_module = types.ModuleType("requests")
            self._configure_verified_private_route(
                state_module,
                config_module,
                requests_module,
            )

            spec = importlib.util.spec_from_file_location(
                "task4_ob_protocol_exception_test",
                BRIDGE / "ob_protocol.py",
            )
            protocol = importlib.util.module_from_spec(spec)
            with mock.patch.dict(
                sys.modules,
                {
                    "state": state_module,
                    "config": config_module,
                    "requests": requests_module,
                },
            ):
                spec.loader.exec_module(protocol)

            messages = {
                "文字": [{"type": "text", "data": {"text": "private-body"}}],
                "图片": [{"type": "image", "data": {"file": image_path.name}}],
                "表情": [{"type": "face", "data": {"id": 1}}],
            }
            for label, message in messages.items():
                with self.subTest(segment=label):
                    request = {
                        "action": "send_private_msg",
                        "params": {"user_id": 7, "message": message},
                        "echo": "task4",
                    }
                    with self.assertLogs("ob11-bridge", level="INFO") as logs:
                        asyncio.run(protocol._handle_ob_api(request))

                    chat_lines = [line for line in logs.output if "CHAT " in line]
                    self.assertEqual(len(chat_lines), 1)
                    output = "\n".join(logs.output)
                    self.assertIn('"event":"outbound"', output)
                    self.assertIn('"status":"failed"', output)
                    self.assertNotIn("private sender detail", output)
                    self.assertNotIn("private image sender detail", output)
                    self.assertNotIn(image_path.name, output)

    def test_ob_image_precondition_failures_log_one_failed_chat_record(self):
        class FakeWebSocket:
            async def send(self, _payload):
                return None

        class UnexpectedSender:
            def send_image(self, _contact, _image_path):
                raise AssertionError("send_image must not run")

        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            state_module = types.ModuleType("state")
            state_module._ob_ws = FakeWebSocket()
            state_module._ob_id_to_contact = {7: "private-contact"}
            state_module.sender_instance = UnexpectedSender()
            config_module = types.ModuleType("config")
            config_module.ASTRBOT_ATTACHMENTS = str(root)
            requests_module = types.ModuleType("requests")
            self._configure_verified_private_route(
                state_module,
                config_module,
                requests_module,
            )

            spec = importlib.util.spec_from_file_location(
                "task4_ob_protocol_image_precondition_test",
                BRIDGE / "ob_protocol.py",
            )
            protocol = importlib.util.module_from_spec(spec)
            with mock.patch.dict(
                sys.modules,
                {
                    "state": state_module,
                    "config": config_module,
                    "requests": requests_module,
                },
            ):
                spec.loader.exec_module(protocol)

            file_values = ("", "base64://a", "missing-private-image.png")
            for file_value in file_values:
                with self.subTest(file_value=bool(file_value)):
                    request = {
                        "action": "send_private_msg",
                        "params": {
                            "user_id": 7,
                            "message": [
                                {"type": "image", "data": {"file": file_value}}
                            ],
                        },
                        "echo": "task4",
                    }
                    with self.assertLogs("ob11-bridge", level="INFO") as logs:
                        asyncio.run(protocol._handle_ob_api(request))

                    chat_lines = [line for line in logs.output if "CHAT " in line]
                    self.assertEqual(len(chat_lines), 1)
                    output = "\n".join(logs.output)
                    self.assertIn('"status":"failed"', output)
                    self.assertIn('"body":"[图片]"', output)
                    if file_value:
                        self.assertNotIn(file_value, output)

    def test_ob_malformed_supported_segments_log_failed_without_escaping(self):
        class FakeWebSocket:
            async def send(self, _payload):
                return None

        class UnexpectedSender:
            def send_text(self, _contact, _text):
                raise AssertionError("send_text must not run")

            def send_image(self, _contact, _image_path):
                raise AssertionError("send_image must not run")

        state_module = types.ModuleType("state")
        state_module._ob_ws = FakeWebSocket()
        state_module._ob_id_to_contact = {7: "private-contact"}
        state_module.sender_instance = UnexpectedSender()
        config_module = types.ModuleType("config")
        config_module.ASTRBOT_ATTACHMENTS = ""
        requests_module = types.ModuleType("requests")
        self._configure_verified_private_route(
            state_module,
            config_module,
            requests_module,
        )
        spec = importlib.util.spec_from_file_location(
            "task4_ob_protocol_malformed_segment_test",
            BRIDGE / "ob_protocol.py",
        )
        protocol = importlib.util.module_from_spec(spec)
        with mock.patch.dict(
            sys.modules,
            {
                "state": state_module,
                "config": config_module,
                "requests": requests_module,
            },
        ):
            spec.loader.exec_module(protocol)

        malformed_messages = (
            "not-a-segment-list",
            ["not-a-segment-object"],
            [{"type": [], "data": "not-an-object"}],
            [{"type": "text", "data": "not-an-object"}],
            [{"type": "text", "data": {"text": 7}}],
            [{"type": "image", "data": {"file": 7}}],
        )
        malformed_params = (
            [],
            {"user_id": [], "message": []},
            {
                "user_id": True,
                "message": [{"type": "text", "data": {"text": "private-body"}}],
            },
        )
        for params in malformed_params:
            with self.subTest(params_type=type(params).__name__):
                request = {
                    "action": "send_private_msg",
                    "params": params,
                    "echo": "task4",
                }
                with self.assertLogs("ob11-bridge", level="INFO") as logs:
                    asyncio.run(protocol._handle_ob_api(request))

                chat_lines = [line for line in logs.output if "CHAT " in line]
                self.assertEqual(len(chat_lines), 1)
                self.assertIn('"status":"failed"', chat_lines[0])

        for message in malformed_messages:
            with self.subTest(message_type=type(message).__name__):
                request = {
                    "action": "send_private_msg",
                    "params": {"user_id": 7, "message": message},
                    "echo": "task4",
                }
                with self.assertLogs("ob11-bridge", level="INFO") as logs:
                    asyncio.run(protocol._handle_ob_api(request))

                chat_lines = [line for line in logs.output if "CHAT " in line]
                self.assertEqual(len(chat_lines), 1)
                self.assertIn('"status":"failed"', chat_lines[0])
                self.assertNotIn("must not run", "\n".join(logs.output))

    def test_bridge_identity_uses_persistent_salt_without_plaintext_identifiers(self):
        def load_state(module_name):
            spec = importlib.util.spec_from_file_location(
                module_name,
                BRIDGE / "state.py",
            )
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module

        with tempfile.TemporaryDirectory() as temporary:
            with mock.patch.dict(
                os.environ,
                {"AKASHABOT_STATE_DIR": temporary},
                clear=False,
            ):
                first_state = load_state("bridge_identity_state_first")
                first_id = first_state._wxid_to_int(
                    "wxid-contact-a",
                    account="wxid-bot",
                    identity_type="private",
                )
                second_state = load_state("bridge_identity_state_second")
                repeated_id = second_state._wxid_to_int(
                    "wxid-contact-a",
                    account="wxid-bot",
                    identity_type="private",
                )
                other_id = second_state._wxid_to_int(
                    "wxid-contact-a",
                    account="wxid-bot",
                    identity_type="group",
                )

            self.assertEqual(first_id, repeated_id)
            self.assertNotEqual(first_id, other_id)
            self.assertGreater(first_id, 0)
            self.assertLessEqual(first_id, (1 << 53) - 1)

            database_path = pathlib.Path(temporary) / "bridge_identity.sqlite3"
            self.assertTrue(database_path.is_file())
            connection = sqlite3.connect(database_path)
            try:
                version = connection.execute("PRAGMA user_version").fetchone()[0]
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    ).fetchall()
                }
                self.assertIn("identity_meta", tables)
                salt = connection.execute(
                    """
                    SELECT value
                    FROM identity_meta
                    WHERE key = 'identity_salt'
                    """
                ).fetchone()
                columns = {
                    row[1]
                    for row in connection.execute(
                        "PRAGMA table_info(identity_map)"
                    ).fetchall()
                }
                rows = connection.execute(
                    """
                    SELECT identity_hmac, ob_id
                    FROM identity_map
                    ORDER BY ob_id
                    """
                ).fetchall()
            finally:
                connection.close()
            self.assertEqual(version, 2)
            self.assertIsNotNone(salt)
            self.assertEqual(len(salt[0]), 32)
            self.assertNotIn("account", columns)
            self.assertNotIn("source_id", columns)
            self.assertEqual(len(rows), 2)
            self.assertTrue(all(len(row[0]) == 32 for row in rows))
            self.assertIn(first_id, {row[1] for row in rows})
            raw_database = database_path.read_bytes()
            self.assertNotIn(b"wxid-bot", raw_database)
            self.assertNotIn(b"wxid-contact-a", raw_database)

    def test_bridge_identity_migrates_plaintext_v1_without_changing_ob_id(self):
        def load_state(module_name):
            spec = importlib.util.spec_from_file_location(
                module_name,
                BRIDGE / "state.py",
            )
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module

        with tempfile.TemporaryDirectory() as temporary:
            database_path = pathlib.Path(temporary) / "bridge_identity.sqlite3"
            connection = sqlite3.connect(database_path)
            try:
                connection.execute(
                    """
                    CREATE TABLE identity_map (
                        account TEXT NOT NULL,
                        identity_type TEXT NOT NULL,
                        source_id TEXT NOT NULL,
                        ob_id INTEGER NOT NULL UNIQUE,
                        created_at INTEGER NOT NULL,
                        PRIMARY KEY (account, identity_type, source_id)
                    )
                    """
                )
                connection.execute(
                    """
                    INSERT INTO identity_map (
                        account,
                        identity_type,
                        source_id,
                        ob_id,
                        created_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        "legacy-account-private",
                        "private",
                        "legacy-session-private",
                        424242,
                        1,
                    ),
                )
                connection.execute("PRAGMA user_version = 1")
                connection.commit()
            finally:
                connection.close()

            with mock.patch.dict(
                os.environ,
                {"AKASHABOT_STATE_DIR": temporary},
                clear=False,
            ):
                state_module = load_state("bridge_identity_state_migration")
                migrated_id = state_module._wxid_to_int(
                    "legacy-session-private",
                    account="legacy-account-private",
                    identity_type="private",
                )

            self.assertEqual(migrated_id, 424242)
            connection = sqlite3.connect(database_path)
            try:
                columns = {
                    row[1]
                    for row in connection.execute(
                        "PRAGMA table_info(identity_map)"
                    ).fetchall()
                }
                self.assertIn("identity_hmac", columns)
                self.assertNotIn("account", columns)
                self.assertNotIn("source_id", columns)
                legacy_table = connection.execute(
                    """
                    SELECT 1
                    FROM sqlite_master
                    WHERE type = 'table' AND name = 'identity_map_legacy'
                    """
                ).fetchone()
                row = connection.execute(
                    "SELECT identity_hmac, ob_id FROM identity_map"
                ).fetchone()
            finally:
                connection.close()
            self.assertIsNone(legacy_table)
            self.assertEqual(row[1], 424242)
            self.assertEqual(len(row[0]), 32)
            raw_database = database_path.read_bytes()
            self.assertNotIn(b"legacy-account-private", raw_database)
            self.assertNotIn(b"legacy-session-private", raw_database)

    def test_private_route_persists_hmac_identity_and_refreshes_routing_name(self):
        def load_state(module_name):
            spec = importlib.util.spec_from_file_location(
                module_name,
                BRIDGE / "state.py",
            )
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module

        with tempfile.TemporaryDirectory() as temporary:
            with mock.patch.dict(
                os.environ,
                {"AKASHABOT_STATE_DIR": temporary},
                clear=False,
            ):
                first_state = load_state("bridge_private_route_state_first")
                self.assertTrue(
                    hasattr(first_state, "remember_private_route"),
                    "persistent private route API is missing",
                )
                ob_id = first_state.remember_private_route(
                    "private-session-raw",
                    account="private-account-raw",
                    routing_name="唯一备注",
                )
                repeated_id = first_state.remember_private_route(
                    "private-session-raw",
                    account="private-account-raw",
                    routing_name="更新备注",
                )
                volatile_binding = first_state.get_private_route_binding(ob_id)
                second_state = load_state("bridge_private_route_state_second")
                route = second_state.get_private_route(ob_id)

                self.assertEqual(repeated_id, ob_id)
                self.assertEqual(
                    volatile_binding,
                    {
                        "ob_id": ob_id,
                        "account": "private-account-raw",
                        "session": "private-session-raw",
                        "routing_name": "更新备注",
                    },
                )
                self.assertIsNone(second_state.get_private_route_binding(ob_id))
                self.assertIsNotNone(route)
                self.assertEqual(route["routing_name"], "更新备注")
                self.assertTrue(
                    second_state.private_route_matches(
                        route,
                        account="private-account-raw",
                        session="private-session-raw",
                    )
                )
                self.assertFalse(
                    second_state.private_route_matches(
                        route,
                        account="private-account-raw",
                        session="different-session",
                    )
                )

            database_path = pathlib.Path(temporary) / "bridge_identity.sqlite3"
            connection = sqlite3.connect(database_path)
            try:
                columns = {
                    row[1]
                    for row in connection.execute(
                        "PRAGMA table_info(private_routes)"
                    ).fetchall()
                }
                rows = connection.execute(
                    """
                    SELECT ob_id, identity_hmac, routing_name
                    FROM private_routes
                    """
                ).fetchall()
            finally:
                connection.close()
            self.assertEqual(
                columns,
                {"ob_id", "identity_hmac", "routing_name", "updated_at"},
            )
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0][0], ob_id)
            self.assertEqual(len(rows[0][1]), 32)
            self.assertEqual(rows[0][2], "更新备注")
            raw_database = database_path.read_bytes()
            self.assertNotIn(b"private-account-raw", raw_database)
            self.assertNotIn(b"private-session-raw", raw_database)

    def test_private_send_uses_only_unique_contacts_match_for_stable_route(self):
        class FakeWebSocket:
            def __init__(self):
                self.payloads = []

            async def send(self, payload):
                self.payloads.append(json.loads(payload))

        class FakeSender:
            def __init__(self):
                self.calls = []

            def send_text(self, contact, text):
                self.calls.append((contact, text))
                return True

        class FakeResponse:
            status_code = 200

            @staticmethod
            def json():
                return {
                    "success": True,
                    "count": 1,
                    "contacts": [
                        {
                            "username": "stable-private-session",
                            "displayName": "唯一备注",
                            "remark": "唯一备注",
                            "nickname": "原昵称",
                            "alias": "alias",
                            "type": "friend",
                        }
                    ],
                }

        route = {
            "ob_id": 7,
            "identity_hmac": b"x" * 32,
            "routing_name": "唯一备注",
        }
        state_module = types.ModuleType("state")
        state_module._ob_ws = FakeWebSocket()
        state_module._ob_id_to_contact = {}
        state_module.sender_instance = FakeSender()
        state_module.get_private_route = lambda ob_id: route if ob_id == 7 else None
        state_module.private_route_matches = (
            lambda supplied_route, *, account, session: (
                supplied_route is route
                and account == "bot-account"
                and session == "stable-private-session"
            )
        )
        config_module = types.ModuleType("config")
        config_module.ASTRBOT_ATTACHMENTS = ""
        config_module.WE_FLOW_BASE_URL = "http://127.0.0.1:5031"
        config_module.ACCESS_TOKEN = "contacts-" + "token"
        config_module.BOT_WXID = "bot-account"
        request_calls = []
        requests_module = types.ModuleType("requests")

        def get_contacts(url, **kwargs):
            request_calls.append((url, kwargs))
            return FakeResponse()

        requests_module.get = get_contacts
        protocol = self._load_ob_protocol(
            "private_route_success_ob_protocol",
            state_module,
            config_module,
            requests_module,
        )

        request = {
            "action": "send_private_msg",
            "params": {
                "user_id": "7",
                "message": [
                    {"type": "text", "data": {"text": "private-body"}}
                ],
            },
            "echo": "private-route-success",
        }
        with self.assertLogs("ob11-bridge", level="INFO"):
            asyncio.run(protocol._handle_ob_api(request))

        self.assertEqual(
            state_module.sender_instance.calls,
            [("唯一备注", "private-body")],
        )
        self.assertEqual(len(request_calls), 1)
        url, kwargs = request_calls[0]
        self.assertEqual(url, "http://127.0.0.1:5031/api/v1/contacts")
        self.assertEqual(
            kwargs["params"],
            {"keyword": "唯一备注", "limit": 10000},
        )
        self.assertEqual(
            kwargs["headers"]["Authorization"],
            "Bearer contacts-" + "token",
        )
        self.assertGreater(kwargs["timeout"], 0)
        self.assertEqual(state_module._ob_ws.payloads[-1]["status"], "ok")
        self.assertEqual(state_module._ob_ws.payloads[-1]["retcode"], 0)

    def test_private_send_failure_notifies_memory_plugin_before_failed_ack(self):
        class FakeWebSocket:
            def __init__(self):
                self.payloads = []

            async def send(self, payload):
                self.payloads.append(json.loads(payload))

        class CancelledSender:
            def send_text(self, _contact, _text):
                return False

        class FakeResponse:
            status_code = 200

            @staticmethod
            def json():
                return {
                    "success": True,
                    "count": 1,
                    "contacts": [
                        {
                            "username": "stable-private-session",
                            "displayName": "唯一备注",
                            "type": "friend",
                        }
                    ],
                }

        route = {
            "ob_id": 7,
            "identity_hmac": b"x" * 32,
            "routing_name": "唯一备注",
        }
        state_module = types.ModuleType("state")
        state_module._ob_ws = FakeWebSocket()
        state_module._ob_id_to_contact = {}
        state_module._self_id_int = 99
        state_module.sender_instance = CancelledSender()
        state_module.get_private_route = lambda ob_id: route if ob_id == 7 else None
        state_module.private_route_matches = (
            lambda supplied_route, *, account, session: (
                supplied_route is route
                and account == "bot-account"
                and session == "stable-private-session"
            )
        )
        config_module = types.ModuleType("config")
        config_module.ASTRBOT_ATTACHMENTS = ""
        config_module.WE_FLOW_BASE_URL = "http://127.0.0.1:5031"
        config_module.ACCESS_TOKEN = "contacts-" + "token"
        config_module.BOT_WXID = "bot-account"
        requests_module = types.ModuleType("requests")
        requests_module.get = lambda *_args, **_kwargs: FakeResponse()
        protocol = self._load_ob_protocol(
            "private_route_send_failure_ob_protocol",
            state_module,
            config_module,
            requests_module,
        )

        request = {
            "action": "send_private_msg",
            "params": {
                "user_id": 7,
                "message": [
                    {"type": "text", "data": {"text": "cancelled-body"}}
                ],
            },
            "echo": "private-send-failure",
        }
        with self.assertLogs("ob11-bridge", level="INFO"):
            asyncio.run(protocol._handle_ob_api(request))

        self.assertEqual(len(state_module._ob_ws.payloads), 2)
        notice, response = state_module._ob_ws.payloads
        self.assertEqual("notice", notice["post_type"])
        self.assertEqual("akasha_send_result", notice["notice_type"])
        self.assertEqual("failed", notice["sub_type"])
        self.assertIs(notice["success"], False)
        self.assertEqual("bot-account", notice["account"])
        self.assertEqual("stable-private-session", notice["session"])
        self.assertEqual(7, notice["user_id"])
        self.assertEqual("failed", response["status"])
        self.assertNotEqual(0, response["retcode"])
        self.assertEqual("private-send-failure", response["echo"])

    def test_private_send_fails_closed_before_uia_when_route_is_not_verifiable(self):
        class FakeWebSocket:
            def __init__(self):
                self.payloads = []

            async def send(self, payload):
                self.payloads.append(json.loads(payload))

        class UnexpectedSender:
            def __init__(self):
                self.calls = []

            def send_text(self, contact, text):
                self.calls.append((contact, text))
                return True

        class FakeResponse:
            def __init__(self, payload=None, status_code=200):
                self._payload = payload
                self.status_code = status_code

            def json(self):
                return self._payload

        route = {
            "ob_id": 7,
            "identity_hmac": b"x" * 32,
            "routing_name": "同名联系人",
        }
        state_module = types.ModuleType("state")
        state_module._ob_ws = FakeWebSocket()
        state_module._ob_id_to_contact = {7: "unsafe-memory-fallback"}
        state_module.sender_instance = UnexpectedSender()
        state_module.get_private_route = lambda ob_id: route if ob_id == 7 else None
        state_module.get_private_route_binding = lambda ob_id: (
            {
                "ob_id": 7,
                "account": "bot-account",
                "session": "expected-session",
                "routing_name": "同名联系人",
            }
            if ob_id == 7
            else None
        )
        state_module.private_route_matches = (
            lambda _route, *, account, session: (
                account == "bot-account" and session == "expected-session"
            )
        )
        config_module = types.ModuleType("config")
        config_module.ASTRBOT_ATTACHMENTS = ""
        config_module.WE_FLOW_BASE_URL = "http://127.0.0.1:5031"
        config_module.ACCESS_TOKEN = "contacts-" + "token"
        config_module.BOT_WXID = "bot-account"
        requests_module = types.ModuleType("requests")
        current_result = {"value": None}

        def get_contacts(*_args, **_kwargs):
            value = current_result["value"]
            if isinstance(value, BaseException):
                raise value
            return value

        requests_module.get = get_contacts
        protocol = self._load_ob_protocol(
            "private_route_failure_ob_protocol",
            state_module,
            config_module,
            requests_module,
        )
        request = {
            "action": "send_private_msg",
            "params": {
                "user_id": 7,
                "message": [
                    {"type": "text", "data": {"text": "must-not-send"}}
                ],
            },
            "echo": "private-route-failure",
        }
        cases = {
            "duplicate routing name": FakeResponse(
                {
                    "success": True,
                    "count": 2,
                    "contacts": [
                        {
                            "username": "expected-session",
                            "displayName": "同名联系人",
                            "type": "friend",
                        },
                        {
                            "username": "other-session",
                            "remark": "同名联系人",
                            "type": "friend",
                        },
                    ],
                }
            ),
            "stable target mismatch": FakeResponse(
                {
                    "success": True,
                    "count": 1,
                    "contacts": [
                        {
                            "username": "other-session",
                            "displayName": "同名联系人",
                            "type": "friend",
                        }
                    ],
                }
            ),
            "wrong contact type": FakeResponse(
                {
                    "success": True,
                    "count": 1,
                    "contacts": [
                        {
                            "username": "expected-session",
                            "displayName": "同名联系人",
                            "type": "group",
                        }
                    ],
                }
            ),
            "truncated result": FakeResponse(
                {
                    "success": True,
                    "count": 10000,
                    "contacts": [
                        {
                            "username": "expected-session",
                            "displayName": "同名联系人",
                            "type": "friend",
                        }
                    ],
                }
            ),
            "http failure": FakeResponse({}, status_code=503),
            "malformed payload": FakeResponse({"success": True, "contacts": {}}),
            "unavailable": RuntimeError("contacts unavailable"),
        }
        for label, result in cases.items():
            with self.subTest(case=label):
                current_result["value"] = result
                state_module._ob_ws.payloads.clear()
                state_module.sender_instance.calls.clear()
                with self.assertLogs("ob11-bridge", level="INFO"):
                    asyncio.run(protocol._handle_ob_api(request))
                self.assertEqual(state_module.sender_instance.calls, [])
                self.assertEqual(len(state_module._ob_ws.payloads), 2)
                self.assertEqual(
                    state_module._ob_ws.payloads[0]["notice_type"],
                    "akasha_send_result",
                )
                self.assertIs(
                    state_module._ob_ws.payloads[0]["success"],
                    False,
                )
                self.assertEqual(
                    state_module._ob_ws.payloads[-1]["status"],
                    "failed",
                )
                self.assertNotEqual(
                    state_module._ob_ws.payloads[-1]["retcode"],
                    0,
                )

        state_module.get_private_route = lambda _ob_id: None
        current_result["value"] = FakeResponse(
            {"success": True, "count": 0, "contacts": []}
        )
        state_module._ob_ws.payloads.clear()
        with self.assertLogs("ob11-bridge", level="INFO"):
            asyncio.run(protocol._handle_ob_api(request))
        self.assertEqual(state_module.sender_instance.calls, [])
        self.assertEqual(
            state_module._ob_ws.payloads[0]["notice_type"],
            "akasha_send_result",
        )
        self.assertEqual(state_module._ob_ws.payloads[-1]["status"], "failed")

        state_module.get_private_route = (
            lambda ob_id: route if ob_id == 7 else None
        )
        state_module.private_route_matches = (
            lambda _route, *, account, session: True
        )
        config_module.BOT_WXID = ""
        current_result["value"] = FakeResponse(
            {
                "success": True,
                "count": 1,
                "contacts": [
                    {
                        "username": "expected-session",
                        "displayName": "同名联系人",
                        "type": "friend",
                    }
                ],
            }
        )
        state_module._ob_ws.payloads.clear()
        state_module.sender_instance.calls.clear()
        with self.assertLogs("ob11-bridge", level="INFO"):
            asyncio.run(protocol._handle_ob_api(request))
        self.assertEqual(state_module.sender_instance.calls, [])
        self.assertEqual(
            state_module._ob_ws.payloads[0]["notice_type"],
            "akasha_send_result",
        )
        self.assertEqual(state_module._ob_ws.payloads[-1]["status"], "failed")

    def test_group_send_keeps_existing_route_without_contacts_api(self):
        class FakeWebSocket:
            def __init__(self):
                self.payloads = []

            async def send(self, payload):
                self.payloads.append(json.loads(payload))

        class FakeSender:
            def __init__(self):
                self.calls = []

            def send_text(self, contact, text):
                self.calls.append((contact, text))
                return True

        state_module = types.ModuleType("state")
        state_module._ob_ws = FakeWebSocket()
        state_module._ob_id_to_contact = {9: "现有群聊"}
        state_module.sender_instance = FakeSender()
        config_module = types.ModuleType("config")
        config_module.ASTRBOT_ATTACHMENTS = ""
        requests_module = types.ModuleType("requests")
        requests_module.get = mock.Mock(
            side_effect=AssertionError("group must not query contacts API")
        )
        protocol = self._load_ob_protocol(
            "group_route_unchanged_ob_protocol",
            state_module,
            config_module,
            requests_module,
        )
        request = {
            "action": "send_group_msg",
            "params": {
                "group_id": 9,
                "message": [{"type": "text", "data": {"text": "group-body"}}],
            },
            "echo": "group-route",
        }

        with self.assertLogs("ob11-bridge", level="INFO"):
            asyncio.run(protocol._handle_ob_api(request))

        self.assertEqual(
            state_module.sender_instance.calls,
            [("现有群聊", "group-body")],
        )
        requests_module.get.assert_not_called()
        self.assertEqual(state_module._ob_ws.payloads[-1]["status"], "ok")

    def test_onebot_message_event_contains_exact_akasha_contract(self):
        state_module = types.ModuleType("state")
        state_module._self_id_int = 91
        state_module._ob_ws = None
        state_module._ob_ws_loop = None
        state_module._ob_id_to_contact = {}
        config_module = types.ModuleType("config")
        config_module.ASTRBOT_ATTACHMENTS = ""
        requests_module = types.ModuleType("requests")

        spec = importlib.util.spec_from_file_location(
            "akasha_contract_ob_protocol",
            BRIDGE / "ob_protocol.py",
        )
        protocol = importlib.util.module_from_spec(spec)
        with mock.patch.dict(
            sys.modules,
            {
                "state": state_module,
                "config": config_module,
                "requests": requests_module,
            },
        ):
            spec.loader.exec_module(protocol)

        source_messages = [
            {
                "rawid": "server-1",
                "timestamp": 123,
                "event_fingerprint": "a" * 64,
                "buffer_ordinal": 1,
                "id_quality": "rawid",
            }
        ]
        event = protocol.make_message_event(
            "private",
            73,
            [{"type": "text", "data": {"text": "hello"}}],
            nickname="route-name",
            account="wxid-bot",
            session="wxid-contact",
            source_messages=source_messages,
            routing_name="route-name",
        )

        self.assertEqual(event["akasha_schema"], 1)
        self.assertEqual(event["account"], "wxid-bot")
        self.assertEqual(event["session"], "wxid-contact")
        self.assertEqual(event["type"], "private")
        self.assertEqual(event["source_messages"], source_messages)
        self.assertIsNot(event["source_messages"][0], source_messages[0])
        self.assertEqual(event["routing_name"], "route-name")
        json.dumps(event, ensure_ascii=False)

    def test_private_buffer_requires_session_and_preserves_source_refs(self):
        class FakeTimer:
            def __init__(self, *_args, **_kwargs):
                self.daemon = False

            def start(self):
                return None

            def cancel(self):
                return None

        identity_calls = []
        private_route_calls = []
        pushed_events = []

        state_module = types.ModuleType("state")
        state_module.group_reply_mode = "all"
        state_module._self_id_int = 1
        state_module._ob_id_to_contact = {}

        def identity_mapper(source_id, **kwargs):
            identity_calls.append((source_id, kwargs))
            return 7001

        def remember_private_route(session, **kwargs):
            private_route_calls.append((session, kwargs))
            return 7001

        state_module._wxid_to_int = identity_mapper
        state_module.remember_private_route = remember_private_route
        config_module = types.ModuleType("config")
        config_module.BOT_NICKNAMES = []
        config_module.BOT_WXID = "wxid-bot"
        config_module.BUFFER_SECONDS = 5
        ob_protocol_module = types.ModuleType("ob_protocol")

        def make_event(message_type, user_id, message, **kwargs):
            return {
                "message_type": message_type,
                "user_id": user_id,
                "message": message,
                **kwargs,
            }

        ob_protocol_module.make_message_event = make_event
        ob_protocol_module.push_event = lambda event: pushed_events.append(event) or 1
        requests_module = types.ModuleType("requests")

        spec = importlib.util.spec_from_file_location(
            "bridge_source_reference_test",
            BRIDGE / "bridge_core.py",
        )
        bridge_core = importlib.util.module_from_spec(spec)
        with mock.patch.dict(
            sys.modules,
            {
                "state": state_module,
                "config": config_module,
                "ob_protocol": ob_protocol_module,
                "requests": requests_module,
            },
        ):
            spec.loader.exec_module(bridge_core)

        bridge = bridge_core.WeFlowBridge(sender=None)
        with mock.patch.object(bridge_core.threading, "Timer", FakeTimer):
            with self.assertLogs("ob11-bridge", level="WARNING"):
                bridge.add_to_buffer(
                    {
                        "content": "missing stable identity",
                        "sourceName": "route-only-name",
                        "sessionType": "private",
                    }
                )
            self.assertEqual(bridge.pending_buffers, {})

            bridge.add_to_buffer(
                {
                    "content": "first",
                    "sourceName": "route-name",
                    "sessionId": "wxid-contact",
                    "sessionType": "private",
                    "rawid": "server-1",
                    "timestamp": 101,
                }
            )
            bridge.add_to_buffer(
                {
                    "content": "second",
                    "sourceName": "route-name",
                    "sessionId": "wxid-contact",
                    "sessionType": "private",
                    "rawid": "",
                    "timestamp": "102",
                }
            )

        refs = bridge.pending_buffers["wxid-contact"]["source_messages"]
        self.assertEqual([ref["buffer_ordinal"] for ref in refs], [1, 2])
        self.assertEqual(refs[0]["id_quality"], "rawid")
        self.assertEqual(refs[1]["id_quality"], "event_fingerprint")
        self.assertRegex(refs[0]["event_fingerprint"], r"^[0-9a-f]{64}$")

        bridge.process_sender("wxid-contact")
        self.assertEqual(
            private_route_calls,
            [
                (
                    "wxid-contact",
                    {"account": "wxid-bot", "routing_name": "route-name"},
                )
            ],
        )
        self.assertEqual(identity_calls, [])
        self.assertEqual(len(pushed_events), 1)
        event = pushed_events[0]
        self.assertEqual(event["account"], "wxid-bot")
        self.assertEqual(event["session"], "wxid-contact")
        self.assertEqual(event["routing_name"], "route-name")
        self.assertEqual(event["source_messages"], refs)

        with mock.patch.object(bridge_core.threading, "Timer", FakeTimer):
            bridge.add_to_buffer(
                {
                    "content": "group still accepted",
                    "sourceName": "group-route",
                    "senderName": "member",
                    "groupName": "group-route",
                    "sessionType": "group",
                    "rawid": "group-1",
                    "timestamp": 103,
                }
            )
        group_keys = [
            key
            for key, entry in bridge.pending_buffers.items()
            if entry["is_group"] and entry["messages"]
        ]
        self.assertEqual(len(group_keys), 1)
        bridge.process_sender(group_keys[0])
        self.assertEqual(len(pushed_events), 2)
        group_event = pushed_events[1]
        self.assertEqual(group_event["message_type"], "group")
        self.assertEqual(group_event["session"], "group-route")
        self.assertEqual(group_event["routing_name"], "group-route")
        self.assertIn(
            "member在群group-route中说：group still accepted",
            group_event["message"][1]["data"]["text"],
        )

    def test_private_buffer_is_not_pushed_when_route_persistence_fails(self):
        pushed_events = []
        state_module = types.ModuleType("state")
        state_module.group_reply_mode = "all"
        state_module._self_id_int = 1
        state_module._ob_id_to_contact = {}
        state_module.remember_private_route = mock.Mock(
            side_effect=RuntimeError("injected route persistence failure")
        )
        config_module = types.ModuleType("config")
        config_module.BOT_NICKNAMES = []
        config_module.BOT_WXID = "wxid-bot"
        config_module.BUFFER_SECONDS = 5
        ob_protocol_module = types.ModuleType("ob_protocol")
        ob_protocol_module.make_message_event = lambda *args, **kwargs: {
            "args": args,
            **kwargs,
        }
        ob_protocol_module.push_event = (
            lambda event: pushed_events.append(event) or 1
        )
        requests_module = types.ModuleType("requests")
        bridge_core = importlib.util.module_from_spec(
            importlib.util.spec_from_file_location(
                "bridge_route_persistence_failure_test",
                BRIDGE / "bridge_core.py",
            )
        )
        with mock.patch.dict(
            sys.modules,
            {
                "state": state_module,
                "config": config_module,
                "ob_protocol": ob_protocol_module,
                "requests": requests_module,
            },
        ):
            bridge_core.__spec__.loader.exec_module(bridge_core)

        bridge = bridge_core.WeFlowBridge(sender=None)
        bridge.pending_buffers["wxid-contact"] = {
            "messages": ["must not push"],
            "source_messages": [],
            "timer": None,
            "timer_version": 1,
            "processing": False,
            "contact": "route-name",
            "is_group": False,
            "source_name": "route-name",
            "group_name": "",
            "sender_in_group": "",
            "session_id_data": "wxid-contact",
        }
        caught = None
        with self.assertLogs("ob11-bridge", level="WARNING"):
            try:
                bridge.process_sender("wxid-contact")
            except RuntimeError as exc:
                caught = exc

        self.assertIsNone(caught, "route persistence failure escaped timer callback")
        self.assertEqual(pushed_events, [])
        self.assertFalse(
            bridge.pending_buffers["wxid-contact"]["processing"]
        )

        state_module.remember_private_route.side_effect = None
        state_module.remember_private_route.return_value = 7001
        state_module.remember_private_route.reset_mock()
        config_module.BOT_WXID = ""
        with self.assertLogs("ob11-bridge", level="WARNING"):
            bridge.process_sender("wxid-contact")

        state_module.remember_private_route.assert_not_called()
        self.assertEqual(pushed_events, [])
        self.assertEqual(
            bridge.pending_buffers["wxid-contact"]["messages"],
            ["must not push"],
        )
        self.assertFalse(
            bridge.pending_buffers["wxid-contact"]["processing"]
        )

    def test_sse_messages_without_rawid_are_not_added_to_processed_ids(self):
        timestamp = 2_000_000_000
        payloads = [
            {
                "content": "first",
                "sourceName": "route-name",
                "sessionId": "wxid-contact",
                "sessionType": "private",
                "timestamp": timestamp,
                "rawid": "",
            },
            {
                "content": "second",
                "sourceName": "route-name",
                "sessionId": "wxid-contact",
                "sessionType": "private",
                "timestamp": timestamp,
            },
        ]

        class FakeResponse:
            status_code = 200

            def iter_lines(self, decode_unicode=False):
                self.decode_unicode = decode_unicode
                return [
                    "data:" + json.dumps(payload, ensure_ascii=False)
                    for payload in payloads
                ]

        state_module = types.ModuleType("state")
        state_module.group_reply_mode = "all"
        config_module = types.ModuleType("config")
        config_module.BOT_NICKNAMES = []
        config_module.BOT_WXID = ""
        config_module.BUFFER_SECONDS = 5
        config_module.WE_FLOW_BASE_URL = "http://127.0.0.1:5031"
        config_module.ACCESS_TOKEN = "test-token"
        ob_protocol_module = types.ModuleType("ob_protocol")
        ob_protocol_module.make_message_event = lambda *_args, **_kwargs: {}
        ob_protocol_module.push_event = lambda _event: 1
        requests_module = types.ModuleType("requests")
        requests_module.get = lambda *_args, **_kwargs: FakeResponse()
        requests_module.exceptions = types.SimpleNamespace(
            ConnectionError=ConnectionError,
        )

        spec = importlib.util.spec_from_file_location(
            "bridge_empty_rawid_sse_test",
            BRIDGE / "bridge_core.py",
        )
        bridge_core = importlib.util.module_from_spec(spec)
        with mock.patch.dict(
            sys.modules,
            {
                "state": state_module,
                "config": config_module,
                "ob_protocol": ob_protocol_module,
                "requests": requests_module,
            },
        ):
            spec.loader.exec_module(bridge_core)

        bridge = bridge_core.WeFlowBridge(sender=None)
        bridge.start_timestamp = timestamp - 1
        bridge.add_to_buffer = mock.Mock()
        bridge.listen_sse()

        self.assertEqual(bridge.add_to_buffer.call_count, 2)
        self.assertEqual(bridge.processed_ids, set())

    def test_inbound_chat_logs_keep_complete_contacts_and_bodies(self):
        class FakeTimer:
            def __init__(self, *_args, **_kwargs):
                self.daemon = False

            def start(self):
                return None

            def cancel(self):
                return None

        state_module = types.ModuleType("state")
        state_module.group_reply_mode = "mention"
        config_module = types.ModuleType("config")
        config_module.BOT_NICKNAMES = ["测试机器人"]
        config_module.BUFFER_SECONDS = 5
        ob_protocol_module = types.ModuleType("ob_protocol")
        ob_protocol_module.push_event = lambda _event: True
        ob_protocol_module.make_message_event = lambda *_args, **_kwargs: {}
        requests_module = types.ModuleType("requests")

        spec = importlib.util.spec_from_file_location(
            "bridge_core_chat_log_test",
            BRIDGE / "bridge_core.py",
        )
        bridge_core = importlib.util.module_from_spec(spec)
        with mock.patch.dict(
            sys.modules,
            {
                "state": state_module,
                "config": config_module,
                "ob_protocol": ob_protocol_module,
                "requests": requests_module,
            },
        ):
            spec.loader.exec_module(bridge_core)

        bridge = bridge_core.WeFlowBridge(sender=None)
        private_message = {
            "content": "私聊第一行\n私聊第二行🙂",
            "sourceName": "联系人甲",
            "sessionId": "private-session",
        }
        group_message = {
            "content": "群消息正文",
            "sourceName": "项目群(12)",
            "senderName": "群成员乙",
            "groupName": "项目群(12)",
            "sessionId": "group-session@chatroom",
            "sessionType": "group",
        }

        with mock.patch.object(bridge_core.threading, "Timer", FakeTimer):
            with self.assertLogs("ob11-bridge", level="INFO") as logs:
                bridge.add_to_buffer(private_message)
                bridge.add_to_buffer(group_message)

        records = [
            json.loads(line.split("CHAT ", 1)[1])
            for line in logs.output
            if "CHAT " in line
        ]
        self.assertEqual(
            records,
            [
                {
                    "event": "inbound",
                    "scope": "private",
                    "contact": "联系人甲",
                    "status": "received",
                    "body": "私聊第一行\n私聊第二行🙂",
                },
                {
                    "event": "inbound",
                    "scope": "group",
                    "contact": "项目群",
                    "sender": "群成员乙",
                    "status": "received",
                    "body": "群消息正文",
                },
            ],
        )

    def test_ob_non_base64_attachment_with_tmp_in_path_is_not_deleted(self):
        class FakeWebSocket:
            async def send(self, _payload):
                return None

        class FakeSender:
            def send_image(self, _contact, _image_path):
                return True

        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            image_path = root / "tmp-user-attachment.png"
            image_path.write_bytes(b"user-owned-attachment")

            state_module = types.ModuleType("state")
            state_module._ob_ws = FakeWebSocket()
            state_module._ob_id_to_contact = {7: "private-contact"}
            state_module.sender_instance = FakeSender()
            config_module = types.ModuleType("config")
            config_module.ASTRBOT_ATTACHMENTS = str(root)
            requests_module = types.ModuleType("requests")
            self._configure_verified_private_route(
                state_module,
                config_module,
                requests_module,
            )

            spec = importlib.util.spec_from_file_location(
                "task4_ob_protocol_tmp_path_test",
                BRIDGE / "ob_protocol.py",
            )
            protocol = importlib.util.module_from_spec(spec)
            with mock.patch.dict(
                sys.modules,
                {
                    "state": state_module,
                    "config": config_module,
                    "requests": requests_module,
                },
            ):
                spec.loader.exec_module(protocol)

            request = {
                "action": "send_private_msg",
                "params": {
                    "user_id": 7,
                    "message": [
                        {
                            "type": "image",
                            "data": {"file": image_path.name},
                        }
                    ],
                },
                "echo": "task4",
            }
            asyncio.run(protocol._handle_ob_api(request))

            self.assertTrue(image_path.is_file())
            self.assertEqual(image_path.read_bytes(), b"user-owned-attachment")

    def _assert_failed_base64_tempfile_stage_is_cleaned(self, stage):
        state_module = types.ModuleType("state")
        config_module = types.ModuleType("config")
        config_module.ASTRBOT_ATTACHMENTS = ""
        requests_module = types.ModuleType("requests")
        spec = importlib.util.spec_from_file_location(
            f"ob_protocol_{stage}_failure_test",
            BRIDGE / "ob_protocol.py",
        )
        protocol = importlib.util.module_from_spec(spec)
        with mock.patch.dict(
            sys.modules,
            {
                "state": state_module,
                "config": config_module,
                "requests": requests_module,
            },
        ):
            spec.loader.exec_module(protocol)

        marker = RuntimeError(f"injected {stage} failure")
        real_temp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
        temp_path = pathlib.Path(real_temp.name)

        class FailingTempFile:
            def __init__(self, wrapped):
                self._wrapped = wrapped
                self.name = wrapped.name
                self.file = wrapped.file
                self.close_calls = 0

            def write(self, value):
                if stage == "write":
                    raise marker
                return self._wrapped.write(value)

            def flush(self):
                if stage == "flush":
                    raise marker
                return self._wrapped.flush()

            def close(self):
                self.close_calls += 1
                if stage == "close":
                    if self.close_calls == 1:
                        raise marker
                    raise RuntimeError("injected cleanup close failure")
                return self._wrapped.close()

        failing_temp = FailingTempFile(real_temp)
        try:
            with mock.patch.object(
                protocol.tempfile,
                "NamedTemporaryFile",
                return_value=failing_temp,
            ):
                with self.assertRaises(RuntimeError) as raised:
                    protocol._decode_base64_image("aW1hZ2UtYnl0ZXM=")

            self.assertIs(raised.exception, marker)
            self.assertGreaterEqual(failing_temp.close_calls, 1)
            if stage == "close":
                self.assertGreaterEqual(failing_temp.close_calls, 2)
            self.assertFalse(temp_path.exists())
        finally:
            try:
                real_temp.close()
            except Exception:
                pass
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass

    def test_base64_tempfile_write_failure_closes_and_removes_owned_path(self):
        self._assert_failed_base64_tempfile_stage_is_cleaned("write")

    def test_base64_tempfile_flush_failure_closes_and_removes_owned_path(self):
        self._assert_failed_base64_tempfile_stage_is_cleaned("flush")

    def test_base64_tempfile_close_failure_retries_and_removes_owned_path(self):
        self._assert_failed_base64_tempfile_stage_is_cleaned("close")

    def test_chat_log_keeps_complete_chat_while_redacting_credentials(self):
        privacy_path = BRIDGE / "privacy.py"
        self.assertTrue(privacy_path.is_file(), "bridge/privacy.py is missing")
        privacy = runpy.run_path(str(privacy_path))
        chat_record = privacy["chat_record"]
        redact_log_text = privacy["redact_log_text"]

        encoded = chat_record(
            event="inbound",
            scope="group",
            contact="项目群",
            sender="联系人甲",
            body="第一行\n第二行🙂",
            status="received",
        )
        self.assertNotIn("\n", encoded)
        record = json.loads(encoded)
        self.assertEqual(
            record,
            {
                "event": "inbound",
                "scope": "group",
                "contact": "项目群",
                "sender": "联系人甲",
                "status": "received",
                "body": "第一行\n第二行🙂",
            },
        )

        private_path = (
            "C:"
            + chr(92)
            + chr(92).join(("Users", "Example", "private.txt"))
        )
        redacted = redact_log_text(
            'body="普通正文" api_key=sk-1234567890 '
            'Authorization: Bearer abc.def.ghi '
            f"path={private_path}"
        )
        self.assertIn('body="普通正文"', redacted)
        self.assertNotIn("sk-1234567890", redacted)
        self.assertNotIn("abc.def.ghi", redacted)
        self.assertNotIn(private_path, redacted)
        self.assertGreaterEqual(redacted.count("[REDACTED]"), 3)

        forward_path = "C:" + "/" + "/".join(
            ("Users", "Example", "private.txt")
        )
        unc_path = chr(92) * 2 + chr(92).join(
            ("server", "share", "private.txt")
        )
        slash_unc_path = "//" + "/".join(
            ("server", "share", "private.txt")
        )
        unquoted_space_path = (
            "C:"
            + chr(92)
            + "Program Files"
            + chr(92)
            + chr(92).join(("Example", "private.exe"))
        )
        unquoted_unc_space_path = (
            chr(92) * 2
            + "server"
            + chr(92)
            + "Shared Folder"
            + chr(92)
            + "private.txt"
        )
        terminal_space_paths = (
            "C:" + chr(92) + "Program Files",
            chr(92) * 2 + "server" + chr(92) + "Shared Folder",
            (
                "C:"
                + chr(92)
                + chr(92).join(("Users", "Example", "My Documents"))
            ),
        )
        quoted_path = (
            '"C:'
            + chr(92)
            + chr(92).join(("Program Files", "Example", "private.exe"))
            + '"'
        )
        ordinary_backslash = "普通文本 A" + chr(92) + "B 保留"
        for local_path in (
            private_path,
            forward_path,
            unc_path,
            slash_unc_path,
            unquoted_space_path,
            unquoted_unc_space_path,
            *terminal_space_paths,
            quoted_path,
        ):
            with self.subTest(path_style=local_path[:2]):
                value = f"请打开 {local_path} 然后回复我"
                filtered = redact_log_text(value)
                self.assertNotIn(local_path, filtered)
                self.assertIn("[REDACTED]", filtered)
                self.assertTrue(filtered.endswith("然后回复我"))
        self.assertEqual(redact_log_text(ordinary_backslash), ordinary_backslash)
        for url in (
            "https://example.com/path?q=1",
            "http://example.com/path",
            "ws://127.0.0.1:11229/ws",
        ):
            with self.subTest(url=url.split(":", 1)[0]):
                value = f"请访问 {url} 然后回复我"
                self.assertEqual(redact_log_text(value), value)
        path_with_following_text = (
            "error "
            + "C:"
            + chr(92)
            + chr(92).join(("Temp", "private.txt"))
            + " user handles later"
        )
        self.assertEqual(
            redact_log_text(path_with_following_text),
            "error [REDACTED] user handles later",
        )
        lower_case_folder_path = (
            "open "
            + "C:"
            + chr(92)
            + "some private folder then reply"
        )
        self.assertEqual(
            redact_log_text(lower_case_folder_path),
            "open [REDACTED] then reply",
        )

        credential_text = "API " + "Key: private-value 正文保留"
        api_key_filtered = redact_log_text(credential_text)
        self.assertNotIn("private-value", api_key_filtered)
        self.assertTrue(api_key_filtered.endswith("正文保留"))

        separator_body = "甲\u0085乙\u2028丙\u2029丁"
        separator_encoded = chat_record(
            event="inbound",
            scope="private",
            contact="联系人甲",
            body=separator_body,
            status="received",
        )
        self.assertNotIn("\u0085", separator_encoded)
        self.assertNotIn("\u2028", separator_encoded)
        self.assertNotIn("\u2029", separator_encoded)
        self.assertEqual(
            json.loads(separator_encoded)["body"],
            separator_body,
        )

    def test_chat_log_file_end_to_end_keeps_chat_and_redacts_credentials(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            config_path = root / "data" / "config.json"
            log_dir = root / "logs"
            config_path.parent.mkdir(parents=True)
            log_dir.mkdir(parents=True)

            template = json.loads(
                (BRIDGE / "config.example.json").read_text(encoding="utf-8")
            )
            configured_credential = "configured-private-" + "token"
            configured_image_credential = "configured-private-" + "image-key"
            template["access_" + "token"] = configured_credential
            template["image_caption_" + "api_key"] = configured_image_credential
            config_path.write_text(
                json.dumps(template, ensure_ascii=False),
                encoding="utf-8",
            )

            ordinary_backslash = "A" + chr(92) + "B"
            local_path = (
                "C:"
                + chr(92)
                + chr(92).join(("Users", "Example", "private.txt"))
            )
            chat_body = (
                f"正文 {ordinary_backslash}；请打开 {local_path} 然后回复；"
                f"令牌 {configured_credential}；分隔甲\u2028乙"
            )
            script = "\n".join(
                (
                    "import logging",
                    "import config",
                    "from privacy import chat_record",
                    (
                        "entry = chat_record(event='inbound', scope='private', "
                        "contact='联系人甲', body="
                        + repr(chat_body)
                        + ", status='received')"
                    ),
                    "config.log.info('CHAT %s', entry)",
                    "[handler.flush() for handler in logging.getLogger().handlers]",
                )
            )
            environment = os.environ.copy()
            environment["AKASHABOT_CONFIG_PATH"] = str(config_path)
            environment["AKASHABOT_LOG_DIR"] = str(log_dir)
            environment["PYTHONDONTWRITEBYTECODE"] = "1"
            result = subprocess.run(
                [sys.executable, "-B", "-c", script],
                cwd=BRIDGE,
                env=environment,
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

            log_lines = (log_dir / "bridge.log").read_text(
                encoding="utf-8"
            ).splitlines()
            chat_lines = [line for line in log_lines if "CHAT " in line]
            self.assertEqual(len(chat_lines), 1)
            raw_line = chat_lines[0]
            self.assertNotIn(configured_credential, raw_line)
            self.assertNotIn(configured_image_credential, raw_line)
            record = json.loads(raw_line.split("CHAT ", 1)[1])
            self.assertEqual(record["contact"], "联系人甲")
            self.assertEqual(
                record["body"],
                chat_body.replace(configured_credential, "[REDACTED]"),
            )
            self.assertIn(local_path, record["body"])
            self.assertIn("正文", record["body"])
            self.assertIn("然后回复", record["body"])

    def test_source_logs_use_chat_records_and_exclude_raw_paths_and_exceptions(self):
        legacy_markers = (
            "data.get('content','')[:50]",
            "text[:50]",
            "content[:30]",
            "caption_text[:60]",
            "caption[:80]",
            "宸插垏鍒拌仈绯讳汉: {contact}",
        )
        for name in LOG_SOURCE_FILES:
            with self.subTest(file=name):
                source = (BRIDGE / name).read_text(encoding="utf-8")
                for marker in legacy_markers:
                    self.assertNotIn(marker, source, f"{name} contains {marker}")

                findings = _unsafe_logging_calls(source, filename=name)
                self.assertFalse(
                    findings,
                    f"{name} logs raw sensitive value(s): {findings}",
                )

        bridge_source = (BRIDGE / "bridge_core.py").read_text(encoding="utf-8")
        protocol_source = (BRIDGE / "ob_protocol.py").read_text(encoding="utf-8")
        self.assertIn('event="inbound"', bridge_source)
        self.assertIn('event="outbound"', protocol_source)

    def test_sensitive_log_scanner_rejects_representative_mutations(self):
        mutations = {
            "dict subscript message": 'log.info("%s", data["content"])',
            "recipient alias": 'log.info("%s", recipient)',
            "path alias": 'log.info("%s", path)',
            "exception alias": 'log.error("%s", failure)',
            "logger receiver": 'logger.info("%s", content)',
            "body alias": 'log.info("%s", body)',
            "event message subscript": 'log.info("%s", event["message"])',
            "event sender attribute": 'log.info("%s", event.sender_name)',
            "object logger receiver": 'self.log.info("%s", content)',
        }
        for name, source in mutations.items():
            with self.subTest(mutation=name):
                self.assertTrue(
                    _unsafe_logging_calls(source),
                    f"scanner accepted raw sensitive log mutation: {source}",
                )

        safe_logs = {
            "message length": 'log.info("%s", len(body))',
            "exception type": 'log.info("%s", type(error).__name__)',
            "structured chat record": (
                'log.info("%s", chat_record(event="inbound", scope="private", '
                'contact=contact, body=body, status="received"))'
            ),
            "object logger boolean": 'self.log.info("%s", bool(event["message"]))',
            "status constant": 'logger.info("status=connected")',
        }
        for name, source in safe_logs.items():
            with self.subTest(safe_log=name):
                self.assertFalse(
                    _unsafe_logging_calls(source),
                    f"scanner rejected metadata-only log: {source}",
                )


if __name__ == "__main__":
    unittest.main()
