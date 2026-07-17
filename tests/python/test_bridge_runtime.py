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
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[2]
BRIDGE = ROOT / "bridge"
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
SAFE_LOG_WRAPPERS = {"bool", "len", "message_meta", "pseudonym", "type"}
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
                    "print(config.BRIDGE_LOG_FILE)",
                ],
                cwd=BRIDGE,
                env=environment,
                text=True,
                capture_output=True,
                check=True,
            )
            lines = result.stdout.strip().splitlines()
            self.assertEqual(pathlib.Path(lines[-2]), config_path)
            self.assertEqual(pathlib.Path(lines[-1]), log_dir / "bridge.log")
            self.assertTrue((log_dir / "bridge.log").is_file())

    def test_state_pid_path_is_derived_from_environment_directory(self):
        source = (BRIDGE / "main.py").read_text(encoding="utf-8")
        tree = ast.parse(source, filename="main.py")
        entrypoint = next(
            node
            for node in tree.body
            if isinstance(node, ast.If)
            and isinstance(node.test, ast.Compare)
            and isinstance(node.test.left, ast.Name)
            and node.test.left.id == "__name__"
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

    def test_web_panel_uses_configured_log_file(self):
        source = (BRIDGE / "web_panel.py").read_text(encoding="utf-8")
        self.assertIn("open(config.BRIDGE_LOG_FILE", source)
        self.assertNotIn('open("bridge.log"', source)

    def _load_web_panel(self, calibration, config_path, log_path):
        state_module = types.ModuleType("state")
        state_module._ob_ws = None
        state_module._ob_ws_ready = types.SimpleNamespace(is_set=lambda: False)
        state_module.bridge_instance = None
        state_module.running = False
        state_module.paused = types.SimpleNamespace(is_set=lambda: False)
        state_module.group_reply_mode = "mention"

        config_module = types.ModuleType("config")
        config_module.CONFIG_FILE = str(config_path)
        config_module.BRIDGE_LOG_FILE = str(log_path)
        config_module.UIA_FIXED_CALIBRATION = calibration

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

    def _invoke_web_handler(self, panel, method, path, body=None):
        handler = object.__new__(panel.WebHandler)
        handler.path = path
        captured = {}

        def capture(_handler, data, code=200):
            captured["data"] = data
            captured["code"] = code

        handler.send_json = types.MethodType(capture, handler)
        if body is not None:
            payload = json.dumps(body).encode("utf-8")
            handler.headers = {"Content-Length": str(len(payload))}
            handler.rfile = io.BytesIO(payload)
        getattr(handler, method)()
        return captured

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

    def test_task5_status_keeps_operations_but_exposes_only_safe_sender_metadata(self):
        invalid = {"schema_version": 1, "completed": True}
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            config_path = root / "config.json"
            config_path.write_text("{}", encoding="utf-8")
            log_path = root / "bridge.log"
            log_path.write_text("status=ready", encoding="utf-8")
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
        self.assertIn("log", status)
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

    def test_task5_config_get_and_post_never_expose_or_overwrite_calibration(self):
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
            config_path.write_text(
                json.dumps(
                    {
                        "access_token": "private-token",
                        "buffer_seconds": 5,
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

            post_response = self._invoke_web_handler(
                panel,
                "do_POST",
                "/api/config",
                {
                    "buffer_seconds": 7,
                    "uia_fixed_calibration": {
                        "completed": True,
                        "points": {"private": "overwrite-attempt"},
                    },
                },
            )
            saved = json.loads(config_path.read_text(encoding="utf-8"))

        self.assertEqual(post_response, {"data": {"ok": True}, "code": 200})
        self.assertEqual(saved["buffer_seconds"], 7)
        self.assertEqual(saved["uia_fixed_calibration"], calibration)

    def test_task5_web_ui_has_no_sender_selector_or_send_api_field(self):
        source = (BRIDGE / "web_panel.py").read_text(encoding="utf-8")
        self.assertNotIn("{key:'send_method'", source)
        self.assertNotIn("{key:'weflow_send_api'", source)
        self.assertNotIn("cfg_send_method", source)
        self.assertNotIn("s.send_method", source)
        self.assertIn("s.sender_mode", source)

    def test_task4_runtime_scope_uses_only_direct_uia_fixed_sender(self):
        main_source = (BRIDGE / "main.py").read_text(encoding="utf-8")
        config_source = (BRIDGE / "config.py").read_text(encoding="utf-8")

        self.assertIn(
            "from uia_fixed_sender import UiaFixedSender",
            main_source,
        )
        self.assertIn(
            "state.sender_instance = UiaFixedSender(config.UIA_FIXED_CALIBRATION)",
            main_source,
        )
        self.assertIn("sender_mode=uia_fixed", main_source)
        self.assertIn(
            'UIA_FIXED_CALIBRATION = config.get("uia_fixed_calibration")',
            config_source,
        )
        config_tree = ast.parse(config_source, filename="config.py")
        privacy_filter_assignment = next(
            node
            for node in config_tree.body
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name)
                and target.id == "_privacy_filter"
                for target in node.targets
            )
        )
        self.assertNotIn(
            "UIA_FIXED_CALIBRATION",
            ast.unparse(privacy_filter_assignment.value),
        )

        for name in TASK4_RUNTIME_SOURCE_FILES:
            source = (BRIDGE / name).read_text(encoding="utf-8")
            for marker in TASK4_LEGACY_MARKERS:
                with self.subTest(file=name, marker=marker):
                    self.assertNotIn(marker, source)

        for name in TASK4_REMOVED_SOURCE_FILES:
            with self.subTest(file=name):
                self.assertFalse((BRIDGE / name).exists())

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
            private_values = (
                "private-contact",
                "private-body",
                image_path.name,
            )
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
                        self.assertNotIn(f"{label}已发送", output)
                        self.assertEqual(output.count("消息发送失败"), 1)
                        for private_value in private_values:
                            self.assertNotIn(private_value, output)

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
                    self.assertIn(f"{label}已发送", output)
                    self.assertNotIn("消息发送失败", output)

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

    def test_privacy_helpers_have_exact_metadata_only_output(self):
        privacy_path = BRIDGE / "privacy.py"
        self.assertTrue(privacy_path.is_file(), "bridge/privacy.py is missing")
        privacy = runpy.run_path(str(privacy_path))
        pseudonym = privacy["pseudonym"]
        message_meta = privacy["message_meta"]

        self.assertEqual(pseudonym("Alice"), "id:3bc51062973c")
        self.assertNotIn("Alice", pseudonym("Alice"))
        self.assertEqual(message_meta("hello"), "type=text length=5")
        self.assertEqual(message_meta(""), "type=empty length=0")
        self.assertEqual(message_meta(None), "type=empty length=0")

    def test_source_logs_exclude_raw_messages_identities_paths_and_exceptions(self):
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
            "message metadata": 'log.info("%s", message_meta(text))',
            "contact pseudonym": 'log.info("%s", pseudonym(contact))',
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
