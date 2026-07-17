import ast
import json
import os
import pathlib
import re
import runpy
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[2]
BRIDGE = ROOT / "bridge"
LOG_SOURCE_FILES = (
    "bridge_core.py",
    "ob_protocol.py",
    "senders.py",
    "uia_sender.py",
    "uia_fixed_sender.py",
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
