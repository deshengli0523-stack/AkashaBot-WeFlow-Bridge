import asyncio
import json
import pathlib
import sqlite3
import sys
import tempfile
import time
import types
import unittest

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


class ContactMemoryTests(unittest.TestCase):
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
                    expected_memory_revision=0,
                )
                connection = sqlite3.connect(store.path)
                try:
                    connection.execute(
                        "ALTER TABLE contacts DROP COLUMN memory_revision"
                    )
                    connection.execute(
                        "ALTER TABLE qwen_sessions DROP COLUMN memory_revision"
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
                finally:
                    connection.close()
                self.assertEqual(5, version)
                self.assertIn("memory_revision", contact_columns)
                self.assertIn("memory_revision", session_columns)
                session = await store.active_qwen_session(contact.id)
                self.assertIsNotNone(session)
                self.assertEqual("legacy-conversation", session.conversation_id)
                self.assertTrue(session.dirty)

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
