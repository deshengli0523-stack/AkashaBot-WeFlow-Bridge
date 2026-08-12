import asyncio
import hashlib
import importlib.util
import json
import pathlib
import sqlite3
import sys
import tempfile
import time
import types
import unittest
import uuid
from unittest import mock


sys.dont_write_bytecode = True
ROOT = pathlib.Path(__file__).resolve().parents[2]
BRIDGE = ROOT / "bridge"


def _load_file(module_name, path, modules):
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    with mock.patch.dict(sys.modules, modules):
        spec.loader.exec_module(module)
    return module


REPLY_STORE = _load_file(
    f"reply_store_test_{uuid.uuid4().hex}",
    BRIDGE / "reply_store.py",
    {},
)


class MergedReplyDurabilityTests(unittest.TestCase):
    def test_request_is_durable_idempotent_and_result_requires_exact_ack(self):
        with tempfile.TemporaryDirectory(prefix="akasha-merged-store-") as temp:
            path = pathlib.Path(temp) / "reply.sqlite3"
            store = REPLY_STORE.ReplyStore(path)
            generation = store.allocate_generation()
            payload = {
                "rawid": "raw-1",
                "sessionId": "wxid-contact",
                "content": "新话题",
            }
            self.assertEqual(
                1,
                store.accept_raw_and_advance_epoch(
                    101,
                    generation,
                    "raw-1",
                    payload,
                ),
            )
            self.assertIsNone(
                store.accept_raw_and_advance_epoch(
                    101,
                    generation,
                    "raw-1",
                    payload,
                )
            )
            self.assertTrue(store.mark_raw_envelope_buffered("raw-1"))
            store.spool_batch(
                {"post_type": "message"},
                target_id=101,
                generation=generation,
                epoch=1,
                raw_ids=("raw-1",),
            )
            self.assertEqual([], store.pending_raw_envelopes())

            plugin_instance = str(uuid.uuid4())
            admission = store.create_admission(
                target_id=101,
                generation=generation,
                epoch=1,
                plugin_instance_id=plugin_instance,
            )
            request_id = str(uuid.uuid4())
            text = "第一句。\n第二句。"
            fingerprint = (
                f"101:{generation}:1:"
                + hashlib.sha256(text.encode("utf-8")).hexdigest()
            )
            accepted = store.accept_job(
                request_id=request_id,
                fingerprint=fingerprint,
                target_id=101,
                generation=generation,
                epoch=1,
                text=text,
                plugin_instance_id=plugin_instance,
                admission_token=admission,
                routing_name="唯一备注",
                account="wxid-bot",
                session="wxid-contact",
            )
            self.assertEqual("accepted", accepted["outcome"])

            reopened = REPLY_STORE.ReplyStore(path)
            self.assertTrue(
                reopened.lookup_request(request_id, fingerprint)["cached"]
            )
            self.assertIsNotNone(reopened.claim_next_job(generation))
            reopened.finish_job(request_id, outcome="sent", committed=True)
            notice = reopened.pending_notices()[0]
            self.assertTrue(notice["payload"]["success"])
            self.assertFalse(
                reopened.ack_notice(notice["notice_id"], "0" * 64)
            )
            self.assertTrue(
                reopened.ack_notice(notice["notice_id"], notice["digest"])
            )
            self.assertEqual([], reopened.pending_notices())

            reopened.advance_epoch(101, generation)
            cached = reopened.lookup_request(request_id, fingerprint)
            self.assertEqual("sent", cached["outcome"])
            with self.assertRaises(REPLY_STORE.ReplyStoreError):
                reopened.lookup_request(request_id, fingerprint + "-changed")

            unknown_request = str(uuid.uuid4())
            unknown_text = "等待人工确认的回复"
            unknown_fingerprint = (
                f"101:{generation}:2:"
                + hashlib.sha256(unknown_text.encode("utf-8")).hexdigest()
            )
            unknown_admission = reopened.create_admission(
                target_id=101,
                generation=generation,
                epoch=2,
                plugin_instance_id=plugin_instance,
            )
            reopened.accept_job(
                request_id=unknown_request,
                fingerprint=unknown_fingerprint,
                target_id=101,
                generation=generation,
                epoch=2,
                text=unknown_text,
                plugin_instance_id=plugin_instance,
                admission_token=unknown_admission,
                routing_name="唯一备注",
                account="wxid-bot",
                session="wxid-contact",
            )
            self.assertIsNotNone(reopened.claim_next_job(generation))
            reopened.finish_job(
                unknown_request,
                outcome="commit_unknown",
                error_code="E_UIA_COMMIT_UNKNOWN",
                error_stage="submit",
            )
            unknown_rows = reopened.list_commit_unknown()
            self.assertEqual(
                [unknown_request],
                [row["request_id"] for row in unknown_rows],
            )
            self.assertEqual("唯一备注", unknown_rows[0]["routing_name"])
            revision = int(unknown_rows[0]["result_revision"])
            self.assertFalse(
                reopened.resolve_commit_unknown(unknown_request, revision + 1, "not_sent")
            )
            self.assertTrue(
                reopened.resolve_commit_unknown(unknown_request, revision, "not_sent")
            )
            self.assertEqual([], reopened.list_commit_unknown())
            self.assertEqual(
                "failed",
                reopened.lookup_request(unknown_request, unknown_fingerprint)["outcome"],
            )

    def test_recovery_snapshot_and_manual_release_preserve_submit_boundaries(self):
        with tempfile.TemporaryDirectory(prefix="akasha-recovery-store-") as temp:
            store = REPLY_STORE.ReplyStore(pathlib.Path(temp) / "reply.sqlite3")
            generation = store.allocate_generation()
            plugin_instance = str(uuid.uuid4())

            epoch = store.advance_epoch(101, generation)
            admission = store.create_admission(
                target_id=101,
                generation=generation,
                epoch=epoch,
                plugin_instance_id=plugin_instance,
            )
            batch_id = store.spool_batch(
                {"post_type": "message", "message_type": "private"},
                target_id=101,
                generation=generation,
                epoch=epoch,
            )
            self.assertTrue(store.bind_batch_admission(batch_id, admission))

            snapshot = store.recovery_snapshot()
            self.assertEqual(1, snapshot["summary"]["contacts"])
            self.assertEqual(1, snapshot["summary"]["active_admissions"])
            self.assertEqual(1, snapshot["summary"]["pending_batches"])
            self.assertNotIn(
                admission,
                json.dumps(snapshot, ensure_ascii=False),
            )
            self.assertNotIn(
                plugin_instance,
                json.dumps(snapshot, ensure_ascii=False),
            )

            store.set_quarantine(101, "draft-request", "E_UIA_DRAFT_RECOVERY_REQUIRED")
            with self.assertRaisesRegex(
                REPLY_STORE.ReplyStoreError,
                "E_UIA_DRAFT_QUARANTINED",
            ):
                store.create_admission(
                    target_id=101,
                    generation=generation,
                    epoch=epoch,
                    plugin_instance_id=plugin_instance,
                )
            self.assertEqual("active", store.admission_status(admission)["state"])
            with self.assertRaisesRegex(
                REPLY_STORE.ReplyStoreError,
                "E_RECOVERY_DRAFT_REVIEW_REQUIRED",
            ):
                store.release_stale_target(101)
            self.assertTrue(store.resolve_quarantine(101, "draft-request"))

            released = store.release_stale_target(101)
            self.assertEqual(
                {"released_admissions": 1, "requeued_batches": 1},
                released,
            )
            self.assertEqual("released", store.admission_status(admission)["state"])
            self.assertEqual("", store.pending_batches()[0]["admission_token"])

            # Once a request reaches the submit ambiguity boundary, the broad
            # recovery operation must refuse it; only exact human resolution
            # with request_id + revision is allowed.
            second_epoch = store.advance_epoch(202, generation)
            second_admission = store.create_admission(
                target_id=202,
                generation=generation,
                epoch=second_epoch,
                plugin_instance_id=plugin_instance,
            )
            request_id = str(uuid.uuid4())
            text = "等待人工确认"
            fingerprint = (
                f"202:{generation}:{second_epoch}:"
                + hashlib.sha256(text.encode("utf-8")).hexdigest()
            )
            store.accept_job(
                request_id=request_id,
                fingerprint=fingerprint,
                target_id=202,
                generation=generation,
                epoch=second_epoch,
                text=text,
                plugin_instance_id=plugin_instance,
                admission_token=second_admission,
                routing_name="恢复测试联系人",
                account="wxid-bot",
                session="wxid-contact",
            )
            store.finish_job(
                request_id,
                outcome="commit_unknown",
                error_code="E_UIA_COMMIT_UNKNOWN",
                error_stage="submit",
            )
            with self.assertRaisesRegex(
                REPLY_STORE.ReplyStoreError,
                "E_RECOVERY_COMMIT_REVIEW_REQUIRED",
            ):
                store.release_stale_target(202)
            unknown_target = next(
                row
                for row in store.recovery_snapshot()["targets"]
                if row["target_id"] == 202
            )
            self.assertEqual("恢复测试联系人", unknown_target["routing_name"])
            self.assertEqual(request_id, unknown_target["commit_unknown"][0]["request_id"])

            # A new inbound epoch remains durable but is not sent to AstrBot
            # while this contact still has an exact human-review boundary.
            next_epoch = store.advance_epoch(202, generation)
            with self.assertRaisesRegex(
                REPLY_STORE.ReplyStoreError,
                "E_UIA_COMMIT_UNKNOWN",
            ):
                store.create_admission(
                    target_id=202,
                    generation=generation,
                    epoch=next_epoch,
                    plugin_instance_id=plugin_instance,
                )
            revision = unknown_target["commit_unknown"][0]["revision"]
            self.assertTrue(
                store.resolve_commit_unknown(request_id, revision, "not_sent")
            )
            self.assertTrue(
                store.create_admission(
                    target_id=202,
                    generation=generation,
                    epoch=next_epoch,
                    plugin_instance_id=plugin_instance,
                )
            )


class MergedReplyStateTests(unittest.TestCase):
    def test_manual_rehandshake_exposes_no_capability_credentials(self):
        state = _load_file(
            f"state_rehandshake_{uuid.uuid4().hex}",
            BRIDGE / "state.py",
            {},
        )
        connection = object()
        state.running = True
        state.stopping = False
        state.lifecycle_generation = 9
        state._ob_ws = connection
        state._ob_ws_ready.set()
        state._merged_capability = {
            "plugin_instance_id": str(uuid.uuid4()),
            "lease_id": "private-lease-value",
            "generation": 9,
            "connection_id": id(connection),
            "expires_monotonic": time.monotonic() + 10,
            "recovery_ready": True,
        }

        public = state.merged_reply_capability_status()
        self.assertTrue(public["ready"])
        self.assertNotIn("lease_id", public)
        self.assertNotIn("plugin_instance_id", public)

        requested = state.request_merged_reply_rehandshake()
        self.assertTrue(requested["requested"])
        self.assertEqual(9, requested["generation"])
        self.assertIsNone(state._merged_capability)
        self.assertEqual("disconnected", state.merged_reply_capability_status()["phase"])

        state.running = False
        with self.assertRaisesRegex(
            state.ReplyStoreError,
            "E_RECOVERY_MERGED_OFFLINE",
        ):
            state.request_merged_reply_rehandshake()

    def test_same_contact_epoch_cancels_before_but_not_after_commit(self):
        with tempfile.TemporaryDirectory(prefix="akasha-merged-state-") as temp:
            store = REPLY_STORE.ReplyStore(pathlib.Path(temp) / "state.sqlite3")
            state = _load_file(
                f"state_merged_{uuid.uuid4().hex}",
                BRIDGE / "state.py",
                {},
            )
            state.default_store = lambda: store
            state.running = True
            state.lifecycle_generation = store.allocate_generation()
            generation = state.lifecycle_generation
            self.assertEqual(1, state.advance_reply_epoch(101, generation))
            self.assertEqual(1, state.advance_reply_epoch(202, generation))

            first = state.begin_send_preview(
                "联系人甲",
                "旧回复",
                target_id=101,
                generation=generation,
                reply_epoch=1,
                request_id=str(uuid.uuid4()),
            )
            state.advance_reply_epoch(202, generation)
            self.assertFalse(first.is_set())
            state.advance_reply_epoch(101, generation)
            self.assertTrue(first.is_set())
            self.assertEqual("superseded", state.get_send_cancel_reason(first))
            state.end_send_preview(first)

            current = state.begin_send_preview(
                "联系人甲",
                "已到提交边界",
                target_id=101,
                generation=generation,
                reply_epoch=2,
            )
            self.assertTrue(state.try_commit_send(current))
            state.advance_reply_epoch(101, generation)
            self.assertFalse(current.is_set())

    def test_capability_opens_only_after_recovery_activation(self):
        with tempfile.TemporaryDirectory(prefix="akasha-merged-ready-") as temp:
            store = REPLY_STORE.ReplyStore(pathlib.Path(temp) / "state.sqlite3")
            state = _load_file(
                f"state_ready_{uuid.uuid4().hex}",
                BRIDGE / "state.py",
                {},
            )
            state.default_store = lambda: store
            state.running = True
            state.stopping = False
            state.lifecycle_generation = store.allocate_generation()
            state._ob_ws = object()
            plugin_instance_id = str(uuid.uuid4())
            lease = state.register_merged_reply_capability(
                plugin_instance_id=plugin_instance_id,
                generation=state.lifecycle_generation,
                protocol_version=1,
                action_version=1,
                result_schema=2,
                memory_schema=1,
                streaming_response=False,
                lifecycle_tracker_ready=True,
                connection=state._ob_ws,
            )
            self.assertFalse(state.merged_reply_ready())
            self.assertFalse(
                state.validate_merged_reply_lease(
                    plugin_instance_id=plugin_instance_id,
                    lease_id=lease["lease_id"],
                    generation=state.lifecycle_generation,
                )
            )
            self.assertTrue(
                state.validate_merged_reply_lease(
                    plugin_instance_id=plugin_instance_id,
                    lease_id=lease["lease_id"],
                    generation=state.lifecycle_generation,
                    require_ready=False,
                )
            )
            state.activate_merged_reply_capability(
                plugin_instance_id=plugin_instance_id,
                lease_id=lease["lease_id"],
                generation=state.lifecycle_generation,
                connection=state._ob_ws,
            )
            self.assertTrue(state.merged_reply_ready())

    def test_stale_lease_cannot_revoke_replacement_capability(self):
        with tempfile.TemporaryDirectory(prefix="akasha-merged-lease-") as temp:
            store = REPLY_STORE.ReplyStore(pathlib.Path(temp) / "state.sqlite3")
            state = _load_file(
                f"state_lease_{uuid.uuid4().hex}",
                BRIDGE / "state.py",
                {},
            )
            state.default_store = lambda: store
            state.running = True
            state.stopping = False
            state.lifecycle_generation = store.allocate_generation()
            state._ob_ws = object()
            plugin_instance_id = str(uuid.uuid4())

            stale = state.register_merged_reply_capability(
                plugin_instance_id=plugin_instance_id,
                generation=state.lifecycle_generation,
                protocol_version=1,
                action_version=1,
                result_schema=2,
                memory_schema=1,
                streaming_response=False,
                lifecycle_tracker_ready=True,
                connection=state._ob_ws,
            )
            current = state.register_merged_reply_capability(
                plugin_instance_id=plugin_instance_id,
                generation=state.lifecycle_generation,
                protocol_version=1,
                action_version=1,
                result_schema=2,
                memory_schema=1,
                streaming_response=False,
                lifecycle_tracker_ready=True,
                connection=state._ob_ws,
            )
            state.activate_merged_reply_capability(
                plugin_instance_id=plugin_instance_id,
                lease_id=current["lease_id"],
                generation=state.lifecycle_generation,
                connection=state._ob_ws,
            )
            self.assertTrue(state.merged_reply_ready())

            self.assertFalse(
                state.validate_merged_reply_lease(
                    plugin_instance_id=plugin_instance_id,
                    lease_id=stale["lease_id"],
                    generation=state.lifecycle_generation,
                )
            )
            self.assertTrue(state.merged_reply_ready())
            with self.assertRaises(state.ReplyStoreError):
                state.renew_merged_reply_capability(
                    plugin_instance_id=plugin_instance_id,
                    lease_id=stale["lease_id"],
                    generation=state.lifecycle_generation,
                    connection=state._ob_ws,
                )
            self.assertTrue(state.merged_reply_ready())


class MergedReplyBufferTests(unittest.TestCase):
    def test_quiet_window_resets_without_extending_max_window(self):
        state = types.ModuleType("state")
        state.group_reply_mode = "mention"
        state.lifecycle_generation = 0
        config = types.ModuleType("config")
        config.BUFFER_SECONDS = 5.0
        config.BUFFER_QUIET_SECONDS = 1.5
        config.BUFFER_MAX_SECONDS = 5.0
        config.BOT_NICKNAMES = []
        config.BOT_WXID = ""
        config.MONEY_RECEIVE_ENABLED = False
        config.WE_FLOW_BASE_URL = "http://127.0.0.1:5031"
        config.ACCESS_TOKEN = "token"

        ob_protocol = types.ModuleType("ob_protocol")
        ob_protocol.push_event = lambda _event: 1
        ob_protocol.make_message_event = lambda *args, **kwargs: {}
        privacy = types.ModuleType("privacy")
        privacy.chat_record = lambda **_kwargs: "{}"
        money_service = types.ModuleType("money_service")
        money_service.MoneyActionService = object
        money_service.WeFlowMoneySource = object
        requests = types.ModuleType("requests")
        store = types.SimpleNamespace(
            pending_raw_envelopes=lambda: [],
            mark_raw_envelope_buffered=lambda _rawid: False,
        )
        reply_store = types.ModuleType("reply_store")
        reply_store.ReplyStoreError = RuntimeError
        reply_store.default_store = lambda: store

        bridge_core = _load_file(
            f"bridge_core_merged_{uuid.uuid4().hex}",
            BRIDGE / "bridge_core.py",
            {
                "state": state,
                "config": config,
                "ob_protocol": ob_protocol,
                "privacy": privacy,
                "money_service": money_service,
                "requests": requests,
                "reply_store": reply_store,
            },
        )

        clock = [0.0]
        timers = []

        class FakeTimer:
            def __init__(self, delay, callback):
                self.delay = delay
                self.callback = callback
                self.cancelled = False
                timers.append(self)

            def start(self):
                return None

            def cancel(self):
                self.cancelled = True

        bridge = bridge_core.WeFlowBridge(sender=None)
        message = {
            "content": "消息",
            "sourceName": "联系人",
            "sessionId": "wxid-contact",
            "sessionType": "private",
        }
        with (
            mock.patch.object(bridge_core.time, "monotonic", lambda: clock[0]),
            mock.patch.object(bridge_core.threading, "Timer", FakeTimer),
        ):
            bridge.add_to_buffer(dict(message))
            self.assertAlmostEqual(1.5, timers[-1].delay)
            clock[0] = 1.0
            bridge.add_to_buffer(dict(message))
            self.assertAlmostEqual(1.5, timers[-1].delay)
            clock[0] = 4.5
            bridge.add_to_buffer(dict(message))
            self.assertAlmostEqual(0.5, timers[-1].delay)


class MergedReplyProtocolTests(unittest.TestCase):
    def test_action_only_persists_acceptance_and_never_calls_sender(self):
        class FakeWebSocket:
            def __init__(self):
                self.payloads = []

            async def send(self, payload):
                self.payloads.append(json.loads(payload))

        class FakeSender:
            def __init__(self):
                self.calls = []

            def send_text(self, *args, **kwargs):
                self.calls.append((args, kwargs))
                return True

        class StoreError(RuntimeError):
            def __init__(self, code):
                super().__init__(code)
                self.code = code

        class FakeStore:
            @staticmethod
            def lookup_request(_request_id, _fingerprint):
                return None

        store = FakeStore()
        reply_store = types.ModuleType("reply_store")
        reply_store.ReplyStoreError = StoreError
        reply_store.default_store = lambda: store
        state = types.ModuleType("state")
        state.running = True
        state.lifecycle_generation = 7
        state._self_id_int = 99
        state._ob_ws = FakeWebSocket()
        state._ob_id_to_contact = {}
        state.sender_instance = FakeSender()
        state.is_generation_running = lambda generation: generation == 7
        state.get_sender_for_generation = lambda generation: (
            state.sender_instance if generation == 7 else None
        )
        state.validate_merged_reply_lease = lambda **_kwargs: True
        state.get_private_route_binding = lambda _target: {
            "routing_name": "唯一备注",
            "account": "wxid-bot",
            "session": "wxid-contact",
        }
        accepted = []

        def accept_job(**kwargs):
            accepted.append(kwargs)
            return {
                "accepted": True,
                "request_id": kwargs["request_id"],
                "outcome": "accepted",
                "committed": False,
                "cached": False,
            }

        state.accept_merged_reply_job = accept_job
        state.record_send_result = lambda *_args, **_kwargs: None
        config = types.ModuleType("config")
        config.BOT_NICKNAMES = []
        config.BOT_WXID = "wxid-bot"
        privacy = types.ModuleType("privacy")
        privacy.chat_record = lambda **_kwargs: "{}"
        favorite = types.ModuleType("favorite_sticker")
        favorite.STICKER_KEYS = frozenset()
        requests = types.ModuleType("requests")
        protocol = _load_file(
            f"ob_protocol_merged_{uuid.uuid4().hex}",
            BRIDGE / "ob_protocol.py",
            {
                "state": state,
                "config": config,
                "requests": requests,
                "privacy": privacy,
                "favorite_sticker": favorite,
                "reply_store": reply_store,
            },
        )

        request_id = str(uuid.uuid4())
        plugin_instance = str(uuid.uuid4())
        asyncio.run(
            protocol._handle_ob_api(
                {
                    "action": "send_akasha_merged_reply",
                    "params": {
                        "user_id": 101,
                        "bridge_generation": 7,
                        "reply_epoch": 2,
                        "request_id": request_id,
                        "text": "第一句。\r\n第二句。  ",
                        "plugin_instance_id": plugin_instance,
                        "lease_id": "lease-" + "a" * 32,
                        "admission_token": "admission-" + "b" * 32,
                    },
                    "echo": "accepted",
                },
                generation=7,
            )
        )
        self.assertEqual([], state.sender_instance.calls)
        self.assertEqual(1, len(accepted))
        self.assertEqual("第一句。\n第二句。", accepted[0]["text"])
        response = state._ob_ws.payloads[-1]
        self.assertEqual("accepted", response["data"]["outcome"])


class MergedReplyPluginTests(unittest.TestCase):
    @staticmethod
    def _load_plugin():
        def decorator(*_args, **_kwargs):
            return lambda function: function

        class Plain:
            def __init__(self, text):
                self.text = text

        class FakeStar:
            def __init__(self, context, config=None):
                self.context = context
                self.config = config or {}

        registry = types.SimpleNamespace(
            _events={},
            request_agent_stop_all=lambda *_args, **_kwargs: 0,
        )
        fake_modules = {
            "astrbot": types.ModuleType("astrbot"),
            "astrbot.api": types.ModuleType("astrbot.api"),
            "astrbot.api.event": types.ModuleType("astrbot.api.event"),
            "astrbot.api.message_components": types.ModuleType(
                "astrbot.api.message_components"
            ),
            "astrbot.api.provider": types.ModuleType("astrbot.api.provider"),
            "astrbot.api.star": types.ModuleType("astrbot.api.star"),
            "astrbot.core": types.ModuleType("astrbot.core"),
            "astrbot.core.utils": types.ModuleType("astrbot.core.utils"),
            "astrbot.core.utils.active_event_registry": types.ModuleType(
                "astrbot.core.utils.active_event_registry"
            ),
        }
        fake_modules[
            "astrbot.core.utils.active_event_registry"
        ].active_event_registry = registry
        fake_modules["astrbot.api"].logger = types.SimpleNamespace(
            info=lambda *_args, **_kwargs: None,
            warning=lambda *_args, **_kwargs: None,
            error=lambda *_args, **_kwargs: None,
        )
        fake_modules["astrbot.api.event"].AstrMessageEvent = type(
            "AstrMessageEvent", (), {}
        )
        fake_modules["astrbot.api.event"].filter = types.SimpleNamespace(
            on_llm_request=decorator,
            on_decorating_result=decorator,
            on_astrbot_loaded=decorator,
            event_message_type=decorator,
            on_agent_done=decorator,
            after_message_sent=decorator,
            EventMessageType=types.SimpleNamespace(PRIVATE_MESSAGE="private"),
        )
        fake_modules["astrbot.api.message_components"].Plain = Plain
        fake_modules["astrbot.api.provider"].ProviderRequest = type(
            "ProviderRequest", (), {}
        )
        fake_modules["astrbot.api.star"].Context = type("Context", (), {})
        fake_modules["astrbot.api.star"].Star = FakeStar
        module = _load_file(
            f"merged_plugin_{uuid.uuid4().hex}",
            ROOT / "plugins" / "astrbot_plugin_akasha_merged_reply" / "main.py",
            fake_modules,
        )
        return module, Plain

    def test_readiness_requires_streaming_response_to_be_explicitly_false(self):
        module, _plain = self._load_plugin()

        class Context:
            def __init__(self, value):
                self.value = value

            def get_config(self):
                return self.value

        context = Context(
            {"provider_settings": {"streaming_response": False}}
        )
        plugin = module.Main(context=context, config={})
        self.assertTrue(plugin._streaming_disabled())

        context.value = {"provider_settings": {"streaming_response": True}}
        self.assertFalse(plugin._streaming_disabled())
        context.value = {"provider_settings": {}}
        self.assertFalse(plugin._streaming_disabled())
        context.value = None
        self.assertFalse(plugin._streaming_disabled())

    def test_readiness_requires_contact_memory_recovery_callbacks(self):
        module, _plain = self._load_plugin()
        context = types.SimpleNamespace()
        plugin = module.Main(context=context, config={})
        self.assertFalse(plugin._memory_ready())
        for name in (
            "akasha_memory_pre_action_terminal",
            "akasha_memory_acceptance",
            "akasha_memory_recovery_records",
            "akasha_memory_recovery_acceptance",
            "akasha_memory_recovery_terminal",
        ):
            setattr(context, name, lambda *_args, **_kwargs: None)
        self.assertTrue(plugin._memory_ready())

    def test_send_snapshot_waits_for_bridge_activation_confirmation(self):
        module, _plain = self._load_plugin()

        async def scenario():
            plugin = module.Main(context=types.SimpleNamespace(), config={})
            plugin._lease = {
                "lease_id": "lease-1",
                "bridge_generation": 7,
                "expires_at": time.monotonic() + 60,
                "activated": True,
                "activation_confirmed": False,
            }
            before_confirmation = await plugin._lease_snapshot()
            plugin._lease["activation_confirmed"] = True
            after_confirmation = await plugin._lease_snapshot()
            return before_confirmation, after_confirmation

        before_confirmation, after_confirmation = asyncio.run(scenario())
        self.assertIsNone(before_confirmation)
        self.assertEqual("lease-1", after_confirmation["lease_id"])

    def test_finalizer_does_not_delete_memory_when_finish_reports_consumed(self):
        module, _plain = self._load_plugin()
        terminal_calls = []

        class Bot:
            async def call_action(self, action, **_params):
                if action == module.GET_ADMISSION_ACTION:
                    raise TimeoutError("temporary status failure")
                return {"data": {"state": "consumed", "request_id": "request-1"}}

        class Context:
            async def akasha_memory_pre_action_terminal(self, *args):
                terminal_calls.append(args)
                return True

        class Event:
            bot = Bot()

            @staticmethod
            def get_extra(key, default=None):
                return "request-1" if key == module.REQUEST_ID_KEY else default

        async def scenario():
            plugin = module.Main(context=Context(), config={})
            plugin._lease = {
                "lease_id": "lease-1",
                "bridge_generation": 7,
                "expires_at": time.monotonic() + 60,
                "activated": True,
                "activation_confirmed": True,
            }
            resolved = await plugin._finalize_unconsumed(
                Event(),
                {"admission_token": "admission-1"},
                outcome="failed",
                reason="E_PROVIDER_NO_RESULT",
            )
            return resolved

        self.assertTrue(asyncio.run(scenario()))
        self.assertEqual([], terminal_calls)

    def test_restart_recovery_reconciles_consumed_and_released_admissions(self):
        module, _plain = self._load_plugin()
        accepted = []
        terminal = []
        pending = [
            {"request_id": f"accepted-{index}", "admission_token": f"a-{index}"}
            for index in range(65)
        ] + [
            {"request_id": f"released-{index}", "admission_token": f"b-{index}"}
            for index in range(65)
        ]

        class Context:
            async def akasha_memory_recovery_records(self):
                return list(pending[:128])

            async def akasha_memory_recovery_acceptance(self, request_id):
                accepted.append(request_id)
                pending[:] = [
                    record
                    for record in pending
                    if record["request_id"] != request_id
                ]
                return True

            async def akasha_memory_recovery_terminal(self, request_id, outcome, reason):
                terminal.append((request_id, outcome, reason))
                pending[:] = [
                    record
                    for record in pending
                    if record["request_id"] != request_id
                ]
                return True

        async def call_action(_action, **params):
            token = params["admission_token"]
            if token.startswith("a-"):
                return {
                    "data": {
                        "state": "consumed",
                        "request_id": "accepted-" + token[2:],
                    }
                }
            return {
                "data": {
                    "state": "released",
                    "outcome": "failed",
                    "reason": "E_PLUGIN_INSTANCE_CHANGED",
                    "request_id": "",
                }
            }

        async def scenario():
            plugin = module.Main(context=Context(), config={})
            return await plugin._recover_memory_bindings(
                call_action,
                {"lease_id": "lease-1", "bridge_generation": 7},
            )

        self.assertTrue(asyncio.run(scenario()))
        self.assertEqual(65, len(accepted))
        self.assertEqual(65, len(terminal))
        self.assertEqual([], pending)

    def test_agent_done_before_decoration_preserves_plain_result_for_one_send(self):
        module, Plain = self._load_plugin()
        calls = []

        class Bot:
            async def call_action(self, action, **params):
                calls.append((action, params))
                return {"data": {"accepted": True, "outcome": "accepted"}}

        class Context:
            async def akasha_memory_acceptance(self, _event, _status):
                return True

        class Result:
            chain = [Plain("第一句。"), Plain("第二句。")]

            @staticmethod
            def is_llm_result():
                return True

        class Event:
            bot = Bot()
            unified_msg_origin = "aiocqhttp:FriendMessage:101"

            def __init__(self):
                self.extra = {module.MEMORY_BIND_STATUS_KEY: "bound"}
                self.result = None
                self.stopped = False
                self.message_obj = types.SimpleNamespace(raw_message={})

            def get_platform_name(self):
                return "aiocqhttp"

            def is_private_chat(self):
                return True

            def get_sender_id(self):
                return "101"

            def get_extra(self, key, default=None):
                return self.extra.get(key, default)

            def set_extra(self, key, value):
                self.extra[key] = value

            def get_result(self):
                return self.result

            def clear_result(self):
                self.result = None

            def stop_event(self):
                self.stopped = True

        async def scenario():
            context = Context()
            plugin = module.Main(context=context, config={})
            event = Event()
            event.message_obj.raw_message = {
                "akasha_reply_schema": 1,
                "type": "private",
                "user_id": 101,
                "bridge_generation": 7,
                "reply_epoch": 2,
                "plugin_instance_id": plugin.plugin_instance_id,
                "admission_token": "admission-" + "c" * 32,
            }
            plugin._lease = {
                "lease_id": "lease-" + "d" * 32,
                "bridge_generation": 7,
                "expires_at": time.monotonic() + 60,
                "activated": True,
                "activation_confirmed": True,
            }
            request = types.SimpleNamespace(system_prompt="角色设定")
            await plugin.prepare_merged_reply(event, request)
            await plugin.finalize_agent(
                event,
                None,
                types.SimpleNamespace(completion_text="第一句。第二句。"),
            )
            event.result = Result()
            await plugin.claim_and_normalize(event)
            await plugin.send_merged_reply(event)
            return event, request

        event, request = asyncio.run(scenario())
        self.assertEqual(1, len(calls))
        self.assertEqual("send_akasha_merged_reply", calls[0][0])
        self.assertEqual("第一句。第二句。", calls[0][1]["text"])
        self.assertIsNone(event.result)
        self.assertTrue(event.stopped)
        self.assertIn("[AKASHA_MERGED_REPLY_V1]", request.system_prompt)

    def test_send_replays_same_request_after_concurrent_lease_replacement(self):
        module, Plain = self._load_plugin()
        calls = []
        acceptances = []
        terminals = []
        plugin_holder = {}

        class Bot:
            async def call_action(self, action, **params):
                calls.append((action, params))
                if len(calls) == 1:
                    plugin_holder["plugin"]._lease = {
                        "lease_id": "lease-new",
                        "bridge_generation": 7,
                        "expires_at": time.monotonic() + 60,
                        "activated": True,
                        "activation_confirmed": True,
                    }
                    return {
                        "status": "failed",
                        "retcode": 1403,
                        "data": {
                            "confirmed": False,
                            "error_code": "E_MERGED_REPLY_LEASE_INVALID",
                            "error_stage": "request",
                            "committed": False,
                        },
                    }
                return {"data": {"accepted": True, "outcome": "accepted"}}

        class Context:
            async def akasha_memory_acceptance(self, _event, status):
                acceptances.append(status)
                return True

            async def akasha_memory_pre_action_terminal(
                self,
                _event,
                outcome,
                reason,
            ):
                terminals.append((outcome, reason))
                return True

        class Result:
            chain = [Plain("续租竞态后的回复")]

            @staticmethod
            def is_llm_result():
                return True

        class Event:
            bot = Bot()
            unified_msg_origin = "aiocqhttp:FriendMessage:101"

            def __init__(self):
                self.extra = {module.MEMORY_BIND_STATUS_KEY: "bound"}
                self.result = Result()
                self.stopped = False
                self.message_obj = types.SimpleNamespace(raw_message={})

            def get_platform_name(self):
                return "aiocqhttp"

            def is_private_chat(self):
                return True

            def get_sender_id(self):
                return "101"

            def get_extra(self, key, default=None):
                return self.extra.get(key, default)

            def set_extra(self, key, value):
                self.extra[key] = value

            def get_result(self):
                return self.result

            def clear_result(self):
                self.result = None

            def stop_event(self):
                self.stopped = True

        async def scenario():
            plugin = module.Main(context=Context(), config={})
            plugin_holder["plugin"] = plugin
            event = Event()
            event.message_obj.raw_message = {
                "akasha_reply_schema": 1,
                "type": "private",
                "user_id": 101,
                "bridge_generation": 7,
                "reply_epoch": 2,
                "plugin_instance_id": plugin.plugin_instance_id,
                "admission_token": "admission-" + "c" * 32,
            }
            plugin._lease = {
                "lease_id": "lease-old",
                "bridge_generation": 7,
                "expires_at": time.monotonic() + 60,
                "activated": True,
                "activation_confirmed": True,
            }
            request = types.SimpleNamespace(system_prompt="角色设定")
            await plugin.prepare_merged_reply(event, request)
            await plugin.claim_and_normalize(event)
            await plugin.send_merged_reply(event)
            return event

        event = asyncio.run(scenario())
        self.assertEqual(2, len(calls))
        self.assertEqual(
            ["lease-old", "lease-new"],
            [params["lease_id"] for _action, params in calls],
        )
        self.assertEqual(
            calls[0][1]["request_id"],
            calls[1][1]["request_id"],
        )
        self.assertEqual(
            calls[0][1]["admission_token"],
            calls[1][1]["admission_token"],
        )
        self.assertEqual(["accepted"], acceptances)
        self.assertEqual([], terminals)
        self.assertIsNone(event.result)
        self.assertTrue(event.stopped)


class MergedReplyMemoryTests(unittest.TestCase):
    def test_revisions_reconcile_only_the_request_bound_generated_turn(self):
        plugin_root = ROOT / "plugins" / "astrbot_plugin_akasha_contact_memory"
        sys.path.insert(0, str(plugin_root))
        try:
            from akasha_memory.security import SecretManager
            from akasha_memory.store import MemoryStore
        finally:
            sys.path.remove(str(plugin_root))

        async def scenario():
            with tempfile.TemporaryDirectory(prefix="akasha-merged-memory-") as temp:
                root = pathlib.Path(temp)
                secrets = SecretManager(root)
                store = MemoryStore(root)
                await store.initialize()
                contact = await store.ensure_contact(
                    contact_hmac=secrets.contact_hmac("account", "session"),
                    account_enc=secrets.encrypt_text("account"),
                    session_enc=secrets.encrypt_text("session"),
                    routing_name="contact",
                )
                await store.archive_generated(
                    contact.id,
                    response_id="first",
                    content="相同正文",
                    source_time=1,
                )
                await store.archive_generated(
                    contact.id,
                    response_id="second",
                    content="相同正文",
                    source_time=2,
                )
                newest_request = str(uuid.uuid4())
                older_request = str(uuid.uuid4())
                for request_id, response_id in (
                    (older_request, "first"),
                    (newest_request, "second"),
                ):
                    self.assertEqual(
                        "bound",
                        await store.bind_merged_reply(
                            request_id=request_id,
                            contact_id=contact.id,
                            target_id=101,
                            bridge_generation=7,
                            reply_epoch=2,
                            admission_token="admission-" + response_id,
                            response_id=response_id,
                            text="相同正文",
                            applicable=True,
                        ),
                    )
                await store.apply_merged_reply_result(
                    request_id=newest_request,
                    notice_id=str(uuid.uuid4()),
                    result_digest="a" * 64,
                    result_revision=1,
                    target_id=101,
                    bridge_generation=7,
                    reply_epoch=2,
                    outcome="commit_unknown",
                )
                await store.apply_merged_reply_result(
                    request_id=newest_request,
                    notice_id=str(uuid.uuid4()),
                    result_digest="b" * 64,
                    result_revision=2,
                    target_id=101,
                    bridge_generation=7,
                    reply_epoch=2,
                    outcome="superseded",
                    discarded_parts=("相同正文",),
                )
                await store.apply_merged_reply_result(
                    request_id=newest_request,
                    notice_id=str(uuid.uuid4()),
                    result_digest="c" * 64,
                    result_revision=1,
                    target_id=101,
                    bridge_generation=7,
                    reply_epoch=2,
                    outcome="commit_unknown",
                )
                connection = sqlite3.connect(store.path)
                try:
                    rows = connection.execute(
                        """
                        SELECT source_uid FROM messages
                        WHERE contact_id=? AND origin='generated'
                        """,
                        (contact.id,),
                    ).fetchall()
                    binding = connection.execute(
                        """
                        SELECT status,result_revision,outcome
                        FROM merged_reply_bindings WHERE request_id=?
                        """,
                        (newest_request,),
                    ).fetchone()
                finally:
                    connection.close()
                self.assertEqual([("generated:first",)], rows)
                self.assertEqual(("result_applied", 2, "superseded"), binding)

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
