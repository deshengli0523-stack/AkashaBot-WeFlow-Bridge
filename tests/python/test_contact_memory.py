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
                        "ALTER TABLE contacts DROP COLUMN next_seed_recent_limit"
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
                self.assertEqual(10, version)
                self.assertIn("memory_revision", contact_columns)
                self.assertIn("next_seed_recent_limit", contact_columns)
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
                    connection.execute(
                        "ALTER TABLE contacts DROP COLUMN next_seed_recent_limit"
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
                self.assertEqual(10, version)

        asyncio.run(scenario())

    def test_memory_schema_v6_adds_one_shot_seed_limit_with_zero_default(self):
        async def scenario():
            with tempfile.TemporaryDirectory(prefix="akasha-memory-v6-") as temp:
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
                connection = sqlite3.connect(store.path)
                try:
                    connection.execute(
                        "ALTER TABLE contacts DROP COLUMN next_seed_recent_limit"
                    )
                    connection.execute("PRAGMA user_version = 6")
                    connection.commit()
                finally:
                    connection.close()

                await store.initialize()
                revision, recent_limit = await store.contact_seed_state(contact.id)
                connection = sqlite3.connect(store.path)
                try:
                    version = connection.execute(
                        "PRAGMA user_version"
                    ).fetchone()[0]
                finally:
                    connection.close()
                self.assertEqual(10, version)
                self.assertEqual(0, revision)
                self.assertEqual(0, recent_limit)

        asyncio.run(scenario())

    def test_memory_schema_v7_adds_delivery_confirmation_state(self):
        async def scenario():
            with tempfile.TemporaryDirectory(
                prefix="akasha-memory-v7-"
            ) as temp:
                root = pathlib.Path(temp)
                store = MemoryStore(root)
                await store.initialize()
                connection = sqlite3.connect(store.path)
                try:
                    connection.execute("DROP TABLE message_delivery")
                    connection.execute("PRAGMA user_version = 7")
                    connection.commit()
                finally:
                    connection.close()

                await store.initialize()
                connection = sqlite3.connect(store.path)
                try:
                    version = connection.execute(
                        "PRAGMA user_version"
                    ).fetchone()[0]
                    delivery_table = connection.execute(
                        """
                        SELECT name
                        FROM sqlite_master
                        WHERE type = 'table' AND name = 'message_delivery'
                        """
                    ).fetchone()
                finally:
                    connection.close()

                self.assertEqual(10, version)
                self.assertIsNotNone(delivery_table)

        asyncio.run(scenario())

    def test_memory_schema_v8_adds_versioned_session_message_markers(self):
        async def scenario():
            with tempfile.TemporaryDirectory(
                prefix="akasha-memory-v8-"
            ) as temp:
                root = pathlib.Path(temp)
                store = MemoryStore(root)
                await store.initialize()
                contact = await store.ensure_contact(
                    contact_hmac="contact-hmac",
                    account_enc="account",
                    session_enc="session",
                    routing_name="contact",
                )
                session = await store.create_qwen_session(
                    contact.id,
                    conversation_id="legacy-clean-session",
                    model="qwen3.7-max",
                    persona_hash="persona",
                    tool_hash="tools",
                    created_at=time.time(),
                    expires_at=time.time() + 3600,
                    estimated_tokens=100,
                    expected_memory_revision=0,
                )
                await store.activate_qwen_session(
                    session.id,
                    expected_memory_revision=0,
                )
                connection = sqlite3.connect(store.path)
                try:
                    connection.execute("DROP TABLE qwen_session_messages")
                    connection.execute("PRAGMA user_version = 8")
                    connection.commit()
                finally:
                    connection.close()

                await store.initialize()
                connection = sqlite3.connect(store.path)
                try:
                    version = connection.execute(
                        "PRAGMA user_version"
                    ).fetchone()[0]
                    marker_table = connection.execute(
                        """
                        SELECT name
                        FROM sqlite_master
                        WHERE type = 'table'
                          AND name = 'qwen_session_messages'
                        """
                    ).fetchone()
                finally:
                    connection.close()

                self.assertEqual(10, version)
                self.assertIsNotNone(marker_table)
                migrated = await store.active_qwen_session(contact.id)
                self.assertIsNotNone(migrated)
                self.assertTrue(migrated.dirty)

        asyncio.run(scenario())

    def test_memory_schema_v10_repairs_interrupted_representation_backfill(self):
        async def scenario():
            with tempfile.TemporaryDirectory(
                prefix="akasha-memory-v10-repair-"
            ) as temp:
                root = pathlib.Path(temp)
                store = MemoryStore(root)
                await store.initialize()
                contact = await store.ensure_contact(
                    contact_hmac="contact-hmac",
                    account_enc="account",
                    session_enc="session",
                    routing_name="contact",
                )
                session = await store.create_qwen_session(
                    contact.id,
                    conversation_id="interrupted-session",
                    model="qwen3.7-max",
                    persona_hash="persona",
                    tool_hash="tools",
                    created_at=time.time() - 60,
                    expires_at=time.time() + 3600,
                    estimated_tokens=100,
                    expected_memory_revision=0,
                )
                await store.activate_qwen_session(
                    session.id,
                    expected_memory_revision=0,
                )
                await store.upsert_messages(
                    contact.id,
                    (
                        MemoryMessage(
                            "raw:interrupted-media",
                            time.time(),
                            "in",
                            "[图片]",
                            semantic_content="[图片: cat]",
                            origin="weflow",
                        ),
                    ),
                )
                connection = sqlite3.connect(store.path)
                try:
                    connection.execute(
                        "UPDATE messages SET representation_hash = ''"
                    )
                    connection.execute(
                        "UPDATE qwen_sessions SET dirty = 0"
                    )
                    connection.commit()
                finally:
                    connection.close()

                await store.initialize()
                connection = sqlite3.connect(store.path)
                try:
                    representation_hash = connection.execute(
                        """
                        SELECT representation_hash FROM messages
                        WHERE source_uid = 'raw:interrupted-media'
                        """
                    ).fetchone()[0]
                finally:
                    connection.close()
                self.assertTrue(representation_hash)
                repaired = await store.active_qwen_session(contact.id)
                self.assertIsNotNone(repaired)
                self.assertTrue(repaired.dirty)

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

    def test_limited_rebuild_uses_only_twenty_recent_messages(self):
        async def scenario():
            with tempfile.TemporaryDirectory(prefix="akasha-memory-limited-seed-") as temp:
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
                            f"raw:{index:02d}",
                            float(index),
                            "in",
                            f"history-{index:02d}",
                        )
                        for index in range(1, 36)
                    ],
                )
                await store.save_summary(
                    contact.id,
                    content="summary-must-not-enter-limited-rebuild",
                    through_message_id=10,
                )

                bundle = await ContextBuilder(store).build(
                    contact.id,
                    current_prompt="history-01",
                    recent_message_limit=20,
                )
                seeded = [
                    str(item["content"])
                    for item in bundle.items
                    if str(item["content"]).startswith("history-")
                ]
                self.assertEqual(
                    [f"history-{index:02d}" for index in range(16, 36)],
                    seeded,
                )
                self.assertEqual(20, bundle.recent_count)
                self.assertEqual(0, bundle.retrieved_count)
                self.assertNotIn(
                    "summary-must-not-enter-limited-rebuild",
                    "\n".join(str(item["content"]) for item in bundle.items),
                )

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
                reopened = MemoryStore(root)
                await reopened.initialize()
                self.assertEqual(
                    (1, 20),
                    await reopened.contact_seed_state(contact.id),
                )
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

    def test_bridge_send_success_confirms_generated_cloud_turn(self):
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
                    "success": True,
                    "delivered_parts": ["第一句。", "第二句。"],
                }
            )

            def is_private_chat(self):
                return True

        async def scenario():
            with tempfile.TemporaryDirectory(
                prefix="akasha-memory-send-success-"
            ) as temp:
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
                    response_id="delivered-response",
                    content="第一句。第二句。",
                )
                runtime = ContactMemoryRuntime(
                    mode="active",
                    secret_manager=secrets,
                    store=store,
                    synchronizer=FakeSync(),
                    context_builder=ContextBuilder(store),
                    qwen_sessions=None,
                )

                self.assertTrue(await runtime.record_send_success(FakeEvent()))
                self.assertEqual(
                    ["第一句。第二句。"],
                    [
                        message.content
                        for message in await store.recent_messages(contact.id)
                    ],
                )

        asyncio.run(scenario())

    def test_external_weflow_turn_advances_revision_without_dirtying_session(self):
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
                self.assertFalse(current.dirty)
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

    def test_send_failure_limits_only_the_next_successful_seed(self):
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
                self.seeded = {}
                self.responses = 0

            async def create_conversation(self, *, metadata=None):
                conversation_id = f"conv-{len(self.created) + 1}"
                self.created.append(conversation_id)
                self.seeded[conversation_id] = []
                return conversation_id

            async def add_items(self, conversation_id, items):
                offset = len(self.seeded[conversation_id])
                self.seeded[conversation_id].extend(dict(item) for item in items)
                return [
                    f"{conversation_id}-item-{offset + index}"
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
                self.responses += 1
                return QwenResult(
                    response_id=f"response-{self.responses}",
                    text=f"reply-{self.responses}",
                )

            async def delete_conversation_fully(self, conversation_id):
                return None

        async def scenario():
            with tempfile.TemporaryDirectory(prefix="akasha-memory-seed-once-") as temp:
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
                            f"raw:{index:02d}",
                            float(index),
                            "in",
                            f"history-{index:02d}",
                        )
                        for index in range(1, 36)
                    ],
                )
                client = FakeClient()
                manager = QwenSessionManager(
                    store=store,
                    context_builder=ContextBuilder(store),
                    client=client,
                )

                await manager.respond(
                    contact,
                    prompt="first",
                    system_prompt="persona",
                    request_key="request-1",
                )
                await store.invalidate_unconfirmed_outputs(contact.id)
                failure_revision, rebuild_limit = (
                    await store.contact_seed_state(contact.id)
                )
                self.assertEqual(20, rebuild_limit)

                await manager.respond(
                    contact,
                    prompt="second",
                    system_prompt="persona",
                    request_key="request-2",
                )
                self.assertEqual(
                    (failure_revision, 0),
                    await store.contact_seed_state(contact.id),
                )
                await store.mark_contact_sessions_dirty(contact.id)
                await manager.respond(
                    contact,
                    prompt="third",
                    system_prompt="persona",
                    request_key="request-3",
                )

                def seeded_history(conversation_id):
                    return [
                        str(item["content"])
                        for item in client.seeded[conversation_id]
                        if str(item["content"]).startswith("history-")
                    ]

                self.assertEqual(35, len(seeded_history("conv-1")))
                self.assertEqual(
                    [f"history-{index:02d}" for index in range(16, 36)],
                    seeded_history("conv-2"),
                )
                self.assertEqual(35, len(seeded_history("conv-3")))

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
                self.assertFalse(current.dirty)
                self.assertTrue(
                    await store.confirm_generated_delivery(
                        contact.id,
                        ["fallback reply A"],
                    )
                )

                await manager.respond(
                    contact,
                    prompt="request D",
                    system_prompt="persona",
                    request_key="owner-d",
                    tool_fingerprint="schema",
                    tools=tools,
                )
                current = await store.active_qwen_session(contact.id)
                self.assertEqual("conv-2", current.conversation_id)
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
                self.assertEqual("conv-2", current.conversation_id)
                self.assertFalse(current.dirty)

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

    def test_successful_delivery_survives_later_incoming_before_weflow_confirmation(
        self,
    ):
        async def scenario():
            with tempfile.TemporaryDirectory(
                prefix="akasha-memory-delivery-confirmed-"
            ) as temp:
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
                    conversation_id="persistent-conversation",
                    model="qwen3.7-max",
                    persona_hash="persona",
                    tool_hash="tools",
                    created_at=time.time() - 2100,
                    expires_at=time.time() + 3600,
                    estimated_tokens=100,
                    expected_memory_revision=0,
                )
                await store.activate_qwen_session(
                    session.id,
                    expected_memory_revision=0,
                )
                await store.archive_generated(
                    contact.id,
                    response_id="cloud-response",
                    content="第一句。第二句。",
                    source_time=time.time() - 1800,
                )

                self.assertFalse(
                    await store.confirm_generated_delivery(
                        contact.id,
                        ["第一句。"],
                    )
                )
                self.assertTrue(
                    await store.confirm_generated_delivery(
                        contact.id,
                        ["第二句。"],
                    )
                )
                await store.upsert_messages(
                    contact.id,
                    [
                        MemoryMessage(
                            "raw:later-incoming",
                            time.time(),
                            "in",
                            "下一条消息",
                            origin="weflow",
                        )
                    ],
                )
                resolved, dirty = await store.reconcile_pending_outputs(
                    contact.id,
                    stale_after_seconds=30,
                )

                self.assertEqual(resolved, 0)
                self.assertFalse(dirty)
                self.assertFalse(
                    (await store.active_qwen_session(contact.id)).dirty
                )
                self.assertIn(
                    "第一句。第二句。",
                    [
                        message.content
                        for message in await store.recent_messages(contact.id)
                    ],
                )

        asyncio.run(scenario())

    def test_external_delta_appends_without_recreating_contact_conversation(self):
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
                self.add_calls = []
                self.respond_calls = []

            async def create_conversation(self, *, metadata=None):
                conversation_id = f"conv-{len(self.created) + 1}"
                self.created.append(conversation_id)
                return conversation_id

            async def add_items(self, conversation_id, items):
                copied = [dict(item) for item in items]
                self.add_calls.append((conversation_id, copied))
                return [
                    f"{conversation_id}-item-{len(self.add_calls)}-{index}"
                    for index in range(len(copied))
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
                self.respond_calls.append((conversation_id, prompt))
                index = len(self.respond_calls)
                return QwenResult(
                    response_id=f"response-{index}",
                    text=f"reply-{index}",
                    input_tokens=100 + index,
                    output_tokens=10,
                )

            async def delete_conversation_fully(self, _conversation_id):
                return None

        async def scenario():
            with tempfile.TemporaryDirectory(
                prefix="akasha-memory-incremental-"
            ) as temp:
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

                await manager.respond(
                    contact,
                    prompt="first prompt",
                    system_prompt="persona",
                    request_key="request-1",
                )
                client.add_calls.clear()
                await store.upsert_messages(
                    contact.id,
                    [
                        MemoryMessage(
                            "raw:manual-outgoing",
                            time.time() + 1,
                            "out",
                            "operator note",
                            origin="weflow",
                        )
                    ],
                )
                self.assertFalse(
                    (await store.active_qwen_session(contact.id)).dirty
                )

                await manager.respond(
                    contact,
                    prompt="second prompt",
                    system_prompt="persona",
                    request_key="request-2",
                )

                self.assertEqual(client.created, ["conv-1"])
                self.assertEqual(
                    client.respond_calls,
                    [
                        ("conv-1", "first prompt"),
                        ("conv-1", "second prompt"),
                    ],
                )
                self.assertEqual(
                    client.add_calls,
                    [
                        (
                            "conv-1",
                            [{"role": "assistant", "content": "operator note"}],
                        )
                    ],
                )

        asyncio.run(scenario())

    def test_incremental_delta_keeps_bounded_backlog_and_late_fallback(self):
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
                self.add_calls = []
                self.respond_calls = []

            async def create_conversation(self, *, metadata=None):
                conversation_id = f"conv-{len(self.created) + 1}"
                self.created.append(conversation_id)
                return conversation_id

            async def add_items(self, conversation_id, items):
                copied = [dict(item) for item in items]
                self.add_calls.append((conversation_id, copied))
                return [
                    f"{conversation_id}-item-{len(self.add_calls)}-{index}"
                    for index in range(len(copied))
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
                self.respond_calls.append((conversation_id, prompt))
                index = len(self.respond_calls)
                return QwenResult(
                    response_id=f"response-{index}",
                    text=f"reply-{index}",
                    input_tokens=100,
                    output_tokens=10,
                )

            async def delete_conversation_fully(self, _conversation_id):
                return None

        def appended_contents(client):
            return [
                str(item["content"])
                for _conversation_id, items in client.add_calls
                for item in items
                if item.get("role") in {"user", "assistant"}
            ]

        async def scenario():
            with tempfile.TemporaryDirectory(
                prefix="akasha-memory-delta-watermark-"
            ) as temp:
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
                await manager.respond(
                    contact,
                    prompt="create session",
                    system_prompt="persona",
                    request_key="request-1",
                )
                self.assertTrue(
                    await store.confirm_generated_delivery(
                        contact.id,
                        ("reply-1",),
                    )
                )
                await store.upsert_messages(
                    contact.id,
                    (
                        MemoryMessage(
                            "raw:reply-1",
                            time.time(),
                            "out",
                            "reply-1",
                            origin="weflow",
                        ),
                        MemoryMessage(
                            "raw:after-native-reply",
                            time.time() + 0.1,
                            "out",
                            "external after native reply",
                            origin="weflow",
                        ),
                    ),
                )
                client.add_calls.clear()
                await manager.respond(
                    contact,
                    prompt="after native reply observation",
                    system_prompt="persona",
                    request_key="request-2",
                )
                self.assertEqual(
                    ["external after native reply"],
                    appended_contents(client),
                )

                tiny_messages = [
                    MemoryMessage(
                        f"raw:tiny-{index:03d}",
                        time.time() + index / 1000,
                        "in",
                        f"external-{index:03d}",
                        origin="weflow",
                    )
                    for index in range(201)
                ]
                await store.upsert_messages(contact.id, tiny_messages)
                client.add_calls.clear()
                await manager.respond(
                    contact,
                    prompt="after tiny backlog",
                    system_prompt="persona",
                    request_key="request-3",
                )
                self.assertEqual(
                    [message.content for message in tiny_messages],
                    appended_contents(client),
                )

                large_messages = [
                    MemoryMessage(
                        f"raw:large-{index:03d}",
                        time.time() + 10 + index / 1000,
                        "out",
                        f"large-{index:03d}-" + ("x" * 1000),
                        origin="weflow",
                    )
                    for index in range(60)
                ]
                await store.upsert_messages(contact.id, large_messages)
                client.add_calls.clear()
                await manager.respond(
                    contact,
                    prompt="first bounded append",
                    system_prompt="persona",
                    request_key="request-4",
                )
                partial = appended_contents(client)
                self.assertGreater(len(partial), 0)
                self.assertLess(len(partial), len(large_messages))
                await manager.respond(
                    contact,
                    prompt="second bounded append",
                    system_prompt="persona",
                    request_key="request-5",
                )
                self.assertEqual(
                    [message.content for message in large_messages],
                    appended_contents(client),
                )
                self.assertEqual(
                    await store.contact_memory_revision(contact.id),
                    (await store.active_qwen_session(contact.id)).memory_revision,
                )

                oversized = "oversized-" + ("长" * 40_000)
                await store.upsert_messages(
                    contact.id,
                    (
                        MemoryMessage(
                            "raw:oversized",
                            time.time() + 20,
                            "in",
                            oversized,
                            origin="weflow",
                        ),
                        MemoryMessage(
                            "raw:after-oversized",
                            time.time() + 21,
                            "in",
                            "small after oversized",
                            origin="weflow",
                        ),
                    ),
                )
                client.add_calls.clear()
                await manager.respond(
                    contact,
                    prompt="first oversized append",
                    system_prompt="persona",
                    request_key="request-6",
                )
                await manager.respond(
                    contact,
                    prompt="second oversized append",
                    system_prompt="persona",
                    request_key="request-7",
                )
                oversized_delta = appended_contents(client)
                self.assertEqual(2, len(oversized_delta))
                self.assertTrue(
                    oversized_delta[0].startswith("【单条消息过长")
                )
                self.assertLess(len(oversized_delta[0]), len(oversized))
                self.assertEqual(
                    "small after oversized",
                    oversized_delta[1],
                )
                self.assertEqual(
                    await store.contact_memory_revision(contact.id),
                    (await store.active_qwen_session(contact.id)).memory_revision,
                )

                await store.upsert_messages(
                    contact.id,
                    (
                        MemoryMessage(
                            "raw:native-prompt",
                            time.time() + 30,
                            "in",
                            "native prompt",
                            origin="bridge",
                            pending=True,
                        ),
                    ),
                )
                await manager.respond(
                    contact,
                    prompt="native prompt",
                    system_prompt="persona",
                    request_key="request-8",
                    exclude_source_uids=("raw:native-prompt",),
                )
                await store.upsert_messages(
                    contact.id,
                    (
                        MemoryMessage(
                            "raw:native-prompt",
                            time.time() + 31,
                            "in",
                            "native prompt",
                            origin="weflow",
                        ),
                        MemoryMessage(
                            "raw:after-native-prompt",
                            time.time() + 32,
                            "in",
                            "external after native prompt",
                            origin="weflow",
                        ),
                    ),
                )
                client.add_calls.clear()
                await manager.respond(
                    contact,
                    prompt="after native prompt observation",
                    system_prompt="persona",
                    request_key="request-9",
                )
                self.assertEqual(
                    ["external after native prompt"],
                    appended_contents(client),
                )

                await manager.archive_fallback_output(
                    contact.id,
                    "late fallback reply",
                )
                await manager.respond(
                    contact,
                    prompt="turn before fallback confirmation",
                    system_prompt="persona",
                    request_key="request-10",
                )
                self.assertTrue(
                    await store.confirm_generated_delivery(
                        contact.id,
                        ("late fallback reply",),
                    )
                )
                client.add_calls.clear()
                await manager.respond(
                    contact,
                    prompt="turn after fallback confirmation",
                    system_prompt="persona",
                    request_key="request-11",
                )
                self.assertEqual(
                    ["late fallback reply"],
                    appended_contents(client),
                )
                await store.upsert_messages(
                    contact.id,
                    (
                        MemoryMessage(
                            "raw:late-fallback",
                            time.time() + 40,
                            "out",
                            "late fallback reply",
                            origin="weflow",
                        ),
                        MemoryMessage(
                            "raw:after-fallback",
                            time.time() + 41,
                            "out",
                            "external after fallback",
                            origin="weflow",
                        ),
                    ),
                )
                client.add_calls.clear()
                await manager.respond(
                    contact,
                    prompt="after fallback observation",
                    system_prompt="persona",
                    request_key="request-12",
                )
                self.assertEqual(
                    ["external after fallback"],
                    appended_contents(client),
                )

                await store.upsert_messages(
                    contact.id,
                    (
                        MemoryMessage(
                            "raw:corrected",
                            time.time() + 50,
                            "out",
                            "old authoritative content",
                            origin="weflow",
                        ),
                    ),
                )
                client.add_calls.clear()
                await manager.respond(
                    contact,
                    prompt="before correction",
                    system_prompt="persona",
                    request_key="request-13",
                )
                self.assertEqual(
                    ["old authoritative content"],
                    appended_contents(client),
                )
                await store.upsert_messages(
                    contact.id,
                    (
                        MemoryMessage(
                            "raw:corrected",
                            time.time() + 51,
                            "out",
                            "corrected authoritative content",
                            origin="weflow",
                        ),
                    ),
                )
                client.add_calls.clear()
                await manager.respond(
                    contact,
                    prompt="after correction",
                    system_prompt="persona",
                    request_key="request-14",
                )
                self.assertEqual(
                    ["corrected authoritative content"],
                    appended_contents(client),
                )
                await store.upsert_messages(
                    contact.id,
                    (
                        MemoryMessage(
                            "raw:corrected",
                            time.time() + 52,
                            "out",
                            "old authoritative content",
                            origin="weflow",
                        ),
                    ),
                )
                client.add_calls.clear()
                await manager.respond(
                    contact,
                    prompt="after correction rollback",
                    system_prompt="persona",
                    request_key="request-15",
                )
                self.assertEqual(
                    ["old authoritative content"],
                    appended_contents(client),
                )

                await store.upsert_messages(
                    contact.id,
                    (
                        MemoryMessage(
                            "raw:semantic-correction",
                            time.time() + 53,
                            "in",
                            "[图片]",
                            semantic_content="[图片: cat]",
                            origin="weflow",
                        ),
                    ),
                )
                client.add_calls.clear()
                await manager.respond(
                    contact,
                    prompt="after semantic image",
                    system_prompt="persona",
                    request_key="request-16",
                )
                self.assertEqual(
                    ["[图片: cat]"],
                    appended_contents(client),
                )
                await store.upsert_messages(
                    contact.id,
                    (
                        MemoryMessage(
                            "raw:semantic-correction",
                            time.time() + 54,
                            "in",
                            "[图片]",
                            semantic_content="[图片: dog]",
                            origin="weflow",
                        ),
                    ),
                )
                client.add_calls.clear()
                await manager.respond(
                    contact,
                    prompt="after semantic correction",
                    system_prompt="persona",
                    request_key="request-17",
                )
                self.assertEqual(
                    ["[图片: dog]"],
                    appended_contents(client),
                )
                self.assertEqual(client.created, ["conv-1"])

        asyncio.run(scenario())

    def test_send_failure_preserves_earlier_submitted_generated_turn(self):
        async def scenario():
            with tempfile.TemporaryDirectory(
                prefix="akasha-memory-failure-scope-"
            ) as temp:
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
                    conversation_id="persistent-conversation",
                    model="qwen3.7-max",
                    persona_hash="persona",
                    tool_hash="tools",
                    created_at=time.time(),
                    expires_at=time.time() + 3600,
                    estimated_tokens=100,
                    expected_memory_revision=0,
                )
                await store.activate_qwen_session(
                    session.id,
                    expected_memory_revision=0,
                )
                await store.archive_generated(
                    contact.id,
                    response_id="submitted-a",
                    content="reply A",
                )
                self.assertTrue(
                    await store.confirm_generated_delivery(
                        contact.id,
                        ("reply A",),
                    )
                )
                await store.archive_generated(
                    contact.id,
                    response_id="failed-b",
                    content="reply B",
                )

                deleted = await store.invalidate_unconfirmed_outputs(contact.id)

                self.assertEqual(1, deleted)
                self.assertEqual(
                    ["reply A"],
                    [
                        message.content
                        for message in await store.recent_messages(contact.id)
                        if message.origin == "generated"
                    ],
                )
                self.assertTrue(
                    (await store.active_qwen_session(contact.id)).dirty
                )

        asyncio.run(scenario())

    def test_delta_marker_survives_concurrent_fallback_identity_change(self):
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

        class BlockingClient:
            def __init__(self):
                self.calls = []
                self.entered = asyncio.Event()
                self.release = asyncio.Event()

            async def add_items(self, conversation_id, items):
                copied = [dict(item) for item in items]
                self.calls.append((conversation_id, copied))
                if len(self.calls) == 1:
                    self.entered.set()
                    await self.release.wait()
                return [
                    f"item-{len(self.calls)}-{index}"
                    for index in range(len(copied))
                ]

        async def scenario():
            with tempfile.TemporaryDirectory(
                prefix="akasha-memory-delta-race-"
            ) as temp:
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
                created_at = time.time() - 60
                session = await store.create_qwen_session(
                    contact.id,
                    conversation_id="persistent-conversation",
                    model="qwen3.7-max",
                    persona_hash="persona",
                    tool_hash="tools",
                    created_at=created_at,
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
                    response_id="fallback-race",
                    content="fallback race",
                    id_quality="fallback_response",
                )
                self.assertTrue(
                    await store.confirm_generated_delivery(
                        contact.id,
                        ("fallback race",),
                    )
                )
                client = BlockingClient()
                manager = QwenSessionManager(
                    store=store,
                    context_builder=ContextBuilder(store),
                    client=client,
                )
                append_task = asyncio.create_task(
                    manager._append_external_delta(
                        session,
                        contact_id=contact.id,
                        target_memory_revision=1,
                        current_prompt="",
                        exclude_source_uids=(),
                    )
                )
                await asyncio.wait_for(client.entered.wait(), timeout=2)
                await store.upsert_messages(
                    contact.id,
                    (
                        MemoryMessage(
                            "raw:fallback-race",
                            time.time(),
                            "out",
                            "fallback race",
                            origin="weflow",
                        ),
                    ),
                )
                client.release.set()
                session = await asyncio.wait_for(append_task, timeout=2)
                self.assertEqual(
                    [["fallback race"]],
                    [
                        [item["content"] for item in items]
                        for _conversation_id, items in client.calls
                    ],
                )

                await store.upsert_messages(
                    contact.id,
                    (
                        MemoryMessage(
                            "raw:after-race",
                            time.time() + 1,
                            "out",
                            "after race",
                            origin="weflow",
                        ),
                    ),
                )
                session = await manager._append_external_delta(
                    session,
                    contact_id=contact.id,
                    target_memory_revision=2,
                    current_prompt="",
                    exclude_source_uids=(),
                )
                self.assertEqual(
                    [["fallback race"], ["after race"]],
                    [
                        [item["content"] for item in items]
                        for _conversation_id, items in client.calls
                    ],
                )
                self.assertEqual(2, session.memory_revision)

        asyncio.run(scenario())

    def test_delta_marker_records_the_version_actually_sent_during_correction(self):
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

        class BlockingClient:
            def __init__(self):
                self.calls = []
                self.entered = asyncio.Event()
                self.release = asyncio.Event()

            async def add_items(self, conversation_id, items):
                copied = [dict(item) for item in items]
                self.calls.append((conversation_id, copied))
                if len(self.calls) == 1:
                    self.entered.set()
                    await self.release.wait()
                return [
                    f"item-{len(self.calls)}-{index}"
                    for index in range(len(copied))
                ]

        async def scenario():
            with tempfile.TemporaryDirectory(
                prefix="akasha-memory-content-race-"
            ) as temp:
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
                created_at = time.time() - 60
                session = await store.create_qwen_session(
                    contact.id,
                    conversation_id="persistent-conversation",
                    model="qwen3.7-max",
                    persona_hash="persona",
                    tool_hash="tools",
                    created_at=created_at,
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
                    (
                        MemoryMessage(
                            "raw:corrected-race",
                            time.time(),
                            "out",
                            "old content",
                            origin="weflow",
                        ),
                    ),
                )
                client = BlockingClient()
                manager = QwenSessionManager(
                    store=store,
                    context_builder=ContextBuilder(store),
                    client=client,
                )
                append_task = asyncio.create_task(
                    manager._append_external_delta(
                        session,
                        contact_id=contact.id,
                        target_memory_revision=1,
                        current_prompt="",
                        exclude_source_uids=(),
                    )
                )
                await asyncio.wait_for(client.entered.wait(), timeout=2)
                await store.upsert_messages(
                    contact.id,
                    (
                        MemoryMessage(
                            "raw:corrected-race",
                            time.time() + 1,
                            "out",
                            "corrected content",
                            origin="weflow",
                        ),
                    ),
                )
                client.release.set()
                session = await asyncio.wait_for(append_task, timeout=2)
                self.assertEqual(
                    [["old content"], ["corrected content"]],
                    [
                        [item["content"] for item in items]
                        for _conversation_id, items in client.calls
                    ],
                )

                session = await manager._append_external_delta(
                    session,
                    contact_id=contact.id,
                    target_memory_revision=2,
                    current_prompt="",
                    exclude_source_uids=(),
                )
                self.assertEqual(2, len(client.calls))
                self.assertEqual(2, session.memory_revision)

        asyncio.run(scenario())

    def test_split_reconcile_during_remote_add_does_not_append_twice(self):
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

        class BlockingClient:
            def __init__(self):
                self.calls = []
                self.entered = asyncio.Event()
                self.release = asyncio.Event()

            async def add_items(self, conversation_id, items):
                copied = [dict(item) for item in items]
                self.calls.append((conversation_id, copied))
                if len(self.calls) == 1:
                    self.entered.set()
                    await self.release.wait()
                return [
                    f"item-{len(self.calls)}-{index}"
                    for index in range(len(copied))
                ]

        async def scenario():
            with tempfile.TemporaryDirectory(
                prefix="akasha-memory-split-add-race-"
            ) as temp:
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
                created_at = time.time() - 60
                session = await store.create_qwen_session(
                    contact.id,
                    conversation_id="persistent-conversation",
                    model="qwen3.7-max",
                    persona_hash="persona",
                    tool_hash="tools",
                    created_at=created_at,
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
                    response_id="fallback-split-race",
                    content="part Apart B",
                    id_quality="fallback_response",
                )
                self.assertTrue(
                    await store.confirm_generated_delivery(
                        contact.id,
                        ("part A", "part B"),
                    )
                )
                client = BlockingClient()
                manager = QwenSessionManager(
                    store=store,
                    context_builder=ContextBuilder(store),
                    client=client,
                )
                append_task = asyncio.create_task(
                    manager._append_external_delta(
                        session,
                        contact_id=contact.id,
                        target_memory_revision=1,
                        current_prompt="",
                        exclude_source_uids=(),
                    )
                )
                await asyncio.wait_for(client.entered.wait(), timeout=2)
                now = time.time()
                await store.upsert_messages(
                    contact.id,
                    (
                        MemoryMessage(
                            "raw:split-race-a",
                            now,
                            "out",
                            "part A",
                            origin="weflow",
                        ),
                        MemoryMessage(
                            "raw:split-race-b",
                            now + 1,
                            "out",
                            "part B",
                            origin="weflow",
                        ),
                    ),
                )
                resolved, dirty = await store.reconcile_pending_outputs(
                    contact.id,
                )
                self.assertEqual(1, resolved)
                self.assertFalse(dirty)
                client.release.set()
                session = await asyncio.wait_for(append_task, timeout=2)

                self.assertEqual(
                    [["part Apart B"]],
                    [
                        [item["content"] for item in items]
                        for _conversation_id, items in client.calls
                    ],
                )
                self.assertEqual(1, session.memory_revision)
                self.assertEqual(
                    [],
                    await store.session_delta_messages(
                        contact.id,
                        session_id=session.id,
                        after_created_at=session.created_at,
                        recent_source_floor=session.created_at - 300,
                    ),
                )

        asyncio.run(scenario())

    def test_prompt_snapshot_survives_bridge_to_weflow_uid_rewrite(self):
        fake_client_module = types.ModuleType("akasha_memory.qwen_client")
        fake_client_module.QwenClient = object
        original = sys.modules.get("akasha_memory.qwen_client")
        sys.modules["akasha_memory.qwen_client"] = fake_client_module
        try:
            from akasha_memory.qwen_session import QwenSessionManager, stable_hash
        finally:
            if original is None:
                sys.modules.pop("akasha_memory.qwen_client", None)
            else:
                sys.modules["akasha_memory.qwen_client"] = original

        class FakeClient:
            def __init__(self):
                self.add_calls = []
                self.responses = 0

            async def add_items(self, conversation_id, items):
                copied = [dict(item) for item in items]
                self.add_calls.append((conversation_id, copied))
                return [
                    f"item-{len(self.add_calls)}-{index}"
                    for index in range(len(copied))
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
                self.responses += 1
                return QwenResult(
                    response_id=f"response-{self.responses}",
                    text=f"reply-{self.responses}",
                    input_tokens=100,
                    output_tokens=10,
                )

        async def scenario():
            with tempfile.TemporaryDirectory(
                prefix="akasha-memory-prompt-id-"
            ) as temp:
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
                created_at = time.time() - 60
                session = await store.create_qwen_session(
                    contact.id,
                    conversation_id="persistent-conversation",
                    model="qwen3.7-max",
                    persona_hash=stable_hash("persona"),
                    tool_hash=stable_hash(""),
                    created_at=created_at,
                    expires_at=time.time() + 3600,
                    estimated_tokens=100,
                    expected_memory_revision=0,
                )
                await store.activate_qwen_session(
                    session.id,
                    expected_memory_revision=0,
                )
                source_time = time.time()
                _changed, snapshots = (
                    await store.upsert_messages_with_snapshots(
                        contact.id,
                        (
                            MemoryMessage(
                                "bridge:fp:1",
                                source_time,
                                "in",
                                "native prompt",
                                origin="bridge",
                                pending=True,
                            ),
                        ),
                    )
                )
                self.assertEqual(1, len(snapshots))
                await store.upsert_messages(
                    contact.id,
                    (
                        MemoryMessage(
                            "raw:observed",
                            source_time,
                            "in",
                            "native prompt",
                            origin="weflow",
                        ),
                    ),
                )
                observed = next(
                    message
                    for message in await store.recent_messages(contact.id)
                    if message.content == "native prompt"
                )
                self.assertEqual(snapshots[0].id, observed.id)
                self.assertNotEqual(snapshots[0].source_uid, observed.source_uid)

                client = FakeClient()
                manager = QwenSessionManager(
                    store=store,
                    context_builder=ContextBuilder(store),
                    client=client,
                )
                await manager.respond(
                    contact,
                    prompt="native prompt",
                    system_prompt="persona",
                    request_key="request-1",
                    exclude_source_uids=("bridge:fp:1",),
                    represented_prompt_messages=snapshots,
                )
                await store.upsert_messages(
                    contact.id,
                    (
                        MemoryMessage(
                            "raw:after-prompt",
                            source_time + 1,
                            "in",
                            "external after prompt",
                            origin="weflow",
                        ),
                    ),
                )
                client.add_calls.clear()
                await manager.respond(
                    contact,
                    prompt="next prompt",
                    system_prompt="persona",
                    request_key="request-2",
                )
                self.assertEqual(
                    ["external after prompt"],
                    [
                        item["content"]
                        for _conversation_id, items in client.add_calls
                        for item in items
                    ],
                )

        asyncio.run(scenario())

    def test_context_seed_keeps_new_semantic_version_of_current_prompt(self):
        async def scenario():
            with tempfile.TemporaryDirectory(
                prefix="akasha-memory-semantic-prompt-"
            ) as temp:
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
                source_time = time.time()
                _changed, prompt_snapshots = (
                    await store.upsert_messages_with_snapshots(
                        contact.id,
                        (
                            MemoryMessage(
                                "raw:semantic-prompt",
                                source_time,
                                "in",
                                "[图片]",
                                semantic_content="[图片: cat]",
                                origin="weflow",
                            ),
                        ),
                    )
                )
                await store.upsert_messages(
                    contact.id,
                    (
                        MemoryMessage(
                            "raw:semantic-prompt",
                            source_time + 1,
                            "in",
                            "[图片]",
                            semantic_content="[图片: dog]",
                            origin="weflow",
                        ),
                    ),
                )

                bundle = await ContextBuilder(store).build(
                    contact.id,
                    current_prompt="[图片: cat]",
                    represented_prompt_messages=prompt_snapshots,
                )

                self.assertEqual(
                    ["[图片: dog]"],
                    [
                        item["content"]
                        for item in bundle.items
                        if item.get("role") == "user"
                    ],
                )
                self.assertEqual(
                    ["[图片: dog]"],
                    [
                        message.effective_content
                        for message in bundle.represented_messages
                    ],
                )

        asyncio.run(scenario())

    def test_session_markers_cannot_cross_contact_boundaries(self):
        async def scenario():
            with tempfile.TemporaryDirectory(
                prefix="akasha-memory-marker-contact-"
            ) as temp:
                root = pathlib.Path(temp)
                secrets = SecretManager(root)
                store = MemoryStore(root)
                await store.initialize()
                contact_a = await store.ensure_contact(
                    contact_hmac=secrets.contact_hmac("account", "session-a"),
                    account_enc=secrets.encrypt_text("account"),
                    session_enc=secrets.encrypt_text("session-a"),
                    routing_name="contact-a",
                )
                contact_b = await store.ensure_contact(
                    contact_hmac=secrets.contact_hmac("account", "session-b"),
                    account_enc=secrets.encrypt_text("account"),
                    session_enc=secrets.encrypt_text("session-b"),
                    routing_name="contact-b",
                )
                created_at = time.time() - 60
                session_a = await store.create_qwen_session(
                    contact_a.id,
                    conversation_id="contact-a-conversation",
                    model="qwen3.7-max",
                    persona_hash="persona",
                    tool_hash="tools",
                    created_at=created_at,
                    expires_at=time.time() + 3600,
                    estimated_tokens=100,
                    expected_memory_revision=0,
                )
                await store.activate_qwen_session(
                    session_a.id,
                    expected_memory_revision=0,
                )
                await store.upsert_messages(
                    contact_b.id,
                    (
                        MemoryMessage(
                            "raw:contact-b-secret",
                            time.time(),
                            "in",
                            "contact B message",
                            origin="weflow",
                        ),
                    ),
                )
                message_b = (await store.recent_messages(contact_b.id))[0]

                await store.record_qwen_external_messages(
                    session_a.id,
                    (message_b,),
                )
                delta = await store.session_delta_messages(
                    contact_b.id,
                    session_id=session_a.id,
                    after_created_at=created_at,
                    recent_source_floor=created_at - 300,
                )
                self.assertEqual(
                    ["contact B message"],
                    [message.content for message in delta],
                )

        asyncio.run(scenario())

    def test_unrepresented_fallback_split_remains_queued_for_delta(self):
        async def scenario():
            with tempfile.TemporaryDirectory(
                prefix="akasha-memory-unrepresented-split-"
            ) as temp:
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
                created_at = time.time() - 60
                session = await store.create_qwen_session(
                    contact.id,
                    conversation_id="persistent-conversation",
                    model="qwen3.7-max",
                    persona_hash="persona",
                    tool_hash="tools",
                    created_at=created_at,
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
                    response_id="fallback-not-yet-appended",
                    content="part Apart B",
                    id_quality="fallback_response",
                )
                self.assertTrue(
                    await store.confirm_generated_delivery(
                        contact.id,
                        ("part A", "part B"),
                    )
                )
                now = time.time()
                await store.upsert_messages(
                    contact.id,
                    (
                        MemoryMessage(
                            "raw:unrepresented-a",
                            now,
                            "out",
                            "part A",
                            origin="weflow",
                        ),
                        MemoryMessage(
                            "raw:unrepresented-b",
                            now + 1,
                            "out",
                            "part B",
                            origin="weflow",
                        ),
                    ),
                )
                resolved, dirty = await store.reconcile_pending_outputs(
                    contact.id,
                )
                self.assertEqual(1, resolved)
                self.assertFalse(dirty)
                delta = await store.session_delta_messages(
                    contact.id,
                    session_id=session.id,
                    after_created_at=session.created_at,
                    recent_source_floor=session.created_at - 300,
                )
                self.assertEqual(
                    ["part A", "part B"],
                    [message.content for message in delta],
                )

        asyncio.run(scenario())

    def test_split_weflow_confirmation_inherits_session_representation(self):
        async def scenario():
            with tempfile.TemporaryDirectory(
                prefix="akasha-memory-split-marker-"
            ) as temp:
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
                created_at = time.time() - 60
                session = await store.create_qwen_session(
                    contact.id,
                    conversation_id="persistent-conversation",
                    model="qwen3.7-max",
                    persona_hash="persona",
                    tool_hash="tools",
                    created_at=created_at,
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
                    response_id="fallback-split",
                    content="part Apart B",
                    id_quality="fallback_response",
                )
                self.assertTrue(
                    await store.confirm_generated_delivery(
                        contact.id,
                        ("part A", "part B"),
                    )
                )
                generated = next(
                    message
                    for message in await store.recent_messages(contact.id)
                    if message.origin == "generated"
                )
                await store.record_qwen_external_messages(
                    session.id,
                    (generated,),
                )
                now = time.time()
                await store.upsert_messages(
                    contact.id,
                    (
                        MemoryMessage(
                            "raw:split-a",
                            now,
                            "out",
                            "part A",
                            origin="weflow",
                        ),
                        MemoryMessage(
                            "raw:split-b",
                            now + 1,
                            "out",
                            "part B",
                            origin="weflow",
                        ),
                    ),
                )
                resolved, dirty = await store.reconcile_pending_outputs(
                    contact.id,
                )
                self.assertEqual(1, resolved)
                self.assertFalse(dirty)
                await store.upsert_messages(
                    contact.id,
                    (
                        MemoryMessage(
                            "raw:after-split",
                            now + 2,
                            "out",
                            "after split",
                            origin="weflow",
                        ),
                    ),
                )
                delta = await store.session_delta_messages(
                    contact.id,
                    session_id=session.id,
                    after_created_at=session.created_at,
                    recent_source_floor=session.created_at - 300,
                )
                self.assertEqual(["after split"], [item.content for item in delta])

        asyncio.run(scenario())

    def test_media_xml_is_replaced_with_short_semantic_placeholders(self):
        original_aiohttp = sys.modules.get("aiohttp")
        sys.modules["aiohttp"] = types.ModuleType("aiohttp")
        try:
            from akasha_memory.weflow_sync import parse_weflow_messages
        finally:
            if original_aiohttp is None:
                sys.modules.pop("aiohttp", None)
            else:
                sys.modules["aiohttp"] = original_aiohttp

        records = [
            {
                "rawid": "app-1",
                "localType": "49",
                "parsedContent": "<msg><appmsg><title>secret</title></appmsg></msg>",
            },
            {
                "rawid": "file-1",
                "localType": "49",
                "mediaType": "file",
                "fileName": "report.pdf",
                "rawContent": "<msg><appmsg><type>6</type></appmsg></msg>",
            },
            {
                "rawid": "image-1",
                "localType": "3",
                "rawContent": "<msg><img aeskey=\"private\" /></msg>",
            },
            {
                "rawid": "video-1",
                "localType": "43",
                "content": "<msg><videomsg cdnthumburl=\"private\" /></msg>",
            },
            {
                "rawid": "sticker-1",
                "localType": "47",
                "mediaType": "emoji",
                "rawContent": (
                    "<msg><emoji md5=\"private\" "
                    "cdnurl=\"https://private.example/sticker\" /></msg>"
                ),
            },
        ]

        messages = parse_weflow_messages(records)

        self.assertEqual(
            [message.content for message in messages],
            [
                "[微信应用消息]",
                "[文件: report.pdf]",
                "[图片]",
                "[图片]",
                "[视频]",
            ],
        )
        serialized = "\n".join(message.content for message in messages)
        self.assertNotIn("<msg", serialized)
        self.assertNotIn("private", serialized)
        self.assertTrue(all(len(message.content) <= 120 for message in messages))

    def test_memory_defaults_keep_initial_seed_well_below_context_limit(self):
        schema = json.loads(
            (PLUGIN / "_conf_schema.json").read_text(encoding="utf-8")
        )
        builder = ContextBuilder(MemoryStore(pathlib.Path(".")))

        self.assertEqual(schema["seed_max_tokens"]["default"], 24_000)
        self.assertEqual(schema["soft_context_tokens"]["default"], 120_000)
        self.assertEqual(schema["fallback_context_tokens"]["default"], 24_000)
        self.assertEqual(builder.seed_max_tokens, 24_000)
        self.assertEqual(builder.recent_query_limit, 200)
        self.assertEqual(builder.retrieval_limit, 0)


if __name__ == "__main__":
    unittest.main()
