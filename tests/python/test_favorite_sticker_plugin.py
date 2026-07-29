import functools
import importlib.util
import json
import os
import pathlib
import sys
import tempfile
import types
import unittest
import uuid
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[2]
PLUGIN = ROOT / "plugins" / "astrbot_plugin_akasha_favorite_stickers"


def load_plugin():
    class ActionFailed(Exception):
        def __init__(self, result):
            super().__init__("action failed")
            self.result = result

    class FakeStar:
        def __init__(self, context, config=None):
            self.context = context
            self.config = config or {}

    class FakeFunctionTool:
        def __init__(self, *, name, description, parameters, handler):
            self.name = name
            self.description = description
            self.parameters = parameters
            self.handler = handler

    fake_modules = {
        "aiocqhttp": types.ModuleType("aiocqhttp"),
        "aiocqhttp.exceptions": types.ModuleType("aiocqhttp.exceptions"),
        "astrbot": types.ModuleType("astrbot"),
        "astrbot.api": types.ModuleType("astrbot.api"),
        "astrbot.api.event": types.ModuleType("astrbot.api.event"),
        "astrbot.api.star": types.ModuleType("astrbot.api.star"),
        "astrbot.core": types.ModuleType("astrbot.core"),
        "astrbot.core.agent": types.ModuleType("astrbot.core.agent"),
        "astrbot.core.agent.tool": types.ModuleType("astrbot.core.agent.tool"),
    }
    fake_modules["aiocqhttp.exceptions"].ActionFailed = ActionFailed
    fake_modules["astrbot.api"].logger = types.SimpleNamespace(
        info=lambda *_args, **_kwargs: None,
        warning=lambda *_args, **_kwargs: None,
    )
    fake_modules["astrbot.api.event"].AstrMessageEvent = type(
        "AstrMessageEvent",
        (),
        {},
    )
    fake_modules["astrbot.api.star"].Context = type("Context", (), {})
    fake_modules["astrbot.api.star"].Star = FakeStar
    fake_modules["astrbot.core.agent.tool"].FunctionTool = FakeFunctionTool

    package_name = "_akasha_favorite_sticker_plugin_test"
    package = types.ModuleType(package_name)
    package.__path__ = [str(PLUGIN)]
    fake_modules[package_name] = package

    catalog_spec = importlib.util.spec_from_file_location(
        f"{package_name}.catalog",
        PLUGIN / "catalog.py",
    )
    catalog = importlib.util.module_from_spec(catalog_spec)
    fake_modules[catalog_spec.name] = catalog
    main_spec = importlib.util.spec_from_file_location(
        f"{package_name}.main",
        PLUGIN / "main.py",
    )
    main = importlib.util.module_from_spec(main_spec)
    fake_modules[main_spec.name] = main
    with mock.patch.dict(sys.modules, fake_modules):
        catalog_spec.loader.exec_module(catalog)
        main_spec.loader.exec_module(main)
    return catalog, main, ActionFailed


CATALOG, PLUGIN_MAIN, ACTION_FAILED = load_plugin()


class FakeContext:
    def __init__(self):
        self.tools = []

    def add_llm_tools(self, *tools):
        self.tools.extend(tools)


class FakeBot:
    def __init__(self, result=None):
        self.result = {"confirmed": True} if result is None else result
        self.calls = []

    async def call_action(self, action, **params):
        self.calls.append((action, params))
        return self.result


class FailingBot(FakeBot):
    async def call_action(self, action, **params):
        self.calls.append((action, params))
        raise ACTION_FAILED(
            {
                "status": "failed",
                "retcode": 1404,
                "data": {
                    "error_code": "E_UIA_STICKER_TEMPLATE_MISSING",
                },
            }
        )


class FakeEvent:
    def __init__(
        self,
        *,
        private=True,
        sender_id="101",
        group_id="",
        platform="aiocqhttp",
        bot=None,
    ):
        self._private = private
        self._sender_id = sender_id
        self._group_id = group_id
        self._platform = platform
        self.bot = bot or FakeBot()
        self._extras = {}

    def is_private_chat(self):
        return self._private

    def get_sender_id(self):
        return self._sender_id

    def get_group_id(self):
        return self._group_id

    def get_platform_name(self):
        return self._platform

    def get_extra(self, key, default=None):
        return self._extras.get(key, default)

    def set_extra(self, key, value):
        self._extras[key] = value


class FavoriteStickerCatalogTests(unittest.TestCase):
    def test_catalog_is_complete_and_visible_to_the_model(self):
        entries = CATALOG.load_catalog(PLUGIN / "catalog.json")

        self.assertEqual(len(entries), 20)
        self.assertEqual(
            {entry.sticker_key for entry in entries},
            set(CATALOG.EXPECTED_STICKER_KEYS),
        )
        by_id = {entry.sticker_id: entry for entry in entries}
        self.assertEqual(by_id["wink_cat"].sticker_key, "slot_01")
        self.assertEqual(by_id["excited_grin"].sticker_key, "slot_02")
        self.assertEqual(by_id["cute_cap_plush"].sticker_key, "slot_06")
        self.assertEqual(by_id["goofy_big_eyes"].sticker_key, "slot_07")
        self.assertEqual(by_id["pink_heart_cat"].sticker_key, "slot_08")
        self.assertEqual(by_id["please_spare_me"].sticker_key, "slot_09")
        self.assertEqual(by_id["cat_heart_hands"].sticker_key, "slot_10")
        self.assertEqual(
            by_id["sparkly_pleading_cat"].sticker_key,
            "slot_11",
        )
        self.assertEqual(by_id["shark_toothy_grin"].sticker_key, "slot_12")
        self.assertEqual(by_id["silent_stare_cat"].sticker_key, "slot_13")
        self.assertEqual(by_id["sly_panda_smirk"].sticker_key, "slot_14")
        self.assertEqual(by_id["bucket_hat_smile"].sticker_key, "slot_15")
        self.assertEqual(by_id["shy_finger_tap"].sticker_key, "slot_16")
        self.assertEqual(by_id["sneaky_side_eye"].sticker_key, "slot_17")
        self.assertEqual(by_id["rosy_content_smile"].sticker_key, "slot_18")
        self.assertEqual(by_id["bashful_goofy_grin"].sticker_key, "slot_19")
        self.assertEqual(by_id["shiba_big_laugh"].sticker_key, "slot_20")
        description = CATALOG.build_tool_description(entries)
        self.assertIn("wink_cat", description)
        self.assertIn("excited_grin", description)
        self.assertNotIn("slot_21", description)

    def test_persistent_catalog_is_seeded_once_and_not_overwritten(self):
        bundled = PLUGIN / "catalog.json"
        with tempfile.TemporaryDirectory() as temporary_dir:
            persistent = CATALOG.resolve_catalog_path(
                bundled,
                temporary_dir,
            )
            payload = json.loads(persistent.read_text(encoding="utf-8"))
            payload[0]["description"] = "user-defined-description"
            persistent.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

            second = CATALOG.resolve_catalog_path(bundled, temporary_dir)

            self.assertEqual(second, persistent)
            retained = json.loads(second.read_text(encoding="utf-8"))
            self.assertEqual(
                retained[0]["description"],
                "user-defined-description",
            )

    def test_damaged_persistent_catalog_fails_closed(self):
        bundled = PLUGIN / "catalog.json"
        with tempfile.TemporaryDirectory() as temporary_dir:
            persistent = CATALOG.resolve_catalog_path(
                bundled,
                temporary_dir,
            )
            persistent.write_text("[]", encoding="utf-8")

            with self.assertRaises(CATALOG.CatalogError):
                CATALOG.load_catalog(
                    CATALOG.resolve_catalog_path(
                        bundled,
                        temporary_dir,
                    )
                )


class FavoriteStickerPluginTests(unittest.IsolatedAsyncioTestCase):
    def make_plugin(self, **config):
        context = FakeContext()
        with mock.patch.dict(
            os.environ,
            {CATALOG.STATE_DIR_ENV: ""},
        ):
            plugin = PLUGIN_MAIN.Main(
                context,
                {
                    "enabled": True,
                    "allow_private": True,
                    "allow_group": True,
                    "cooldown_seconds": 0,
                    "action_timeout_seconds": 45,
                    **config,
                },
            )
        self.assertEqual(len(context.tools), 1)
        tool = context.tools[0]
        self.assertEqual(tool.name, PLUGIN_MAIN.TOOL_NAME)
        self.assertIs(
            tool.handler,
            PLUGIN_MAIN.Main.send_wechat_favorite_sticker,
        )
        tool.handler = functools.partial(tool.handler, plugin)
        return plugin, tool

    def test_non_finite_numeric_config_falls_back_to_safe_defaults(self):
        for invalid in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(invalid=invalid):
                self.assertEqual(
                    PLUGIN_MAIN._config_float(
                        {"value": invalid},
                        "value",
                        60.0,
                        minimum=0.0,
                        maximum=3600.0,
                    ),
                    60.0,
                )
        self.assertEqual(
            PLUGIN_MAIN._config_float(
                {"value": 5},
                "value",
                45.0,
                minimum=45.0,
                maximum=180.0,
            ),
            45.0,
        )

    async def test_private_action_has_only_one_target_and_fixed_key(self):
        _, tool = self.make_plugin()
        event = FakeEvent(sender_id="123")

        result = await tool.handler(event, sticker_id="goofy_big_eyes")

        self.assertIn("已确认发送", result)
        self.assertEqual(len(event.bot.calls), 1)
        action, params = event.bot.calls[0]
        self.assertEqual(action, PLUGIN_MAIN.ACTION_NAME)
        self.assertEqual(params["user_id"], 123)
        self.assertNotIn("group_id", params)
        self.assertEqual(params["sticker_key"], "slot_07")
        self.assertEqual(
            set(params),
            {"user_id", "sticker_key", "request_id"},
        )
        uuid.UUID(params["request_id"])

    async def test_handler_survives_astrbot_loader_binding(self):
        plugin, tool = self.make_plugin()
        self.assertIsInstance(tool.handler, functools.partial)
        self.assertIs(
            tool.handler.func,
            PLUGIN_MAIN.Main.send_wechat_favorite_sticker,
        )
        self.assertEqual(tool.handler.args, (plugin,))
        event = FakeEvent(sender_id="123")
        result = await tool.handler(event, sticker_id="goofy_big_eyes")

        self.assertIn("已确认发送", result)
        self.assertEqual(len(event.bot.calls), 1)
        action, params = event.bot.calls[0]
        self.assertEqual(action, PLUGIN_MAIN.ACTION_NAME)
        self.assertEqual(params["user_id"], 123)
        self.assertEqual(params["sticker_key"], "slot_07")

    async def test_group_action_never_includes_user_id(self):
        _, tool = self.make_plugin()
        event = FakeEvent(private=False, group_id="456")

        await tool.handler(event, sticker_id="shiba_big_laugh")

        _, params = event.bot.calls[0]
        self.assertEqual(params["group_id"], 456)
        self.assertNotIn("user_id", params)
        self.assertEqual(params["sticker_key"], "slot_20")
        self.assertEqual(
            set(params),
            {"group_id", "sticker_key", "request_id"},
        )

    async def test_invalid_id_and_wrong_platform_do_not_call_bridge(self):
        _, tool = self.make_plugin()
        invalid = FakeEvent()
        wrong_platform = FakeEvent(platform="other")

        invalid_result = await tool.handler(
            invalid,
            sticker_id="../slot_01",
        )
        platform_result = await tool.handler(
            wrong_platform,
            sticker_id="wink_cat",
        )

        self.assertIn("无效", invalid_result)
        self.assertIn("aiocqhttp", platform_result)
        self.assertEqual(invalid.bot.calls, [])
        self.assertEqual(wrong_platform.bot.calls, [])

    async def test_only_one_action_is_allowed_per_event(self):
        _, tool = self.make_plugin()
        event = FakeEvent()

        first = await tool.handler(event, sticker_id="wink_cat")
        second = await tool.handler(event, sticker_id="excited_grin")

        self.assertIn("已确认发送", first)
        self.assertIn("本轮已经尝试", second)
        self.assertEqual(len(event.bot.calls), 1)

    async def test_unconfirmed_result_is_not_reported_as_success(self):
        _, tool = self.make_plugin()
        event = FakeEvent(bot=FakeBot({"confirmed": False}))

        result = await tool.handler(event, sticker_id="wink_cat")

        self.assertIn("未确认", result)
        self.assertNotIn("已确认发送", result)

    async def test_bridge_failure_is_safe_and_not_retried(self):
        _, tool = self.make_plugin()
        event = FakeEvent(bot=FailingBot())

        first = await tool.handler(event, sticker_id="wink_cat")
        second = await tool.handler(event, sticker_id="wink_cat")

        self.assertIn("E_UIA_STICKER_TEMPLATE_MISSING", first)
        self.assertIn("本轮已经尝试", second)
        self.assertEqual(len(event.bot.calls), 1)

    async def test_cooldown_is_scoped_to_the_target(self):
        _, tool = self.make_plugin(cooldown_seconds=60)
        first_event = FakeEvent(sender_id="101")
        same_target = FakeEvent(sender_id="101")
        other_target = FakeEvent(sender_id="102")

        await tool.handler(first_event, sticker_id="wink_cat")
        blocked = await tool.handler(
            same_target,
            sticker_id="excited_grin",
        )
        allowed = await tool.handler(
            other_target,
            sticker_id="excited_grin",
        )

        self.assertIn("冷却期", blocked)
        self.assertEqual(same_target.bot.calls, [])
        self.assertIn("已确认发送", allowed)
        self.assertEqual(len(other_target.bot.calls), 1)


if __name__ == "__main__":
    unittest.main()
