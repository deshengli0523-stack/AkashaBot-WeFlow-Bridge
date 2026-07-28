import asyncio
import importlib.util
import pathlib
import sys
import types
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[2]
PLUGIN = ROOT / "plugins" / "astrbot_plugin_akasha_money_receiver"


def load_plugin():
    def decorator(*_args, **_kwargs):
        def apply(function):
            return function

        return apply

    filter_stub = types.SimpleNamespace(
        EventMessageType=types.SimpleNamespace(PRIVATE_MESSAGE="private"),
        event_message_type=decorator,
        on_astrbot_loaded=decorator,
    )

    class FakeStar:
        def __init__(self, context, config=None):
            self.context = context
            self.config = config or {}

    class FakeProvider:
        def __init__(self, provider_id="vision"):
            self.provider_config = {"id": provider_id}

    fake_modules = {
        "astrbot": types.ModuleType("astrbot"),
        "astrbot.api": types.ModuleType("astrbot.api"),
        "astrbot.api.event": types.ModuleType("astrbot.api.event"),
        "astrbot.api.provider": types.ModuleType("astrbot.api.provider"),
        "astrbot.api.star": types.ModuleType("astrbot.api.star"),
    }
    fake_modules["astrbot.api"].logger = types.SimpleNamespace(
        error=lambda *_args, **_kwargs: None,
        exception=lambda *_args, **_kwargs: None,
    )
    fake_modules["astrbot.api.event"].AstrMessageEvent = type(
        "AstrMessageEvent", (), {}
    )
    fake_modules["astrbot.api.event"].filter = filter_stub
    fake_modules["astrbot.api.provider"].Provider = FakeProvider
    fake_modules["astrbot.api.star"].Context = type("Context", (), {})
    fake_modules["astrbot.api.star"].Star = FakeStar
    package_name = "_akasha_money_plugin_test"
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
    module.FakeProvider = FakeProvider
    return module


class MoneyPluginTests(unittest.TestCase):
    def test_agent_action_parser_is_strict(self):
        module = load_plugin()
        self.assertEqual(
            module._parse_agent_action('{"type":"click","x":0.4,"y":0.6}'),
            {"type": "click", "x": 0.4, "y": 0.6},
        )
        self.assertEqual(
            module._parse_agent_action(
                '{"type":"done","normal_chat":true}'
            ),
            {"type": "done", "normal_chat": True},
        )
        self.assertEqual(
            module._parse_agent_action('{"type":"reselect_contact"}'),
            {"type": "reselect_contact"},
        )
        with self.assertRaises(ValueError):
            module._parse_agent_action(
                '{"type":"done","normal_chat":false}'
            )
        with self.assertRaises(ValueError):
            module._parse_agent_action(
                '{"type":"click","x":500,"y":600}'
            )

    def test_vision_loop_uses_astrbot_provider_and_waits_for_bridge_terminal(self):
        module = load_plugin()

        class Provider(module.FakeProvider):
            def __init__(self):
                super().__init__()
                self.calls = []

            async def text_chat(self, **kwargs):
                self.calls.append(kwargs)
                return types.SimpleNamespace(
                    completion_text='{"type":"done","normal_chat":true}'
                )

        async def scenario():
            provider = Provider()
            context = types.SimpleNamespace()
            plugin = module.Main(
                context,
                {
                    "max_agent_steps": 3,
                },
            )
            plugin._provider = provider
            bridge_calls = []

            async def bridge(**kwargs):
                bridge_calls.append(kwargs)
                if kwargs["path"].endswith("/frame"):
                    return {
                        "status": "active",
                        "weflow_success": True,
                        "image_data_url": "data:image/png;base64,AAAA",
                        "frame_sha256": "a" * 64,
                        "frame_nonce": "nonce-1",
                        "target_contact": "target-contact",
                    }
                if kwargs["path"].endswith("/step"):
                    return {
                        "status": "active",
                        "visual_success": True,
                        "weflow_success": False,
                    }
                return {
                    "status": "completed",
                    "visual_success": True,
                    "weflow_success": True,
                }

            plugin._request_json = bridge
            await plugin._run_agent(
                {
                    "request_id": "request-1",
                    "token": "t" * 43,
                    "money_kind": "transfer",
                    "amount_cny": "1.00",
                    "expires_in_seconds": 30,
                }
            )
            self.assertEqual(len(provider.calls), 1)
            self.assertEqual(
                provider.calls[0]["image_urls"],
                ["data:image/png;base64,AAAA"],
            )
            self.assertIn(
                '"target-contact"',
                provider.calls[0]["prompt"],
            )
            self.assertEqual(
                [call["path"] for call in bridge_calls],
                [
                    "/api/money-action/frame",
                    "/api/money-action/step",
                    "/api/money-action/status",
                ],
            )

        asyncio.run(scenario())

    def test_done_is_retried_until_weflow_receipt_is_confirmed(self):
        module = load_plugin()

        class Provider(module.FakeProvider):
            def __init__(self):
                super().__init__()
                self.responses = [
                    '{"type":"done","normal_chat":true}',
                    '{"type":"click","x":0.5,"y":0.5}',
                    '{"type":"done","normal_chat":true}',
                ]
                self.prompts = []

            async def text_chat(self, **kwargs):
                self.prompts.append(kwargs["prompt"])
                return types.SimpleNamespace(
                    completion_text=self.responses.pop(0)
                )

        async def scenario():
            provider = Provider()
            plugin = module.Main(
                types.SimpleNamespace(),
                {"max_agent_steps": 3},
            )
            plugin._provider = provider
            frame_count = 0
            step_actions = []

            async def bridge(**kwargs):
                nonlocal frame_count
                if kwargs["path"].endswith("/frame"):
                    frame_count += 1
                    return {
                        "status": "active",
                        "weflow_success": frame_count == 3,
                        "image_data_url": "data:image/png;base64,AAAA",
                        "frame_sha256": str(frame_count) * 64,
                        "frame_nonce": f"nonce-{frame_count}",
                        "target_contact": "target-contact",
                    }
                if kwargs["path"].endswith("/step"):
                    action = kwargs["payload"]["action"]
                    step_actions.append(action)
                    return {
                        "status": (
                            "completed"
                            if action["type"] == "done"
                            else "active"
                        ),
                        "visual_success": action["type"] == "done",
                        "weflow_success": frame_count == 3,
                    }
                raise AssertionError("unexpected status poll")

            plugin._request_json = bridge
            await plugin._run_agent(
                {
                    "request_id": "request-retry",
                    "token": "t" * 43,
                    "money_kind": "red_packet",
                    "amount_cny": "",
                    "expires_in_seconds": 30,
                }
            )

            self.assertEqual(
                step_actions,
                [
                    {"type": "wait"},
                    {"type": "click", "x": 0.5, "y": 0.5},
                    {"type": "done", "normal_chat": True},
                ],
            )
            self.assertIn("尚未确认", provider.prompts[0])
            self.assertIn("已确认", provider.prompts[2])

        asyncio.run(scenario())

    def test_contact_memory_provider_is_not_selected_for_vision(self):
        module = load_plugin()
        memory = module.FakeProvider(module.CONTACT_MEMORY_PROVIDER_ID)
        vision = module.FakeProvider("vision")
        context = types.SimpleNamespace(
            get_using_provider=lambda: memory,
            get_all_providers=lambda: [memory, vision],
            get_provider_by_id=lambda _provider_id: None,
        )
        plugin = module.Main(context, {})
        self.assertIs(plugin._resolve_provider(), vision)

    def test_transfer_ignores_legacy_limit_and_extreme_amount(self):
        module = load_plugin()

        class Provider(module.FakeProvider):
            def __init__(self):
                super().__init__("vision")
                self.calls = 0

            async def text_chat(self, **_kwargs):
                self.calls += 1
                return types.SimpleNamespace(
                    completion_text='{"type":"done","normal_chat":true}'
                )

        async def scenario():
            provider = Provider()
            plugin = module.Main(
                types.SimpleNamespace(),
                {"transfer_auto_accept_max_cny": 0.0},
            )
            plugin._provider = provider
            failures = []

            async def fail(_item, reason):
                failures.append(reason)

            plugin._send_failure = fail

            async def bridge(**kwargs):
                if kwargs["path"].endswith("/frame"):
                    return {
                        "status": "active",
                        "weflow_success": True,
                        "image_data_url": "data:image/png;base64,AAAA",
                        "frame_sha256": "a" * 64,
                        "frame_nonce": "nonce-transfer",
                        "target_contact": "target-contact",
                    }
                if kwargs["path"].endswith("/step"):
                    return {
                        "status": "completed",
                        "visual_success": True,
                        "weflow_success": True,
                    }
                raise AssertionError("unexpected status poll")

            plugin._request_json = bridge
            await plugin._run_agent(
                {
                    "request_id": "request-high",
                    "token": "t" * 43,
                    "money_kind": "transfer",
                    "amount_cny": "999999999999999999999999.99",
                    "expires_in_seconds": 30,
                }
            )
            self.assertEqual(failures, [])
            self.assertEqual(provider.calls, 1)

        asyncio.run(scenario())

    def test_provider_call_cannot_outlive_transaction_budget(self):
        module = load_plugin()

        class Provider(module.FakeProvider):
            async def text_chat(self, **_kwargs):
                await asyncio.sleep(10)

        async def scenario():
            plugin = module.Main(
                types.SimpleNamespace(),
                {
                    "provider_timeout_seconds": 0.1,
                    "max_agent_steps": 1,
                },
            )
            plugin._provider = Provider()

            async def bridge(**kwargs):
                if kwargs["path"].endswith("/frame"):
                    return {
                        "status": "active",
                        "image_data_url": "data:image/png;base64,AAAA",
                        "frame_sha256": "a" * 64,
                        "frame_nonce": "nonce-timeout",
                        "target_contact": "target-contact",
                    }
                return {"status": "failed"}

            plugin._request_json = bridge
            with self.assertRaises(asyncio.TimeoutError):
                await plugin._run_agent(
                    {
                        "request_id": "request-timeout",
                        "token": "t" * 43,
                        "money_kind": "red_packet",
                        "amount_cny": "",
                        "expires_in_seconds": 30,
                    }
                )

        asyncio.run(scenario())

    def test_terminate_fails_current_and_queued_transactions(self):
        module = load_plugin()

        async def scenario():
            plugin = module.Main(types.SimpleNamespace(), {})
            current = {
                "request_id": "current",
                "token": "c" * 43,
                "money_kind": "red_packet",
            }
            pending = {
                "request_id": "pending",
                "token": "p" * 43,
                "money_kind": "red_packet",
            }
            plugin._current = current
            plugin._queued_ids.update({"current", "pending"})
            await plugin._queue.put(pending)
            failed = []

            async def fail(item, reason, **_kwargs):
                failed.append((item["request_id"], reason))

            plugin._send_failure = fail
            await plugin.terminate()
            self.assertCountEqual(
                failed,
                [
                    ("current", "plugin_terminated"),
                    ("pending", "plugin_terminated"),
                ],
            )
            self.assertTrue(plugin._queue.empty())
            self.assertEqual(plugin._queued_ids, set())

        asyncio.run(scenario())

    def test_notice_deadline_starts_before_queue_wait_and_zero_is_expired(self):
        module = load_plugin()

        class Event:
            def __init__(self, expires):
                self.stopped = False
                self.message_obj = types.SimpleNamespace(
                    raw_message={
                        "notice_type": "akasha_money_action",
                        "sub_type": "start",
                        "request_id": f"request-{expires}",
                        "capability_token": "t" * 43,
                        "money_kind": "red_packet",
                        "expires_in_seconds": expires,
                    }
                )

            def stop_event(self):
                self.stopped = True

        async def scenario():
            plugin = module.Main(types.SimpleNamespace(), {})
            failures = []

            async def fail(item, reason, **kwargs):
                failures.append((item["request_id"], reason, kwargs))

            plugin._send_failure = fail
            expired = Event(0)
            await plugin.handle_money_notice(expired)
            self.assertTrue(expired.stopped)
            self.assertEqual(plugin._queue.qsize(), 0)
            self.assertEqual(failures[0][1], "transaction_expired")

            queued = Event(0.1)
            await plugin.handle_money_notice(queued)
            item = plugin._queue.get_nowait()
            await asyncio.sleep(0.11)
            self.assertEqual(plugin._remaining(item), 0.0)
            plugin._queue.task_done()

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
