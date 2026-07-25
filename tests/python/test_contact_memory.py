import asyncio
from contextlib import asynccontextmanager
import importlib.util
import json
import pathlib
import sqlite3
import sys
import tempfile
import time
import types
import unittest
from unittest import mock

sys.dont_write_bytecode = True

ROOT = pathlib.Path(__file__).resolve().parents[2]
PLUGIN = ROOT / "plugins" / "astrbot_plugin_akasha_contact_memory"
sys.path.insert(0, str(PLUGIN))

from akasha_memory.context_builder import ContextBuilder  # noqa: E402
from akasha_memory.models import ContactBinding  # noqa: E402
from akasha_memory.models import MemoryMessage  # noqa: E402
from akasha_memory.models import QwenResult  # noqa: E402
from akasha_memory.models import QwenToolCall  # noqa: E402
from akasha_memory.security import SecretManager  # noqa: E402
from akasha_memory.store import MemoryStore  # noqa: E402


def _load_plugin_main_with_astrbot_stubs():
    def decorator(*_args, **_kwargs):
        def apply(function):
            return function

        return apply

    def command_group(*_args, **_kwargs):
        def apply(function):
            function.command = decorator
            return function

        return apply

    filter_stub = types.SimpleNamespace(
        EventMessageType=types.SimpleNamespace(PRIVATE_MESSAGE="private"),
        PermissionType=types.SimpleNamespace(ADMIN="admin"),
        after_message_sent=decorator,
        command_group=command_group,
        event_message_type=decorator,
        on_agent_done=decorator,
        on_astrbot_loaded=decorator,
        on_llm_request=decorator,
        permission_type=decorator,
    )

    class FakeStar:
        def __init__(self, context, config=None):
            self.context = context
            self.config = config or {}

    class FakeProvider:
        pass

    provider_cls_map = {}

    def register_provider_adapter(provider_type, _description):
        def apply(provider_class):
            provider_cls_map[provider_type] = provider_class
            return provider_class

        return apply

    fake_modules = {
        "aiohttp": types.ModuleType("aiohttp"),
        "astrbot": types.ModuleType("astrbot"),
        "astrbot.api": types.ModuleType("astrbot.api"),
        "astrbot.api.event": types.ModuleType("astrbot.api.event"),
        "astrbot.api.provider": types.ModuleType("astrbot.api.provider"),
        "astrbot.api.star": types.ModuleType("astrbot.api.star"),
        "astrbot.core": types.ModuleType("astrbot.core"),
        "astrbot.core.provider": types.ModuleType("astrbot.core.provider"),
        "astrbot.core.provider.entities": types.ModuleType(
            "astrbot.core.provider.entities"
        ),
        "astrbot.core.provider.register": types.ModuleType(
            "astrbot.core.provider.register"
        ),
    }
    fake_modules["astrbot.api"].logger = types.SimpleNamespace(
        error=lambda *_args, **_kwargs: None,
        info=lambda *_args, **_kwargs: None,
    )
    fake_modules["astrbot.api.event"].AstrMessageEvent = type(
        "AstrMessageEvent", (), {}
    )
    fake_modules["astrbot.api.event"].filter = filter_stub
    fake_modules["astrbot.api.provider"].Provider = FakeProvider
    fake_modules["astrbot.api.provider"].ProviderRequest = type(
        "ProviderRequest", (), {}
    )
    fake_modules["astrbot.api.star"].Context = type("Context", (), {})
    fake_modules["astrbot.api.star"].Star = FakeStar
    fake_modules["astrbot.api.star"].StarTools = type("StarTools", (), {})
    fake_modules["astrbot.core.provider.entities"].LLMResponse = type(
        "LLMResponse", (), {}
    )
    fake_modules["astrbot.core.provider.entities"].ProviderType = (
        types.SimpleNamespace(CHAT_COMPLETION="chat")
    )
    fake_modules["astrbot.core.provider.entities"].TokenUsage = type(
        "TokenUsage", (), {}
    )
    fake_modules["astrbot.core.provider.register"].provider_cls_map = (
        provider_cls_map
    )
    fake_modules[
        "astrbot.core.provider.register"
    ].register_provider_adapter = register_provider_adapter

    package_name = "_akasha_contact_memory_plugin_test"
    package = types.ModuleType(package_name)
    package.__path__ = [str(PLUGIN)]
    fake_modules[package_name] = package
    spec = importlib.util.spec_from_file_location(
        f"{package_name}.main",
        PLUGIN / "main.py",
    )
    module = importlib.util.module_from_spec(spec)
    fake_modules[spec.name] = module
    with mock.patch.dict(sys.modules, fake_modules):
        spec.loader.exec_module(module)
    return module


class ContactMemoryTests(unittest.TestCase):
    def test_provider_binding_waits_for_astrbot_loaded_hook(self):
        module = _load_plugin_main_with_astrbot_stubs()

        async def scenario():
            manager = types.SimpleNamespace(inst_map={})
            context = types.SimpleNamespace(provider_manager=manager)
            plugin = module.Main(context, {})
            cold_calls = 0

            async def initialize_cold():
                nonlocal cold_calls
                cold_calls += 1
                await asyncio.sleep(0)
                plugin.runtime = object()

            plugin._initialize_runtime_locked = initialize_cold
            await plugin.initialize()
            self.assertEqual(0, cold_calls)

            await asyncio.gather(
                plugin.initialize_after_astrbot_loaded(),
                plugin.initialize_after_astrbot_loaded(),
            )
            self.assertEqual(1, cold_calls)
            self.assertTrue(
                getattr(manager, module.ASTRBOT_LOADED_MARKER)
            )

            hot_plugin = module.Main(context, {})
            hot_calls = 0

            async def initialize_hot():
                nonlocal hot_calls
                hot_calls += 1
                hot_plugin.runtime = object()

            hot_plugin._initialize_runtime_locked = initialize_hot
            await hot_plugin.initialize()
            self.assertEqual(1, hot_calls)

            late_manager = types.SimpleNamespace(
                inst_map={},
                _mcp_init_task=object(),
            )
            late_context = types.SimpleNamespace(
                provider_manager=late_manager
            )
            late_plugin = module.Main(late_context, {})
            late_calls = 0

            async def initialize_late():
                nonlocal late_calls
                late_calls += 1
                late_plugin.runtime = object()

            late_plugin._initialize_runtime_locked = initialize_late
            await late_plugin.initialize()
            self.assertEqual(1, late_calls)

            retry_plugin = module.Main(context, {})
            retry_calls = 0

            async def initialize_with_retry():
                nonlocal retry_calls
                retry_calls += 1
                if retry_calls == 1:
                    raise RuntimeError("fixture initialization failure")
                retry_plugin.runtime = object()

            retry_plugin._initialize_runtime_locked = initialize_with_retry
            with self.assertRaisesRegex(
                RuntimeError, "fixture initialization failure"
            ):
                await retry_plugin.initialize()
            await retry_plugin.initialize()
            self.assertEqual(2, retry_calls)

            class FakeRuntime:
                def __init__(self):
                    self.closed = False

                async def close(self):
                    self.closed = True

            racing_plugin = module.Main(context, {})
            started = asyncio.Event()
            release = asyncio.Event()
            racing_runtime = FakeRuntime()

            async def initialize_racing():
                started.set()
                await release.wait()
                racing_plugin.runtime = racing_runtime

            racing_plugin._initialize_runtime_locked = initialize_racing
            initialize_task = asyncio.create_task(racing_plugin.initialize())
            await started.wait()
            terminate_task = asyncio.create_task(racing_plugin.terminate())
            await asyncio.sleep(0)
            self.assertFalse(terminate_task.done())
            release.set()
            await asyncio.gather(initialize_task, terminate_task)
            self.assertTrue(racing_runtime.closed)
            self.assertIsNone(racing_plugin.runtime)
            self.assertTrue(racing_plugin._terminated)

        asyncio.run(scenario())

    def test_contact_key_is_stable_but_session_scoped(self):
        with tempfile.TemporaryDirectory(prefix="akasha-memory-secret-") as temp:
            root = pathlib.Path(temp)
            first = SecretManager(root)
            key_a = first.contact_hmac("account", "session-a")
            encrypted = first.encrypt_text("session-a")
            second = SecretManager(root)
            self.assertEqual(key_a, second.contact_hmac("account", "session-a"))
            self.assertNotEqual(key_a, second.contact_hmac("account", "session-b"))
            self.assertEqual("session-a", second.decrypt_text(encrypted))

    def test_memory_schema_v4_migrates_contact_and_session_revisions(self):
        async def scenario():
            with tempfile.TemporaryDirectory(prefix="akasha-memory-migrate-") as temp:
                root = pathlib.Path(temp)
                store = MemoryStore(root)
                await store.initialize()
                secrets = SecretManager(root)
                contact = await store.ensure_contact(
                    contact_hmac=secrets.contact_hmac("account", "session"),
                    account_enc=secrets.encrypt_text("account"),
                    session_enc=secrets.encrypt_text("session"),
                    routing_name="contact",
                )
                await store.create_qwen_session(
                    contact.id,
                    conversation_id="legacy-conversation",
                    model="qwen3.7-max",
                    persona_hash="persona",
                    tool_hash="tools",
                    created_at=1,
                    expires_at=time.time() + 3600,
                    estimated_tokens=100,
                    expected_memory_revision=(
                        await store.contact_memory_revision(contact.id)
                    ),
                )
                connection = sqlite3.connect(store.path)
                try:
                    connection.execute(
                        "ALTER TABLE contacts DROP COLUMN memory_revision"
                    )
                    connection.execute(
                        "ALTER TABLE qwen_sessions DROP COLUMN memory_revision"
                    )
                    connection.execute(
                        "ALTER TABLE messages DROP COLUMN semantic_content"
                    )
                    connection.execute("PRAGMA user_version = 4")
                    connection.commit()
                finally:
                    connection.close()

                await store.initialize()
                connection = sqlite3.connect(store.path)
                try:
                    version = connection.execute(
                        "PRAGMA user_version"
                    ).fetchone()[0]
                    contact_columns = {
                        row[1]
                        for row in connection.execute(
                            "PRAGMA table_info(contacts)"
                        ).fetchall()
                    }
                    session_columns = {
                        row[1]
                        for row in connection.execute(
                            "PRAGMA table_info(qwen_sessions)"
                        ).fetchall()
                    }
                    message_columns = {
                        row[1]
                        for row in connection.execute(
                            "PRAGMA table_info(messages)"
                        ).fetchall()
                    }
                finally:
                    connection.close()
                self.assertEqual(6, version)
                self.assertIn("memory_revision", contact_columns)
                self.assertIn("memory_revision", session_columns)
                self.assertIn("semantic_content", message_columns)
                session = await store.active_qwen_session(contact.id)
                self.assertIsNotNone(session)
                self.assertEqual("legacy-conversation", session.conversation_id)
                self.assertTrue(session.dirty)

        asyncio.run(scenario())

    def test_memory_schema_v5_adds_media_semantics_without_changing_rows(self):
        async def scenario():
            with tempfile.TemporaryDirectory(prefix="akasha-memory-v5-") as temp:
                root = pathlib.Path(temp)
                store = MemoryStore(root)
                await store.initialize()
                secrets = SecretManager(root)
                contact = await store.ensure_contact(
                    contact_hmac=secrets.contact_hmac("account", "session"),
                    account_enc=secrets.encrypt_text("account"),
                    session_enc=secrets.encrypt_text("session"),
                    routing_name="contact",
                )
                await store.upsert_messages(
                    contact.id,
                    [MemoryMessage("raw:legacy", 1, "in", "legacy row")],
                )
                connection = sqlite3.connect(store.path)
                try:
                    connection.execute(
                        "ALTER TABLE messages DROP COLUMN semantic_content"
                    )
                    connection.execute("PRAGMA user_version = 5")
                    connection.commit()
                finally:
                    connection.close()

                await store.initialize()
                messages = await store.recent_messages(contact.id)
                self.assertEqual(1, len(messages))
                self.assertEqual("legacy row", messages[0].content)
                self.assertIsNone(messages[0].semantic_content)
                connection = sqlite3.connect(store.path)
                try:
                    version = connection.execute(
                        "PRAGMA user_version"
                    ).fetchone()[0]
                finally:
                    connection.close()
                self.assertEqual(6, version)

        asyncio.run(scenario())

    def test_media_semantics_survive_weflow_confirmation_and_rebuild_context(self):
        async def scenario():
            with tempfile.TemporaryDirectory(prefix="akasha-media-memory-") as temp:
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
                await store.upsert_messages(
                    contact.id,
                    [
                        MemoryMessage(
                            "raw:image-1",
                            10,
                            "in",
                            "[图片]",
                            semantic_content="[图片: 一只白猫坐在窗边]",
                            origin="bridge",
                            pending=True,
                        )
                    ],
                )
                await store.upsert_messages(
                    contact.id,
                    [
                        MemoryMessage(
                            "raw:image-1",
                            10,
                            "in",
                            "[图片]",
                            message_type="3",
                            origin="weflow",
                        )
                    ],
                )
                await store.upsert_messages(
                    contact.id,
                    [
                        MemoryMessage(
                            "raw:video-1",
                            20,
                            "in",
                            "[视频]",
                            message_type="43",
                            origin="weflow",
                        )
                    ],
                )
                await store.upsert_messages(
                    contact.id,
                    [
                        MemoryMessage(
                            "raw:video-1",
                            20,
                            "in",
                            "[视频]",
                            semantic_content="[视频: 一个人在海边挥手]",
                            origin="bridge",
                            pending=True,
                        )
                    ],
                )

                messages = await store.recent_messages(contact.id)
                self.assertEqual(["[图片]", "[视频]"], [item.content for item in messages])
                self.assertEqual(
                    ["[图片: 一只白猫坐在窗边]", "[视频: 一个人在海边挥手]"],
                    [item.effective_content for item in messages],
                )
                self.assertTrue(all(item.origin == "weflow" for item in messages))
                self.assertTrue(all(not item.pending for item in messages))

                bundle = await ContextBuilder(store).build(contact.id)
                seeded = "\n".join(str(item["content"]) for item in bundle.items)
                self.assertIn("一只白猫坐在窗边", seeded)
                self.assertIn("一个人在海边挥手", seeded)

        asyncio.run(scenario())

    def test_same_nickname_contacts_never_share_messages(self):
        async def scenario():
            with tempfile.TemporaryDirectory(prefix="akasha-memory-store-") as temp:
                root = pathlib.Path(temp)
                secrets = SecretManager(root)
                store = MemoryStore(root)
                await store.initialize()
                contacts = []
                for session in ("session-a", "session-b"):
                    contacts.append(
                        await store.ensure_contact(
                            contact_hmac=secrets.contact_hmac("account", session),
                            account_enc=secrets.encrypt_text("account"),
                            session_enc=secrets.encrypt_text(session),
                            routing_name="相同昵称",
                        )
                    )
                await store.upsert_messages(
                    contacts[0].id,
                    [MemoryMessage("raw:1", 1, "in", "only contact A")],
                )
                await store.upsert_messages(
                    contacts[1].id,
                    [MemoryMessage("raw:1", 1, "in", "only contact B")],
                )
                bundle = await ContextBuilder(store).build(contacts[0].id)
                context = "\n".join(str(item["content"]) for item in bundle.items)
                self.assertIn("only contact A", context)
                self.assertNotIn("only contact B", context)

        asyncio.run(scenario())

    def test_authoritative_weflow_record_reconciles_generated_output(self):
        async def scenario():
            with tempfile.TemporaryDirectory(prefix="akasha-memory-reconcile-") as temp:
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
                    response_id="resp-1",
                    content="same reply",
                    source_time=100,
                )
                await store.upsert_messages(
                    contact.id,
                    [
                        MemoryMessage(
                            "raw:server-1",
                            101,
                            "out",
                            "same reply",
                            origin="weflow",
                        )
                    ],
                )
                messages = await store.recent_messages(contact.id)
                self.assertEqual(1, len(messages))
                self.assertEqual("raw:server-1", messages[0].source_uid)
                self.assertFalse(messages[0].pending)

        asyncio.run(scenario())

    def test_forget_tombstone_blocks_lazy_history_restore(self):
        async def scenario():
            with tempfile.TemporaryDirectory(prefix="akasha-memory-forget-") as temp:
                root = pathlib.Path(temp)
                secrets = SecretManager(root)
                store = MemoryStore(root)
                await store.initialize()
                contact = await store.ensure_contact(
                    contact_hmac=secrets.contact_hmac("account", "session"),
                    account_enc=secrets.encrypt_text("account"),
                    session_enc=secrets.encrypt_text("session"),
                    routing_name="旧昵称",
                )
                contact = await store.ensure_contact(
                    contact_hmac=contact.contact_hmac,
                    account_enc=secrets.encrypt_text("account"),
                    session_enc=secrets.encrypt_text("session"),
                    routing_name="新昵称",
                )
                self.assertEqual(("旧昵称", "新昵称"), contact.aliases)
                await store.tombstone_contact(contact.id)
                await store.forget_contact(contact.id)
                restored = await store.ensure_contact(
                    contact_hmac=contact.contact_hmac,
                    account_enc=secrets.encrypt_text("account"),
                    session_enc=secrets.encrypt_text("session"),
                    routing_name="又一次昵称",
                )
                changed = await store.upsert_messages(
                    contact.id,
                    [MemoryMessage("raw:late", 2, "in", "must stay deleted")],
                )
                self.assertIsNotNone(restored.tombstoned_at)
                self.assertEqual((), restored.aliases)
                self.assertEqual(0, changed)
                self.assertEqual([], await store.recent_messages(contact.id))
                connection = sqlite3.connect(store.path)
                try:
                    row = connection.execute(
                        """
                        SELECT account_enc, session_enc, routing_name
                        FROM contacts WHERE id = ?
                        """,
                        (contact.id,),
                    ).fetchone()
                finally:
                    connection.close()
                self.assertEqual(("", "", ""), row)

        asyncio.run(scenario())

    def test_forget_without_qwen_client_preserves_cloud_deletion_ids(self):
        fake_aiohttp = types.ModuleType("aiohttp")
        original_aiohttp = sys.modules.get("aiohttp")
        sys.modules["aiohttp"] = fake_aiohttp
        try:
            from akasha_memory.runtime import ContactMemoryRuntime
        finally:
            if original_aiohttp is None:
                sys.modules.pop("aiohttp", None)
            else:
                sys.modules["aiohttp"] = original_aiohttp

        class FakeSync:
            @asynccontextmanager
            async def exclusive_contact(self, _contact_id):
                yield

            async def close(self):
                return None

        class FakeEvent:
            unified_msg_origin = "aiocqhttp:FriendMessage:123"
            message_obj = types.SimpleNamespace(
                raw_message={
                    "akasha_schema": 1,
                    "type": "private",
                    "account": "account",
                    "session": "session",
                    "routing_name": "contact",
                    "source_messages": [],
                }
            )

            def is_private_chat(self):
                return True

        async def scenario():
            with tempfile.TemporaryDirectory(prefix="akasha-memory-cloud-forget-") as temp:
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
                await store.upsert_messages(
                    contact.id,
                    [MemoryMessage("raw:keep", 1, "in", "keep until cloud deletion")],
                )
                await store.create_qwen_session(
                    contact.id,
                    conversation_id="cloud-conversation",
                    model="qwen3.7-max",
                    persona_hash="persona",
                    tool_hash="tools",
                    created_at=1,
                    expires_at=time.time() + 3600,
                    estimated_tokens=100,
                    expected_memory_revision=(
                        await store.contact_memory_revision(contact.id)
                    ),
                )
                runtime = ContactMemoryRuntime(
                    mode="off",
                    secret_manager=secrets,
                    store=store,
                    synchronizer=FakeSync(),
                    context_builder=ContextBuilder(store),
                    qwen_sessions=None,
                )

                self.assertEqual((False, 1), await runtime.forget(FakeEvent()))
                self.assertTrue(await store.is_contact_tombstoned(contact.id))
                self.assertEqual(
                    ["cloud-conversation"],
                    await store.list_contact_conversations(contact.id),
                )
                connection = sqlite3.connect(store.path)
                try:
                    message_count = connection.execute(
                        "SELECT COUNT(*) FROM messages WHERE contact_id = ?",
                        (contact.id,),
                    ).fetchone()[0]
                finally:
                    connection.close()
                self.assertEqual(1, message_count)

        asyncio.run(scenario())

    def test_bridge_send_failure_immediately_dirties_contact_session(self):
        fake_aiohttp = types.ModuleType("aiohttp")
        original_aiohttp = sys.modules.get("aiohttp")
        sys.modules["aiohttp"] = fake_aiohttp
        try:
            from akasha_memory.runtime import ContactMemoryRuntime
        finally:
            if original_aiohttp is None:
                sys.modules.pop("aiohttp", None)
            else:
                sys.modules["aiohttp"] = original_aiohttp

        class FakeSync:
            @asynccontextmanager
            async def exclusive_contact(self, _contact_id):
                yield

            async def close(self):
                return None

        class FakeEvent:
            unified_msg_origin = "aiocqhttp:FriendMessage:123"
            message_obj = types.SimpleNamespace(
                raw_message={
                    "akasha_schema": 1,
                    "type": "private",
                    "account": "account",
                    "session": "session",
                    "routing_name": "contact",
                    "source_messages": [],
                    "notice_type": "akasha_send_result",
                    "success": False,
                }
            )

            def is_private_chat(self):
                return True

        async def scenario():
            with tempfile.TemporaryDirectory(prefix="akasha-memory-send-failure-") as temp:
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
                session = await store.create_qwen_session(
                    contact.id,
                    conversation_id="cloud-conversation",
                    model="qwen3.7-max",
                    persona_hash="persona",
                    tool_hash="tools",
                    created_at=1,
                    expires_at=time.time() + 3600,
                    estimated_tokens=100,
                    expected_memory_revision=0,
                )
                session = await store.activate_qwen_session(
                    session.id,
                    expected_memory_revision=0,
                )
                await store.archive_generated(
                    contact.id,
                    response_id="unsent-response",
                    content="must be discarded",
                )
                self.assertFalse(session.dirty)
                runtime = ContactMemoryRuntime(
                    mode="active",
                    secret_manager=secrets,
                    store=store,
                    synchronizer=FakeSync(),
                    context_builder=ContextBuilder(store),
                    qwen_sessions=None,
                )

                self.assertTrue(await runtime.record_send_failure(FakeEvent()))
                current = await store.active_qwen_session(contact.id)
                self.assertIsNotNone(current)
                self.assertTrue(current.dirty)
                self.assertEqual(1, await store.contact_memory_revision(contact.id))
                connection = sqlite3.connect(store.path)
                try:
                    pending_count = connection.execute(
                        """
                        SELECT COUNT(*)
                        FROM messages
                        WHERE contact_id = ?
                          AND origin = 'generated'
                          AND pending = 1
                        """,
                        (contact.id,),
                    ).fetchone()[0]
                finally:
                    connection.close()
                self.assertEqual(0, pending_count)

        asyncio.run(scenario())

    def test_external_weflow_turn_invalidates_but_current_bridge_echo_does_not(self):
        async def scenario():
            with tempfile.TemporaryDirectory(prefix="akasha-memory-external-turn-") as temp:
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
                session = await store.create_qwen_session(
                    contact.id,
                    conversation_id="cloud-conversation",
                    model="qwen3.7-max",
                    persona_hash="persona",
                    tool_hash="tools",
                    created_at=1,
                    expires_at=time.time() + 3600,
                    estimated_tokens=100,
                    expected_memory_revision=0,
                )
                session = await store.activate_qwen_session(
                    session.id,
                    expected_memory_revision=0,
                )
                await store.upsert_messages(
                    contact.id,
                    [
                        MemoryMessage(
                            "bridge:current",
                            100,
                            "in",
                            "current prompt",
                            origin="bridge",
                            pending=True,
                        )
                    ],
                )
                await store.upsert_messages(
                    contact.id,
                    [
                        MemoryMessage(
                            "raw:current",
                            101,
                            "in",
                            "current prompt",
                            origin="weflow",
                        )
                    ],
                )
                current = await store.active_qwen_session(contact.id)
                self.assertFalse(current.dirty)
                self.assertEqual(0, await store.contact_memory_revision(contact.id))

                await store.upsert_messages(
                    contact.id,
                    [
                        MemoryMessage(
                            "raw:manual",
                            102,
                            "out",
                            "manual operator message",
                            origin="weflow",
                        )
                    ],
                )
                current = await store.active_qwen_session(contact.id)
                self.assertTrue(current.dirty)
                self.assertEqual(1, await store.contact_memory_revision(contact.id))

        asyncio.run(scenario())

    def test_split_weflow_output_confirms_pending_generation(self):
        async def scenario():
            with tempfile.TemporaryDirectory(prefix="akasha-memory-split-") as temp:
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
                    response_id="resp-split",
                    content="第一句。 第二句。",
                    source_time=100,
                )
                session = await store.create_qwen_session(
                    contact.id,
                    conversation_id="split-conversation",
                    model="qwen3.7-max",
                    persona_hash="persona",
                    tool_hash="tools",
                    created_at=100,
                    expires_at=time.time() + 3600,
                    estimated_tokens=100,
                    expected_memory_revision=0,
                )
                await store.activate_qwen_session(
                    session.id,
                    expected_memory_revision=0,
                )
                self.assertEqual([], await store.recent_messages(contact.id))
                await store.upsert_messages(
                    contact.id,
                    [
                        MemoryMessage(
                            "raw:part-1",
                            101,
                            "out",
                            "第一句。",
                            origin="weflow",
                        ),
                        MemoryMessage(
                            "raw:part-2",
                            102,
                            "out",
                            "第二句。",
                            origin="weflow",
                        ),
                    ],
                )
                resolved, dirty = await store.reconcile_pending_outputs(contact.id)
                self.assertEqual(1, resolved)
                self.assertFalse(dirty)
                self.assertEqual(0, await store.contact_memory_revision(contact.id))
                self.assertFalse(
                    (await store.active_qwen_session(contact.id)).dirty
                )
                self.assertEqual(
                    ["raw:part-1", "raw:part-2"],
                    [
                        message.source_uid
                        for message in await store.recent_messages(contact.id)
                    ],
                )

        asyncio.run(scenario())

    def test_retrieval_uses_message_time_not_insert_order(self):
        async def scenario():
            with tempfile.TemporaryDirectory(prefix="akasha-memory-order-") as temp:
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
                await store.upsert_messages(
                    contact.id,
                    [MemoryMessage("raw:recent", 200, "in", "recent")],
                )
                # Background backfill inserts this older record later, so its
                # SQLite id is greater even though its source time is earlier.
                await store.upsert_messages(
                    contact.id,
                    [MemoryMessage("raw:old", 100, "in", "older project detail")],
                )
                older = await store.relevant_older_messages(
                    contact.id,
                    terms=("project",),
                    before_source_time=200,
                    before_source_uid="raw:recent",
                )
                self.assertEqual(["raw:old"], [item.source_uid for item in older])

        asyncio.run(scenario())

    def test_request_cache_revalidates_identity_not_shared_umo(self):
        fake_aiohttp = types.ModuleType("aiohttp")
        original_aiohttp = sys.modules.get("aiohttp")
        sys.modules["aiohttp"] = fake_aiohttp
        try:
            from akasha_memory.models import SyncResult
            from akasha_memory.runtime import ContactMemoryRuntime
        finally:
            if original_aiohttp is None:
                sys.modules.pop("aiohttp", None)
            else:
                sys.modules["aiohttp"] = original_aiohttp

        class FakeSync:
            async def sync_contact(self, _contact_id, _binding):
                return SyncResult()

            async def close(self):
                return None

        class FakeEvent:
            def __init__(self, account, session):
                self.unified_msg_origin = "same-platform-umo"
                self.message_obj = types.SimpleNamespace(
                    raw_message={
                        "akasha_schema": 1,
                        "type": "private",
                        "account": account,
                        "session": session,
                        "routing_name": "same nickname",
                        "source_messages": [],
                    }
                )

            def is_private_chat(self):
                return True

        async def scenario():
            with tempfile.TemporaryDirectory(prefix="akasha-memory-request-") as temp:
                root = pathlib.Path(temp)
                store = MemoryStore(root)
                await store.initialize()
                runtime = ContactMemoryRuntime(
                    mode="shadow",
                    secret_manager=SecretManager(root),
                    store=store,
                    synchronizer=FakeSync(),
                    context_builder=ContextBuilder(store),
                    qwen_sessions=None,
                )
                event_a = FakeEvent("account-a", "session-a")
                event_b = FakeEvent("account-b", "session-b")
                prepared_a, _ = await runtime.prepare_request(event_a, "A")
                prepared_b, _ = await runtime.prepare_request(event_b, "B")
                self.assertIsNotNone(prepared_a)
                self.assertIsNotNone(prepared_b)
                self.assertNotEqual(
                    prepared_a.request_key,
                    prepared_b.request_key,
                )
                self.assertEqual(
                    "session-a",
                    runtime.prepared_for(prepared_a.request_key).binding.session,
                )
                self.assertEqual(
                    "session-b",
                    runtime.prepared_for(prepared_b.request_key).binding.session,
                )
                await store.upsert_messages(
                    prepared_a.contact.id,
                    [
                        MemoryMessage(
                            "raw:confirmed",
                            1,
                            "in",
                            "confirmed history",
                            origin="weflow",
                        )
                    ],
                )
                fallback = await runtime.fallback_contexts(
                    prepared_a.request_key,
                    current_prompt="new prompt",
                )
                self.assertTrue(fallback)
                self.assertTrue(all(item.get("_no_save") is True for item in fallback))
                self.assertTrue(all(item.get("role") != "system" for item in fallback))
                self.assertIn(
                    "不可信历史数据",
                    runtime.fallback_system_prompt("persona"),
                )
                self.assertIsNone(
                    await runtime.validate_request(event_b, prepared_a.request_key)
                )

        asyncio.run(scenario())

    def test_qwen_tool_chain_ownership_prevents_contact_interleaving(self):
        fake_client_module = types.ModuleType("akasha_memory.qwen_client")
        fake_client_module.QwenClient = object
        original = sys.modules.get("akasha_memory.qwen_client")
        sys.modules["akasha_memory.qwen_client"] = fake_client_module
        try:
            from akasha_memory.qwen_session import (
                QwenSessionManager,
                StaleToolContinuation,
            )
        finally:
            if original is None:
                sys.modules.pop("akasha_memory.qwen_client", None)
            else:
                sys.modules["akasha_memory.qwen_client"] = original

        class FakeClient:
            def __init__(self):
                self.created = []
                self.calls = []

            async def create_conversation(self, *, metadata=None):
                conversation_id = f"conv-{len(self.created) + 1}"
                self.created.append(conversation_id)
                return conversation_id

            async def add_items(self, conversation_id, items):
                return []

            async def respond(
                self,
                *,
                conversation_id,
                model,
                prompt,
                input_items=None,
                tools=None,
                tool_choice="auto",
                request_max_retries=None,
            ):
                self.calls.append((conversation_id, prompt, input_items))
                if input_items is not None:
                    return QwenResult(
                        response_id=f"resp-{len(self.calls)}",
                        text="tool complete",
                    )
                if prompt in {"request A", "request C"}:
                    return QwenResult(
                        response_id=f"resp-{len(self.calls)}",
                        text="",
                        tool_calls=(
                            QwenToolCall(
                                call_id=f"call-{prompt[-1]}",
                                name="weather",
                                arguments={"city": "杭州"},
                            ),
                        ),
                    )
                return QwenResult(
                    response_id=f"resp-{len(self.calls)}",
                    text="plain complete",
                )

            async def delete_conversation_fully(self, conversation_id):
                return None

        async def scenario():
            with tempfile.TemporaryDirectory(prefix="akasha-memory-tool-owner-") as temp:
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
                client = FakeClient()
                manager = QwenSessionManager(
                    store=store,
                    context_builder=ContextBuilder(store),
                    client=client,
                )
                tools = [
                    {
                        "type": "function",
                        "name": "weather",
                        "description": "weather",
                        "parameters": {"type": "object", "properties": {}},
                    }
                ]
                await manager.respond(
                    contact,
                    prompt="request A",
                    system_prompt="persona",
                    request_key="owner-a",
                    tool_fingerprint="schema",
                    tools=tools,
                )
                pending = await store.active_qwen_session(contact.id)
                self.assertEqual("owner-a", pending.pending_owner)
                self.assertEqual(("call-A",), pending.pending_call_ids)

                await manager.respond(
                    contact,
                    prompt="request B",
                    system_prompt="persona",
                    request_key="owner-b",
                    tool_fingerprint="schema",
                    tools=tools,
                )
                current = await store.active_qwen_session(contact.id)
                self.assertEqual("conv-2", current.conversation_id)
                self.assertFalse(current.dirty)
                self.assertEqual("", current.pending_owner)

                with self.assertRaises(StaleToolContinuation):
                    await manager.respond(
                        contact,
                        system_prompt="persona",
                        request_key="owner-a",
                        tool_fingerprint="schema",
                        input_items=[
                            {
                                "type": "function_call_output",
                                "call_id": "call-A",
                                "output": "late",
                            }
                        ],
                        tools=tools,
                    )
                current = await store.active_qwen_session(contact.id)
                self.assertEqual("conv-2", current.conversation_id)
                self.assertFalse(current.dirty)

                await manager.respond(
                    contact,
                    prompt="request C",
                    system_prompt="persona",
                    request_key="owner-c",
                    tool_fingerprint="schema",
                    tools=tools,
                )
                pending = await store.active_qwen_session(contact.id)
                self.assertEqual("owner-c", pending.pending_owner)
                await manager.respond(
                    contact,
                    system_prompt="persona",
                    request_key="owner-c",
                    tool_fingerprint="schema",
                    input_items=[
                        {
                            "type": "function_call_output",
                            "call_id": "call-C",
                            "output": "sunny",
                        }
                    ],
                    tools=tools,
                )
                current = await store.active_qwen_session(contact.id)
                self.assertEqual("conv-2", current.conversation_id)
                self.assertEqual("", current.pending_owner)
                self.assertFalse(current.dirty)

                await manager.archive_fallback_output(
                    contact.id,
                    "fallback reply A",
                )
                current = await store.active_qwen_session(contact.id)
                self.assertEqual("conv-2", current.conversation_id)
                self.assertTrue(current.dirty)

                await manager.respond(
                    contact,
                    prompt="request D",
                    system_prompt="persona",
                    request_key="owner-d",
                    tool_fingerprint="schema",
                    tools=tools,
                )
                current = await store.active_qwen_session(contact.id)
                self.assertEqual("conv-3", current.conversation_id)
                self.assertFalse(current.dirty)

                await store.upsert_messages(
                    contact.id,
                    [
                        MemoryMessage(
                            "raw:fallback-part-1",
                            time.time(),
                            "out",
                            "fallback ",
                            origin="weflow",
                        ),
                        MemoryMessage(
                            "raw:fallback-part-2",
                            time.time() + 1,
                            "out",
                            "reply A",
                            origin="weflow",
                        ),
                    ],
                )
                resolved, diverged = await store.reconcile_pending_outputs(
                    contact.id
                )
                self.assertEqual(1, resolved)
                self.assertFalse(diverged)
                current = await store.active_qwen_session(contact.id)
                self.assertEqual("conv-3", current.conversation_id)
                self.assertTrue(current.dirty)

        asyncio.run(scenario())

    def test_cancelled_qwen_seed_remains_dirty_and_is_not_reused(self):
        fake_client_module = types.ModuleType("akasha_memory.qwen_client")
        fake_client_module.QwenClient = object
        original = sys.modules.get("akasha_memory.qwen_client")
        sys.modules["akasha_memory.qwen_client"] = fake_client_module
        try:
            from akasha_memory.qwen_session import QwenSessionManager
        finally:
            if original is None:
                sys.modules.pop("akasha_memory.qwen_client", None)
            else:
                sys.modules["akasha_memory.qwen_client"] = original

        class FakeContextBuilder:
            seed_max_tokens = 10000

            async def build(self, *_args, **_kwargs):
                return types.SimpleNamespace(
                    items=[
                        {"role": "user", "content": f"history-{index}"}
                        for index in range(21)
                    ],
                    estimated_tokens=210,
                )

        class FakeClient:
            def __init__(self):
                self.created = []
                self.seed_calls = []
                self.responses = []

            async def create_conversation(self, *, metadata=None):
                conversation_id = f"conv-{len(self.created) + 1}"
                self.created.append(conversation_id)
                return conversation_id

            async def add_items(self, conversation_id, items):
                self.seed_calls.append((conversation_id, len(items)))
                calls_for_conversation = sum(
                    1 for value, _ in self.seed_calls if value == conversation_id
                )
                if conversation_id == "conv-1" and calls_for_conversation == 2:
                    raise asyncio.CancelledError()
                return [
                    f"{conversation_id}-item-{calls_for_conversation}-{index}"
                    for index in range(len(items))
                ]

            async def respond(
                self,
                *,
                conversation_id,
                model,
                prompt,
                input_items=None,
                tools=None,
                tool_choice="auto",
                request_max_retries=None,
            ):
                self.responses.append(conversation_id)
                return QwenResult(response_id="response", text="ok")

            async def delete_conversation_fully(self, conversation_id):
                return None

        async def scenario():
            with tempfile.TemporaryDirectory(prefix="akasha-memory-cancel-seed-") as temp:
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
                client = FakeClient()
                manager = QwenSessionManager(
                    store=store,
                    context_builder=FakeContextBuilder(),
                    client=client,
                )

                with self.assertRaises(asyncio.CancelledError):
                    await manager.respond(
                        contact,
                        prompt="first",
                        system_prompt="persona",
                        request_key="cancelled-request",
                    )

                incomplete = await store.active_qwen_session(contact.id)
                self.assertIsNotNone(incomplete)
                self.assertEqual("conv-1", incomplete.conversation_id)
                self.assertTrue(incomplete.dirty)

                result = await manager.respond(
                    contact,
                    prompt="second",
                    system_prompt="persona",
                    request_key="replacement-request",
                )
                self.assertEqual("ok", result.text)
                self.assertEqual(["conv-1", "conv-2"], client.created)
                self.assertEqual(["conv-2"], client.responses)
                active = await store.active_qwen_session(contact.id)
                self.assertIsNotNone(active)
                self.assertEqual("conv-2", active.conversation_id)
                self.assertFalse(active.dirty)

        asyncio.run(scenario())

    def test_cancelled_qwen_response_commit_is_dirtied_and_not_reused(self):
        fake_client_module = types.ModuleType("akasha_memory.qwen_client")
        fake_client_module.QwenClient = object
        original = sys.modules.get("akasha_memory.qwen_client")
        sys.modules["akasha_memory.qwen_client"] = fake_client_module
        try:
            from akasha_memory.qwen_session import QwenSessionManager
        finally:
            if original is None:
                sys.modules.pop("akasha_memory.qwen_client", None)
            else:
                sys.modules["akasha_memory.qwen_client"] = original

        class FakeClient:
            def __init__(self):
                self.created = []
                self.responses = []

            async def create_conversation(self, *, metadata=None):
                conversation_id = f"conv-{len(self.created) + 1}"
                self.created.append(conversation_id)
                return conversation_id

            async def add_items(self, conversation_id, items):
                return []

            async def respond(
                self,
                *,
                conversation_id,
                model,
                prompt,
                input_items=None,
                tools=None,
                tool_choice="auto",
                request_max_retries=None,
            ):
                self.responses.append(conversation_id)
                return QwenResult(
                    response_id=f"response-{len(self.responses)}",
                    text=f"answer-{len(self.responses)}",
                )

            async def delete_conversation_fully(self, conversation_id):
                return None

        async def scenario():
            with tempfile.TemporaryDirectory(prefix="akasha-memory-cancel-response-") as temp:
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
                client = FakeClient()
                manager = QwenSessionManager(
                    store=store,
                    context_builder=ContextBuilder(store),
                    client=client,
                )
                update_usage = store.update_qwen_session_usage
                cancel_first_commit = True

                async def update_then_cancel(*args, **kwargs):
                    nonlocal cancel_first_commit
                    await update_usage(*args, **kwargs)
                    if cancel_first_commit:
                        cancel_first_commit = False
                        raise asyncio.CancelledError()

                store.update_qwen_session_usage = update_then_cancel
                with self.assertRaises(asyncio.CancelledError):
                    await manager.respond(
                        contact,
                        prompt="first",
                        system_prompt="persona",
                        request_key="cancelled-response",
                    )

                incomplete = await store.active_qwen_session(contact.id)
                self.assertIsNotNone(incomplete)
                self.assertEqual("conv-1", incomplete.conversation_id)
                self.assertTrue(incomplete.dirty)

                result = await manager.respond(
                    contact,
                    prompt="second",
                    system_prompt="persona",
                    request_key="replacement-response",
                )
                self.assertEqual("answer-2", result.text)
                self.assertEqual(["conv-1", "conv-2"], client.created)
                self.assertEqual(["conv-1", "conv-2"], client.responses)
                active = await store.active_qwen_session(contact.id)
                self.assertIsNotNone(active)
                self.assertEqual("conv-2", active.conversation_id)
                self.assertFalse(active.dirty)

        asyncio.run(scenario())

    def test_qwen_session_creation_retries_if_fallback_confirms_after_seed(self):
        fake_client_module = types.ModuleType("akasha_memory.qwen_client")
        fake_client_module.QwenClient = object
        original = sys.modules.get("akasha_memory.qwen_client")
        sys.modules["akasha_memory.qwen_client"] = fake_client_module
        try:
            from akasha_memory.qwen_session import QwenSessionManager
        finally:
            if original is None:
                sys.modules.pop("akasha_memory.qwen_client", None)
            else:
                sys.modules["akasha_memory.qwen_client"] = original

        class FakeClient:
            def __init__(self):
                self.created = []
                self.deleted = []
                self.seeded = []
                self.responses = []

            async def create_conversation(self, *, metadata=None):
                conversation_id = f"conv-{len(self.created) + 1}"
                self.created.append(conversation_id)
                return conversation_id

            async def add_items(self, conversation_id, items):
                self.seeded.append((conversation_id, list(items)))
                return []

            async def respond(
                self,
                *,
                conversation_id,
                model,
                prompt,
                input_items=None,
                tools=None,
                tool_choice="auto",
                request_max_retries=None,
            ):
                self.responses.append(conversation_id)
                return QwenResult(
                    response_id="resp-after-retry",
                    text="new reply",
                )

            async def delete_conversation_fully(self, conversation_id):
                self.deleted.append(conversation_id)

        async def scenario():
            with tempfile.TemporaryDirectory(prefix="akasha-memory-revision-") as temp:
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
                client = FakeClient()
                manager = QwenSessionManager(
                    store=store,
                    context_builder=ContextBuilder(store),
                    client=client,
                )
                await manager.archive_fallback_output(
                    contact.id,
                    "fallback confirmed turn",
                )
                first_insert = True
                create_qwen_session = store.create_qwen_session

                async def confirm_between_seed_and_insert(*args, **kwargs):
                    nonlocal first_insert
                    if first_insert:
                        first_insert = False
                        await store.upsert_messages(
                            contact.id,
                            [
                                MemoryMessage(
                                    "raw:fallback-confirmed",
                                    time.time(),
                                    "out",
                                    "fallback confirmed turn",
                                    origin="weflow",
                                )
                            ],
                        )
                    return await create_qwen_session(*args, **kwargs)

                store.create_qwen_session = confirm_between_seed_and_insert
                await manager.respond(
                    contact,
                    prompt="new prompt",
                    system_prompt="persona",
                    request_key="request-after-fallback",
                )

                self.assertEqual(["conv-1", "conv-2"], client.created)
                self.assertEqual(["conv-1"], client.deleted)
                self.assertEqual(["conv-2"], client.responses)
                self.assertEqual(["conv-2"], [item[0] for item in client.seeded])
                seeded_text = "\n".join(
                    str(item.get("content", ""))
                    for _, items in client.seeded
                    for item in items
                )
                self.assertIn("fallback confirmed turn", seeded_text)
                session = await store.active_qwen_session(contact.id)
                self.assertIsNotNone(session)
                self.assertEqual("conv-2", session.conversation_id)
                self.assertFalse(session.dirty)
                self.assertEqual(
                    await store.contact_memory_revision(contact.id),
                    session.memory_revision,
                )

        asyncio.run(scenario())

    def test_qwen_responses_tool_payload_and_parse(self):
        original_aiohttp = sys.modules.get("aiohttp")
        sys.modules["aiohttp"] = types.ModuleType("aiohttp")
        try:
            from akasha_memory.qwen_client import QwenClient
        finally:
            if original_aiohttp is None:
                sys.modules.pop("aiohttp", None)
            else:
                sys.modules["aiohttp"] = original_aiohttp

        class FakeClient(QwenClient):
            def __init__(self):
                super().__init__(
                    base_url="https://example.invalid/compatible-mode/v1",
                    api_key="test",
                )
                self.body = None
                self.max_attempts = None

            async def _request(self, method, path, **kwargs):
                self.body = kwargs["body"]
                self.max_attempts = kwargs.get("max_attempts")
                return {
                    "id": "resp-tool",
                    "status": "completed",
                    "output": [
                        {
                            "type": "function_call",
                            "call_id": "call-1",
                            "name": "weather",
                            "arguments": '{"city":"杭州"}',
                        }
                    ],
                    "usage": {
                        "input_tokens": 20,
                        "output_tokens": 5,
                        "input_tokens_details": {"cached_tokens": 12},
                    },
                }

        async def scenario():
            client = FakeClient()
            tool = {
                "type": "function",
                "name": "weather",
                "description": "weather",
                "parameters": {"type": "object", "properties": {}},
            }
            result = await client.respond(
                conversation_id="conv-1",
                model="qwen3.7-max",
                prompt="weather?",
                tools=[tool],
                tool_choice="required",
                request_max_retries=2,
            )
            self.assertEqual([tool], client.body["tools"])
            self.assertEqual("required", client.body["tool_choice"])
            self.assertEqual("call-1", result.tool_calls[0].call_id)
            self.assertEqual({"city": "杭州"}, result.tool_calls[0].arguments)
            self.assertEqual(12, result.cached_tokens)
            self.assertEqual(2, client.max_attempts)
            with self.assertRaises(ValueError):
                await client.respond(
                    conversation_id="conv-1",
                    model="qwen3.7-max",
                    prompt="weather?",
                    tools=[tool, {**tool, "name": "weather_2"}],
                    tool_choice="required",
                )

        asyncio.run(scenario())

    def test_switching_contacts_reuses_each_contacts_cloud_conversation(self):
        fake_client_module = types.ModuleType("akasha_memory.qwen_client")
        fake_client_module.QwenClient = object
        original = sys.modules.get("akasha_memory.qwen_client")
        sys.modules["akasha_memory.qwen_client"] = fake_client_module
        try:
            from akasha_memory.qwen_session import QwenSessionManager
        finally:
            if original is None:
                sys.modules.pop("akasha_memory.qwen_client", None)
            else:
                sys.modules["akasha_memory.qwen_client"] = original

        class FakeClient:
            def __init__(self):
                self.created = []
                self.responses = []

            async def create_conversation(self, *, metadata=None):
                conversation_id = f"conv-{len(self.created) + 1}"
                self.created.append(conversation_id)
                return conversation_id

            async def add_items(self, conversation_id, items):
                return [f"item-{conversation_id}-{index}" for index, _ in enumerate(items)]

            async def respond(
                self,
                *,
                conversation_id,
                model,
                prompt,
                input_items=None,
                tools=None,
                tool_choice="auto",
                request_max_retries=None,
            ):
                self.responses.append(conversation_id)
                return QwenResult(
                    response_id=f"resp-{len(self.responses)}",
                    text=f"reply:{prompt}",
                    input_tokens=100,
                    output_tokens=10,
                )

            async def delete_conversation_fully(self, conversation_id):
                return None

        async def scenario():
            with tempfile.TemporaryDirectory(prefix="akasha-memory-session-") as temp:
                root = pathlib.Path(temp)
                secrets = SecretManager(root)
                store = MemoryStore(root)
                await store.initialize()
                contacts = []
                for session in ("session-a", "session-b"):
                    contacts.append(
                        await store.ensure_contact(
                            contact_hmac=secrets.contact_hmac("account", session),
                            account_enc=secrets.encrypt_text("account"),
                            session_enc=secrets.encrypt_text(session),
                            routing_name=session,
                        )
                    )
                client = FakeClient()
                manager = QwenSessionManager(
                    store=store,
                    context_builder=ContextBuilder(store),
                    client=client,
                )
                await manager.respond(
                    contacts[0],
                    prompt="A1",
                    system_prompt="persona",
                    request_key="request-a1",
                )
                await manager.respond(
                    contacts[1],
                    prompt="B1",
                    system_prompt="persona",
                    request_key="request-b1",
                )
                await manager.respond(
                    contacts[0],
                    prompt="A2",
                    system_prompt="persona",
                    request_key="request-a2",
                )
                self.assertEqual(["conv-1", "conv-2"], client.created)
                self.assertEqual(["conv-1", "conv-2", "conv-1"], client.responses)

        asyncio.run(scenario())

    def test_weflow_sync_rejects_account_and_talker_mismatch(self):
        original_aiohttp = sys.modules.get("aiohttp")
        sys.modules["aiohttp"] = types.ModuleType("aiohttp")
        try:
            from akasha_memory.weflow_sync import (
                WeFlowConfigurationError,
                WeFlowSync,
            )
        finally:
            if original_aiohttp is None:
                sys.modules.pop("aiohttp", None)
            else:
                sys.modules["aiohttp"] = original_aiohttp

        class FakeResponse:
            status = 200

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def json(self, **_kwargs):
                return {
                    "success": True,
                    "talker": "wrong-session",
                    "messages": [],
                }

        class FakeSession:
            closed = False

            def get(self, *_args, **_kwargs):
                return FakeResponse()

        async def scenario():
            with tempfile.TemporaryDirectory(prefix="akasha-memory-weflow-") as temp:
                root = pathlib.Path(temp)
                config_path = root / "bridge.json"
                config_path.write_text(
                    json.dumps(
                        {
                            "weflow_base_url": "http://127.0.0.1:5031",
                            "access_token": "test-only",
                            "bot_wxid": "account-a",
                        }
                    ),
                    encoding="utf-8",
                )
                store = MemoryStore(root)
                await store.initialize()
                synchronizer = WeFlowSync(
                    store,
                    bridge_config_path=str(config_path),
                )
                with self.assertRaises(WeFlowConfigurationError):
                    synchronizer._load_endpoint("account-b")
                config_path.write_text(
                    json.dumps(
                        {
                            "weflow_base_url": "http://127.0.0.1:5031",
                            "access_token": "test-only",
                        }
                    ),
                    encoding="utf-8",
                )
                with self.assertRaises(WeFlowConfigurationError):
                    synchronizer._load_endpoint("")
                config_path.write_text(
                    json.dumps(
                        {
                            "weflow_base_url": "http://127.0.0.1:5031",
                            "access_token": "test-only",
                            "bot_wxid": "account-a",
                        }
                    ),
                    encoding="utf-8",
                )
                synchronizer._session = FakeSession()
                with self.assertRaises(RuntimeError):
                    await synchronizer._fetch_page(
                        talker="session-a",
                        limit=10,
                        expected_account="account-a",
                    )

        asyncio.run(scenario())

    def test_forget_exclusive_section_cancels_contact_backfill(self):
        original_aiohttp = sys.modules.get("aiohttp")
        sys.modules["aiohttp"] = types.ModuleType("aiohttp")
        try:
            from akasha_memory.weflow_sync import WeFlowSync
        finally:
            if original_aiohttp is None:
                sys.modules.pop("aiohttp", None)
            else:
                sys.modules["aiohttp"] = original_aiohttp

        async def scenario():
            with tempfile.TemporaryDirectory(prefix="akasha-memory-cancel-") as temp:
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
                synchronizer = WeFlowSync(store)
                started = asyncio.Event()
                fetch_count = 0

                async def blocked_fetch(**_kwargs):
                    nonlocal fetch_count
                    fetch_count += 1
                    started.set()
                    await asyncio.Event().wait()

                synchronizer._fetch_page = blocked_fetch
                self.assertTrue(
                    synchronizer._schedule_backfill(
                        contact.id,
                        talker="session",
                        account="account",
                        initial_offset=0,
                        end_time=1_700_000_000,
                    )
                )
                await asyncio.wait_for(started.wait(), timeout=1)
                async with synchronizer.exclusive_contact(contact.id):
                    await store.tombstone_contact(contact.id)
                    await store.forget_contact(contact.id)
                    queued_sync = asyncio.create_task(
                        synchronizer.sync_contact(
                            contact.id,
                            ContactBinding(
                                account="account",
                                session="session",
                                routing_name="contact",
                                unified_origin="umo",
                            ),
                        )
                    )
                    await asyncio.sleep(0)
                sync_result = await asyncio.wait_for(queued_sync, timeout=1)
                self.assertNotIn(contact.id, synchronizer._backfills)
                self.assertEqual([], await store.recent_messages(contact.id))
                self.assertEqual("tombstoned", sync_result.error_kind)
                self.assertEqual(1, fetch_count)

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
