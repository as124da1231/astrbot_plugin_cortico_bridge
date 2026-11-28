"""策略/配置/面板后端回归（A-1..A-9、T-001/T-004、UI-002/005/006/008）。

main.py 依赖较多 AstrBot 层；这里和 test_images 一样只 stub 到 import 需要的
部分，再用 object.__new__ 绕开插件完整初始化。
"""

import asyncio
import base64
import contextlib
import importlib.util
import json
import sys
import time
import types
import unittest
from collections import OrderedDict, deque
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))


class _Logger:
    def __getattr__(self, _name):
        return lambda *args, **kwargs: None


astrbot = types.ModuleType("astrbot")
api = types.ModuleType("astrbot.api")
api.logger = _Logger()
api.AstrBotConfig = dict
astrbot.api = api

event_mod = types.ModuleType("astrbot.api.event")


class _MessageChain:
    def __init__(self):
        self.chain = []

    def append(self, item):
        self.chain.append(item)


class _Filter:
    class EventMessageType:
        ALL = "ALL"

    @staticmethod
    def on_astrbot_loaded(*args, **kwargs):
        return lambda fn: fn

    @staticmethod
    def event_message_type(*args, **kwargs):
        return lambda fn: fn

    @staticmethod
    def on_llm_request(*args, **kwargs):
        return lambda fn: fn


event_mod.MessageChain = _MessageChain
event_mod.AstrMessageEvent = object
event_mod.filter = _Filter()
api.event = event_mod

star_mod = types.ModuleType("astrbot.api.star")


class _Star:
    def __init__(self, context=None):
        self.context = context


class _StarTools:
    @staticmethod
    def get_data_dir(_name):
        raise RuntimeError("test has no data dir")


star_mod.Context = object
star_mod.Star = _Star
star_mod.StarTools = _StarTools
api.star = star_mod

sys.modules["astrbot"] = astrbot
sys.modules["astrbot.api"] = api
sys.modules["astrbot.api.event"] = event_mod
sys.modules["astrbot.api.star"] = star_mod

# 不依赖仓库目录名：把插件根目录注册为合成包 `plugin`
spec = importlib.util.spec_from_loader("plugin", None, is_package=True)
spec.submodule_search_locations = [str(ROOT)]
sys.modules.setdefault("plugin", importlib.util.module_from_spec(spec))

from plugin import main as main_mod  # noqa: E402
from plugin import owner_event  # noqa: E402


class CommandFilter:
    pass


class CommandGroupFilter:
    pass


class RegexFilter:
    pass


class SessionServiceManager:
    @staticmethod
    async def should_process_llm_request(event):
        return event.session_llm_enabled


for native_type, native_module in (
    (CommandFilter, "astrbot.core.star.filter.command"),
    (CommandGroupFilter, "astrbot.core.star.filter.command_group"),
    (RegexFilter, "astrbot.core.star.filter.regex"),
    (SessionServiceManager, "astrbot.core.star.session_llm_manager"),
):
    stub = types.ModuleType(native_module)
    setattr(stub, native_type.__name__, native_type)
    sys.modules[native_module] = stub


class FakeConfig(dict):
    def __init__(self, data=None, fail=False):
        super().__init__(data or {})
        self.fail = fail
        self.saves = 0

    def save_config(self):
        self.saves += 1
        if self.fail:
            raise RuntimeError("disk full")


class At:
    def __init__(self, qq):
        self.qq = qq


class Reply:
    def __init__(self, sender_id):
        self.sender_id = sender_id


class Plain:
    def __init__(self, text):
        self.text = text


class Image:
    def __init__(self, url="https://example.com/cat.png"):
        self.url = url

    async def convert_to_base64(self):
        return "iVBORw0KGgoAAAANSUhEUg"


class FakeEvent:
    def __init__(
        self,
        text="",
        messages=None,
        *,
        private=False,
        group_id="1",
        sender_id="100",
        self_id="999",
        message_id=None,
        timestamp=100,
        raw=None,
    ):
        self._text = text
        self._messages = messages if messages is not None else []
        self._private = private
        self._group_id = group_id
        self._sender_id = sender_id
        self._self_id = self_id
        self._extras = {}
        self._stopped = False
        self._has_send_oper = False
        self.session_llm_enabled = True
        self.is_at_or_wake_command = False
        self.is_wake = False
        self.unified_msg_origin = (
            f"default:{'FriendMessage' if private else 'GroupMessage'}:{group_id}"
        )
        self.message_obj = types.SimpleNamespace(raw_message=raw, time=timestamp)
        if message_id is not None:
            self.message_obj.message_id = message_id

    def get_extra(self, key, default=None):
        return self._extras.get(key, default)

    def set_extra(self, key, value):
        self._extras[key] = value

    def get_message_str(self):
        return self._text

    def get_messages(self):
        return self._messages

    def is_private_chat(self):
        return self._private

    def get_group_id(self):
        return "" if self._private else self._group_id

    def get_sender_id(self):
        return self._sender_id

    def get_sender_name(self):
        return f"用户{self._sender_id}"

    def get_self_id(self):
        return self._self_id

    def get_platform_id(self):
        return "default"

    async def get_group(self):
        return types.SimpleNamespace(group_name="测试群")

    def is_stopped(self):
        return self._stopped

    def stop_event(self):
        self._stopped = True


class FakeServer:
    mode = "full"
    has_client = False
    operations_active = False

    def __init__(self, connected=True, push_ok=True):
        self.connected = connected
        self.push_ok = push_ok
        self.pushed = []
        self.capabilities = {"images": True}
        self.self_id = "999"
        self.host = "0.0.0.0"
        self.port = 17999
        self.token = "old-token"
        self.tool_owner = "111"
        self.listening = False
        self.listen_error = ""
        self.client_address = ""
        self.connected_at = time.time()
        self.last_frame_at = time.time()
        self.origin_refused = 0
        self.query_token_refused = 0
        self.bad_token_refused = 0
        self.version_refused = 0
        self.approval = ""
        self.approved = []

    def pending_mode_approval(self):
        return self.approval

    def revise_permissions(self):
        self.approval = ""

    async def approve_mode(self, approval):
        if not approval or approval != self.approval:
            raise ValueError("切换请求已过期")
        self.approved.append(approval)

    async def push(self, frame):
        self.pushed.append(frame)
        return self.push_ok

    async def offer_message(self, frame):
        return "accepted" if await self.push(frame) else "declined"

    async def push_ready(self):
        return True

    async def stop(self):
        self.listening = False

    async def start(self):
        self.listening = True
        return True


class FakeDelivery:
    def __init__(self, conversations=None, result=(True, "", True, False, "")):
        self.conversations = conversations or [
            {"key": "group:1", "umo": "default:GroupMessage:1"}
        ]
        self.result = result
        self.sent = []

    def known_conversations(self):
        return self.conversations

    def has_umo(self, umo):
        return any(row.get("umo") == umo for row in self.conversations)

    def remember(self, *args, **kwargs):
        return None

    async def send(self, *args, **kwargs):
        self.sent.append((args, kwargs))
        return self.result


class FakeTools:
    def __init__(self, allow=None, catalog=None, enabled=None):
        self.allow = allow if allow is not None else ["*"]
        self.rows = catalog or []
        self.active_rows = enabled if enabled is not None else []
        self.executed = []
        self.frames = []

    def allow_list(self):
        return list(self.allow)

    def catalog(self, force=False):
        return list(self.rows)

    def enabled_catalog(self, force=False):
        return list(self.active_rows)

    def frame(self):
        return {"t": "tools", "items": list(self.active_rows)}

    async def execute(self, name, args, event, trusted=False):
        self.executed.append((name, args, trusted))
        return True, "ok"


class FakeContext:
    def __init__(self, admins=None):
        self.admins = admins if admins is not None else ["111"]

    def get_config(self, *, umo=None):
        return {"admins_id": list(self.admins), "wake_prefix": "/"}


class FakeRequest:
    def __init__(self, method, payload):
        self.method = method
        self.payload = payload

    async def json(self, default=None):
        return self.payload


DEFAULTS = {
    "enabled": True,
    "group_scope": "all",
    "group_mode": "mention",
    "group_allow": [],
    "bridge_platforms": ["default"],
    "private_mode": "all",
    "private_deny": "ignore",
    "name_aliases": ["Coo", "库"],
    "image_mode": "auto",
    "tts_enabled": False,
    "token": "secret",
    "tools_allow": ["*"],
}


def make_bridge(
    config=None,
    *,
    connected=True,
    push_ok=True,
    delivery=None,
    tools=None,
    context=None,
):
    bridge = object.__new__(main_mod.CorticoBridge)
    data = dict(DEFAULTS)
    if config:
        data.update(config)
    bridge.config = FakeConfig(data)
    bridge.context = context or FakeContext()
    bridge.server = FakeServer(connected=connected, push_ok=push_ok)
    bridge._listener_lock = asyncio.Lock()
    bridge.delivery = delivery or FakeDelivery()
    bridge.tools = tools or FakeTools()
    bridge._recent = deque(maxlen=40)
    bridge._tool_log = deque(maxlen=12)
    bridge._turns = OrderedDict()
    bridge._message_segments = {}
    bridge._forward_pages = OrderedDict()
    bridge._seen = OrderedDict()
    bridge._fallback_seen = OrderedDict()
    bridge._msg_seq = 0
    bridge._subscribers = set()
    bridge._names = OrderedDict()
    bridge._image_cache = OrderedDict()
    bridge._vision_logged = ""
    bridge.started_at = time.time()
    bridge.stats = {
        "pushed": 0,
        "sent": 0,
        "failed": 0,
        "skipped": 0,
        "vision": 0,
        "voices": 0,
        "images": 0,
    }
    return bridge


@contextlib.contextmanager
def fake_request(method, payload=None):
    old = main_mod.request
    main_mod.request = FakeRequest(method, payload if payload is not None else {})
    try:
        yield
    finally:
        main_mod.request = old


@contextlib.contextmanager
def fake_responses():
    old_json, old_error = main_mod.json_response, main_mod.error_response
    main_mod.json_response = lambda data, **kwargs: {"status": 200, "data": data}
    main_mod.error_response = lambda msg, status_code=400, **kwargs: {
        "status": status_code,
        "error": msg,
    }
    try:
        yield
    finally:
        main_mod.json_response, main_mod.error_response = old_json, old_error


class CommandAdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_command_filters_are_excluded_before_binding_and_delivery(self):
        for command_type in (CommandFilter, CommandGroupFilter, RegexFilter):
            for private, mode in ((False, "mention"), (False, "all"), (True, "all")):
                with self.subTest(command=command_type.__name__, private=private, mode=mode):
                    bridge = make_bridge({"group_mode": mode})
                    event = FakeEvent("custom alias private-argument", messages=[At("999")], private=private)
                    event.set_extra("activated_handlers", [types.SimpleNamespace(event_filters=[command_type()])])
                    with mock.patch.object(bridge, "_bind_instance_owner") as bind, mock.patch.object(main_mod, "logger") as log:
                        await bridge.observe_all_messages(event)
                        await bridge.on_llm_request(event, None)
                    bind.assert_not_called()
                    self.assertNotIn("private-argument", str(log.mock_calls))
                    self.assertFalse(event.get_extra("_cortico_llm_owned"))
                    self.assertFalse(event.is_stopped())
                    self.assertEqual(bridge.server.pushed, [])
                    self.assertFalse(bridge._turns)
                    self.assertFalse(bridge._recent)

    async def test_structured_slash_interaction_is_excluded_without_handler_metadata(self):
        bridge = make_bridge({"group_mode": "all"})
        event = FakeEvent("reset", messages=[At("999")])
        event.is_slash_command = lambda: True
        await bridge.observe_all_messages(event)
        await bridge.on_llm_request(event, None)
        self.assertFalse(event.get_extra("_cortico_llm_owned"))
        self.assertEqual(bridge.server.pushed, [])

    async def test_unmatched_explicit_prefix_uses_the_current_umo_profile(self):
        class PrefixedContext(FakeContext):
            def get_config(self, *, umo=None):
                config = super().get_config(umo=umo)
                if umo == "default:GroupMessage:1":
                    config["wake_prefix"] = ["!", "::"]
                return config

        bridge = make_bridge({"group_mode": "all"}, context=PrefixedContext())
        for index, (group, raw) in enumerate((("1", "!typo arg"), ("1", " ::typo arg"), ("2", "/typo arg"))):
            with self.subTest(group=group, raw=raw):
                event = FakeEvent("typo arg", [Plain(raw)], group_id=group, message_id=str(index))
                await bridge.observe_all_messages(event)
                await bridge.on_llm_request(event, None)
                self.assertFalse(event.get_extra("_cortico_llm_owned"))
        self.assertEqual(bridge.server.pushed, [])

        ordinary = FakeEvent("/some/path", [Plain("/some/path")], group_id="1", message_id="4")
        await bridge.observe_all_messages(ordinary)
        await bridge.on_llm_request(ordinary, None)
        self.assertEqual([frame["text"] for frame in bridge.server.pushed], ["/some/path"])

    async def test_chat_with_unrelated_listener_uses_a_single_admission_decision(self):
        for private, mode in ((False, "mention"), (False, "all"), (True, "all")):
            with self.subTest(private=private, mode=mode):
                bridge = make_bridge({"group_mode": mode})
                event = FakeEvent("help me read this", [At("999"), Plain("help me read this")], private=private)
                event.set_extra("activated_handlers", [types.SimpleNamespace(event_filters=[object()])])
                with mock.patch.object(bridge, "_astrbot_owns_command", wraps=bridge._astrbot_owns_command) as admission:
                    await bridge.observe_all_messages(event)
                    await bridge.on_llm_request(event, None)
                admission.assert_called_once()
                frame, = bridge.server.pushed
                self.assertEqual(frame["text"], "help me read this")
                self.assertEqual(frame["umo"], event.unified_msg_origin)

    async def test_an_earlier_plugin_can_stop_or_reply_without_the_bridge_taking_over(self):
        for key in ("stopped", "sent"):
            with self.subTest(previous_plugin=key):
                bridge = make_bridge()
                event = FakeEvent("Coo hello", [At("999"), Plain("Coo hello")])
                if key == "stopped":
                    event.stop_event()
                else:
                    event._has_send_oper = True
                await bridge.observe_all_messages(event)
                await bridge.on_llm_request(event, None)
                self.assertFalse(event.get_extra("_cortico_llm_owned"))
                self.assertEqual(bridge.server.pushed, [])

    async def test_mute_and_reopen_are_scoped_to_the_umo(self):
        bridge = make_bridge()
        muted = FakeEvent("Coo hello", [At("999")], group_id="1", message_id="101")
        muted.session_llm_enabled = False
        other = FakeEvent("Coo hello", [At("999")], group_id="2", message_id="102")
        await bridge.observe_all_messages(muted)
        await bridge.on_llm_request(muted, None)
        await bridge.observe_all_messages(other)
        self.assertFalse(muted.get_extra("_cortico_llm_owned"))
        self.assertEqual([frame["umo"] for frame in bridge.server.pushed], [other.unified_msg_origin])

        muted.session_llm_enabled = True
        await bridge.observe_all_messages(muted)
        await bridge.on_llm_request(muted, None)
        self.assertEqual([frame["umo"] for frame in bridge.server.pushed], [other.unified_msg_origin, muted.unified_msg_origin])

    async def test_llm_hook_rechecks_mute_between_the_two_hooks(self):
        for private in (False, True):
            with self.subTest(private=private):
                bridge = make_bridge({"group_mode": "all"})
                event = FakeEvent("hello", [Plain("hello")], private=private, message_id="201")
                await bridge.observe_all_messages(event)
                self.assertTrue(event.get_extra("_cortico_llm_owned"))
                event.session_llm_enabled = False
                await bridge.on_llm_request(event, None)
                self.assertEqual(bridge.server.pushed, [])
                self.assertFalse(bridge._turns)
                self.assertFalse(bridge._seen)
                self.assertFalse(event.is_stopped())

    async def test_close_while_preparing_media_drops_the_frame_and_allows_reopen(self):
        context = FakeContext()
        profile = {"enable": True}
        context.get_config = lambda *, umo=None: {"provider_settings": profile, "wake_prefix": ["/"]}
        for switch in ("profile", "session"):
            with self.subTest(switch=switch):
                bridge = make_bridge(context=context)
                event = FakeEvent("Coo hello", [At("999")], message_id="301")

                async def mute_during_media(*args):
                    if switch == "profile":
                        profile["enable"] = False
                    else:
                        event.session_llm_enabled = False
                    return [], [], []

                with mock.patch.object(bridge, "_image_payloads", side_effect=mute_during_media):
                    await bridge.observe_all_messages(event)
                self.assertEqual(bridge.server.pushed, [])
                self.assertFalse(bridge._turns)
                self.assertFalse(bridge._message_segments)
                self.assertFalse(bridge._seen)
                self.assertFalse(event.is_stopped())

                profile["enable"] = True
                event.session_llm_enabled = True
                await bridge.observe_all_messages(event)
                await bridge.on_llm_request(event, None)
                self.assertEqual(len(bridge.server.pushed), 1)

    async def test_already_accepted_turn_stays_available_after_close(self):
        bridge = make_bridge()
        earlier = FakeEvent("Coo earlier", [At("999")], message_id="401")
        await bridge.observe_all_messages(earlier)
        later = FakeEvent("Coo later", [At("999")], message_id="402")
        later.session_llm_enabled = False
        await bridge.observe_all_messages(later)
        frame, = bridge.server.pushed
        self.assertEqual(frame["id"], "401")
        self.assertEqual(set(bridge._turns), {(earlier.unified_msg_origin, "401")})

    async def test_profile_is_read_after_waiting_for_native_session_state(self):
        context = FakeContext()
        profile = {"enable": True}
        context.get_config = lambda *, umo=None: {"provider_settings": profile, "wake_prefix": ["/"]}
        bridge = make_bridge({"group_mode": "all"}, context=context)
        event = FakeEvent("hello", [Plain("hello")], message_id="450")
        await bridge.observe_all_messages(event)

        async def get_session_state(_event):
            await asyncio.sleep(0)
            profile["enable"] = False
            return True

        with mock.patch.object(SessionServiceManager, "should_process_llm_request", side_effect=get_session_state):
            await bridge.on_llm_request(event, None)
        self.assertEqual(bridge.server.pushed, [])
        self.assertFalse(bridge._turns)

    async def test_llm_command_and_next_message_have_separate_outcomes(self):
        context = FakeContext()
        profile = {"enable": True}
        context.get_config = lambda *, umo=None: {"provider_settings": profile, "wake_prefix": ["/"]}
        bridge = make_bridge(context=context)
        earlier = FakeEvent("Coo earlier", [At("999")], message_id="501")
        await bridge.observe_all_messages(earlier)

        cmd = FakeEvent("llm", [Plain("/llm")], message_id="502")
        cmd._has_send_oper = True
        cmd.set_extra("activated_handlers", [types.SimpleNamespace(event_filters=[CommandFilter()])])
        profile["enable"] = False  # AstrBot 的 /llm 指令已先执行。
        await bridge.observe_all_messages(cmd)
        await bridge.on_llm_request(cmd, None)
        later = FakeEvent("Coo later", [At("999")], message_id="503")
        await bridge.observe_all_messages(later)
        self.assertEqual([frame["id"] for frame in bridge.server.pushed], ["501"])
        self.assertFalse(bridge._recent[-1]["notice"])
        self.assertIn((earlier.unified_msg_origin, "501"), bridge._turns)

        profile["enable"] = True
        await bridge.observe_all_messages(later)
        await bridge.on_llm_request(later, None)
        self.assertEqual([frame["id"] for frame in bridge.server.pushed], ["501", "503"])


class MentionPolicyTests(unittest.IsolatedAsyncioTestCase):
    async def test_mention_at_self_pushes_once_and_llm_does_not_repeat(self):
        bridge = make_bridge()
        event = FakeEvent(
            "你好", messages=[At("999"), Plain("你好")], sender_id="100"
        )
        await bridge.observe_all_messages(event)

        self.assertEqual(len(bridge.server.pushed), 1)
        self.assertTrue(event.is_at_or_wake_command)
        self.assertEqual(event.get_extra("_cortico_llm_owned"), "group")
        self.assertTrue(event.get_extra("_cortico_pushed"))
        self.assertTrue(event.is_stopped())
        self.assertTrue(bridge.server.pushed[0]["mentioned"])

        # 直接推送成功后，LLM 层不再推第二次。
        await bridge.on_llm_request(event, None)
        self.assertEqual(len(bridge.server.pushed), 1)

    async def test_mention_respects_effective_llm_config_for_each_group(self):
        class GroupConfigContext(FakeContext):
            def __init__(self):
                super().__init__()
                self.group_config = {"enable": False}

            def get_config(self, *, umo=None):
                config = super().get_config(umo=umo)
                config["provider_settings"] = (
                    self.group_config if umo == "default:GroupMessage:1" else {"enable": True}
                )
                return config

        context = GroupConfigContext()
        bridge = make_bridge(context=context)
        muted = FakeEvent("你好", messages=[At("999"), Plain("你好")], message_id="1001")
        await bridge.observe_all_messages(muted)
        await bridge.on_llm_request(muted, None)
        self.assertEqual(bridge.server.pushed, [])
        self.assertFalse(muted.get_extra("_cortico_llm_owned"))
        self.assertFalse(muted.is_stopped())

        other = FakeEvent("你好", messages=[At("999"), Plain("你好")], group_id="2", message_id="1002")
        await bridge.observe_all_messages(other)
        self.assertEqual(len(bridge.server.pushed), 1)

        context.group_config["enable"] = True
        resumed = FakeEvent("你好", messages=[At("999"), Plain("你好")], message_id="1003")
        await bridge.observe_all_messages(resumed)
        await bridge.on_llm_request(resumed, None)
        self.assertEqual(len(bridge.server.pushed), 2)

    async def test_mention_alias_wakes(self):
        bridge = make_bridge()
        event = FakeEvent("库，在吗", messages=[Plain("库，在吗")])
        await bridge.observe_all_messages(event)
        self.assertEqual(len(bridge.server.pushed), 1)
        self.assertEqual(event.get_extra("_cortico_llm_owned"), "group")

    async def test_mention_unrelated_text_is_not_owned(self):
        bridge = make_bridge()
        event = FakeEvent("今天天气不错", messages=[Plain("今天天气不错")])
        await bridge.observe_all_messages(event)
        self.assertEqual(bridge.server.pushed, [])
        self.assertFalse(event.get_extra("_cortico_llm_owned"))

    async def test_mention_group_not_allowed_does_not_wake(self):
        bridge = make_bridge({"group_scope": "allowlist", "group_allow": ["other"]})
        event = FakeEvent("你好", messages=[At("999")])
        await bridge.observe_all_messages(event)
        self.assertEqual(bridge.server.pushed, [])
        self.assertFalse(event.get_extra("_cortico_llm_owned"))
        self.assertFalse(event.is_at_or_wake_command)

    async def test_at_all_is_not_wake_and_mentioned_false(self):
        bridge = make_bridge()
        event = FakeEvent("大家好", messages=[At("all")])
        await bridge.observe_all_messages(event)
        self.assertEqual(bridge.server.pushed, [])
        self.assertFalse(event.get_extra("_cortico_llm_owned"))
        self.assertFalse(bridge._is_called(event, "大家好"))

    async def test_all_mode_stays_on_llm_path(self):
        bridge = make_bridge({"group_mode": "all"})
        event = FakeEvent("随便聊聊", messages=[Plain("随便聊聊")])
        await bridge.observe_all_messages(event)
        self.assertEqual(bridge.server.pushed, [])
        self.assertEqual(event.get_extra("_cortico_llm_owned"), "group")

        await bridge.on_llm_request(event, None)
        self.assertEqual(len(bridge.server.pushed), 1)

    async def test_disconnected_bridge_does_not_set_owned(self):
        bridge = make_bridge(connected=False)
        event = FakeEvent("Coo 你好", messages=[Plain("Coo 你好")])
        await bridge.observe_all_messages(event)
        self.assertFalse(event.get_extra("_cortico_llm_owned"))
        self.assertEqual(bridge.server.pushed, [])

    async def test_command_is_left_to_astrbot(self):
        bridge = make_bridge()
        event = FakeEvent("/help", messages=[Plain("/help")])
        await bridge.observe_all_messages(event)
        self.assertFalse(event.get_extra("_cortico_llm_owned"))
        self.assertEqual(bridge.server.pushed, [])

    async def test_push_failure_allows_llm_retry_once(self):
        bridge = make_bridge(push_ok=False)
        event = FakeEvent("Coo 你好", messages=[Plain("Coo 你好")])
        await bridge.observe_all_messages(event)
        self.assertEqual(len(bridge.server.pushed), 1)
        self.assertFalse(event.get_extra("_cortico_pushed"))
        self.assertFalse(event.is_stopped())

        bridge.server.push_ok = True
        await bridge.on_llm_request(event, None)
        self.assertEqual(len(bridge.server.pushed), 2)
        self.assertTrue(event.get_extra("_cortico_pushed"))

    async def test_push_success_survives_ui_record_failure(self):
        bridge = make_bridge()

        class ExplodingRecent(deque):
            def append(self, item):
                raise RuntimeError("ui boom")

        bridge._recent = ExplodingRecent(maxlen=40)
        event = FakeEvent("Coo 你好", messages=[Plain("Coo 你好")])
        with self.assertRaises(RuntimeError):
            await bridge.observe_all_messages(event)
        self.assertEqual(len(bridge.server.pushed), 1)
        self.assertTrue(event.get_extra("_cortico_pushed"))

        # 置位发生在 UI 记录之前，所以 LLM 层不会补推。
        await bridge.on_llm_request(event, None)
        self.assertEqual(len(bridge.server.pushed), 1)


class NoticePolicyTests(unittest.IsolatedAsyncioTestCase):
    def _event(self):
        raw = {
            "post_type": "notice",
            "notice_type": "group_recall",
            "user_id": "100",
            "operator_id": "100",
            "message_id": "9",
        }
        return FakeEvent("", raw=raw, group_id="1")

    async def test_disconnected_notice_is_not_stopped(self):
        bridge = make_bridge(connected=False)
        event = self._event()
        await bridge.observe_all_messages(event)
        self.assertFalse(event.is_stopped())
        self.assertEqual(bridge.server.pushed, [])

    async def test_connected_notice_is_pushed_and_stopped(self):
        bridge = make_bridge()
        event = self._event()
        await bridge.observe_all_messages(event)
        self.assertTrue(event.is_stopped())
        self.assertEqual(len(bridge.server.pushed), 1)
        self.assertEqual(len(bridge._recent), 1)


class MessageIdTests(unittest.IsolatedAsyncioTestCase):
    async def test_fallback_ids_are_unique_for_same_text(self):
        bridge = make_bridge()
        event = FakeEvent("在吗", messages=[Plain("在吗")], timestamp=100)
        ids = {bridge._message_id(event) for _ in range(5)}
        self.assertEqual(len(ids), 5)

    async def test_fallback_dedupes_only_within_three_seconds(self):
        bridge = make_bridge()
        event = FakeEvent("在吗", messages=[Plain("在吗")], timestamp=100)
        key = bridge._fallback_key(event)
        self.assertFalse(bridge._is_fallback_duplicate(key))
        self.assertTrue(bridge._is_fallback_duplicate(key))

        later = FakeEvent("在吗", messages=[Plain("在吗")], timestamp=130)
        self.assertFalse(bridge._is_fallback_duplicate(bridge._fallback_key(later)))

    async def test_same_second_same_text_two_messages_both_push(self):
        bridge = make_bridge()
        # 同一秒、同一发送者、同一句正文：没有 message_id 也必须是两条。
        first = FakeEvent("在吗", messages=[Plain("在吗")], timestamp=100)
        second = FakeEvent("在吗", messages=[Plain("在吗")], timestamp=100)
        await bridge._push_event(first)
        await bridge._push_event(second)
        self.assertEqual(len(bridge.server.pushed), 2)
        self.assertEqual(
            len({frame["id"] for frame in bridge.server.pushed}), 2
        )

    async def test_same_second_two_different_images_both_push(self):
        bridge = make_bridge()
        first = FakeEvent(
            "", messages=[Image("https://example.com/a.png")], timestamp=100
        )
        second = FakeEvent(
            "", messages=[Image("https://example.com/b.png")], timestamp=100
        )
        await bridge._push_event(first)
        await bridge._push_event(second)
        self.assertEqual(len(bridge.server.pushed), 2)
        self.assertEqual(
            len({frame["id"] for frame in bridge.server.pushed}), 2
        )

    async def test_same_event_redelivery_within_three_seconds_pushes_once(self):
        bridge = make_bridge()
        event = FakeEvent("在吗", messages=[Plain("在吗")], timestamp=100)
        await bridge._push_event(event)
        await bridge._push_event(event)
        self.assertEqual(len(bridge.server.pushed), 1)
        self.assertEqual(bridge.stats["skipped"], 1)

    async def test_raw_message_id_dedupes_across_event_objects(self):
        bridge = make_bridge()
        raw = {"message_id": "m-42", "message": []}
        first = FakeEvent("在吗", messages=[Plain("在吗")], timestamp=100, raw=raw)
        second = FakeEvent("在吗", messages=[Plain("在吗")], timestamp=100, raw=raw)
        self.assertEqual(bridge._fallback_key(first), bridge._fallback_key(second))
        await bridge._push_event(first)
        await bridge._push_event(second)
        self.assertEqual(len(bridge.server.pushed), 1)

    async def test_real_platform_id_still_dedupes(self):
        bridge = make_bridge()
        event = FakeEvent("在吗", message_id="m1")
        self.assertTrue(bridge._has_platform_id(event))
        self.assertFalse(bridge._is_duplicate("m1"))
        self.assertTrue(bridge._is_duplicate("m1"))


class CommitConfigTests(unittest.IsolatedAsyncioTestCase):
    async def test_groups_save_failure_rolls_back(self):
        bridge = make_bridge()
        bridge.config.fail = True
        before = dict(bridge.config)
        with fake_responses(), fake_request(
            "POST", {"scope": "allowlist", "groups": ["123"], "mode": "mention"}
        ):
            result = await bridge.page_groups()
        self.assertEqual(result["status"], 500)
        self.assertEqual(dict(bridge.config), before)

    async def test_voice_invalid_max_chars_does_not_touch_tts_enabled(self):
        bridge = make_bridge({"tts_enabled": False})
        with fake_responses(), fake_request(
            "POST", {"enabled": True, "max_chars": "abc"}
        ):
            result = await bridge.page_voice()
        self.assertEqual(result["status"], 400)
        self.assertFalse(bridge.config["tts_enabled"])

    async def test_token_save_failure_keeps_server_token(self):
        bridge = make_bridge()
        bridge.server.token = "old-token"
        bridge.config.fail = True
        with fake_responses(), fake_request("POST", {"action": "generate"}):
            result = await bridge.page_token()
        self.assertEqual(result["status"], 500)
        self.assertEqual(bridge.server.token, "old-token")
        self.assertEqual(bridge.config["token"], "secret")

    async def test_groups_mirror_keeps_session_colon(self):
        bridge = make_bridge()
        with fake_responses(), fake_request(
            "POST",
            {
                "scope": "allowlist",
                "groups": ["default:GroupMessage:123:456"],
                "mode": "mention",
            },
        ):
            result = await bridge.page_groups()
        self.assertEqual(result["status"], 200)
        self.assertEqual(bridge.config["group_allow"], ["default:GroupMessage:123:456"])
        self.assertEqual(bridge.config["observe_groups"], ["123:456"])

    async def test_groups_without_payload_groups_preserves_old_list(self):
        bridge = make_bridge()
        bridge.config["group_allow"] = ["default:GroupMessage:1"]
        with fake_responses(), fake_request("POST", {"scope": "all", "mode": "mention"}):
            result = await bridge.page_groups()
        self.assertEqual(result["status"], 200)
        self.assertEqual(bridge.config["group_allow"], ["default:GroupMessage:1"])
        self.assertEqual(result["data"]["groups"], ["default:GroupMessage:1"])

    async def test_normalize_scope_accepts_platform(self):
        bridge = make_bridge({"bridge_platforms": ["default", "qq2"]})
        out = bridge._normalize_scope_items(["123"], "group", "qq2")
        self.assertEqual(out, ["qq2:GroupMessage:123"])


class MessageComponentTests(unittest.IsolatedAsyncioTestCase):
    async def test_voice_file_is_returned_without_transcription(self):
        bridge = make_bridge({"stt_enabled": False})
        bridge.server.capabilities.update(message_segments=True, component_audio=True)
        source_url = "https://multimedia.nt.qq.com.cn/voice?token=fixture"
        event = FakeEvent("", message_id="voice", raw={"message": [
            {"type": "record", "data": {"url": source_url}},
        ]})
        voice = b"\x02#!SILK_V3\x00\x00"
        bridge._transcribe = mock.AsyncMock()
        self.assertTrue(await bridge._push_event(event))
        with mock.patch.object(main_mod, "download_qq_media", new_callable=mock.AsyncMock, return_value=voice) as download:
            ok, result = await bridge._on_tool_call(
                main_mod.READ_COMPONENT_TOOL, {"message_id": "voice", "path": [1]},
                "voice", scope=event.unified_msg_origin,
            )
        self.assertTrue(ok)
        download.assert_awaited_once_with(source_url, 6 * 1024 * 1024)
        bridge._transcribe.assert_not_awaited()
        self.assertIn("未开启语音转写", result.text)
        self.assertNotIn(source_url, result.text)
        self.assertEqual(result.audio["mime"], "audio/silk")
        self.assertEqual(base64.b64decode(result.audio["base64"]), voice)

    async def test_voice_file_only_preserves_a_transcript_without_new_transcription(self):
        bridge = make_bridge({"stt_enabled": True})
        bridge.server.capabilities["component_audio"] = True
        bridge._transcribe = mock.AsyncMock()
        event = FakeEvent("声音")
        with mock.patch.object(main_mod, "download_qq_media", new_callable=mock.AsyncMock, return_value=b"#!AMR\nfixture"):
            result = await bridge._read_voice({"fields": {"text": "已经转写"}}, "fixture", event, file_only=True)
        self.assertIn("已经转写", result.text)
        self.assertEqual(result.audio["mime"], "audio/amr")
        bridge._transcribe.assert_not_awaited()

    async def test_forward_media_uses_the_selected_path_without_exposing_urls(self):
        bridge = make_bridge()
        bridge.server.capabilities.update(message_segments=True, component_media=True)
        url = "https://multimedia.nt.qq.com.cn/download?token=fixture"
        event = FakeEvent("看看这段", message_id="outer", raw={"message": [
            {"type": "forward", "data": {"id": "forward"}},
        ]})
        event.bot = types.SimpleNamespace(call_action=mock.AsyncMock(return_value={"messages": [{
            "sender": {"nickname": "转发作者"},
            "message": [
                {"type": "image", "data": {"url": url}},
                {"type": "record", "data": {"url": "http://127.0.0.1/private"}},
            ],
        }]}))
        bridge._read_deferred_media = mock.AsyncMock(return_value=main_mod.ComponentReceipt(
            "[图片]", [{"mime": "image/png", "base64": "AQID"}]
        ))
        await bridge._push_event(event)
        async def read(path):
            return await bridge._on_tool_call(
                main_mod.READ_COMPONENT_TOOL, {"message_id": "outer", "path": path}, "outer", scope=event.unified_msg_origin
            )

        ok, page = await read([1])
        self.assertTrue(ok)
        self.assertIn("1.1.1 [图片]", page)
        self.assertIn("1.1.2 [语音]", page)
        self.assertNotIn(url, page)
        self.assertNotIn("127.0.0.1", page)
        bridge._read_deferred_media.assert_not_called()
        ok, image = await read([1, 1, 1])
        self.assertTrue(ok)
        self.assertEqual(image.images[0]["base64"], "AQID")
        self.assertEqual(bridge._read_deferred_media.await_args.args[1], url)
        ok, audio = await read([1, 1, 2])
        self.assertTrue(ok)
        self.assertIn("没有可读的媒体内容", audio)
        self.assertEqual(bridge._read_deferred_media.await_count, 1)

    async def test_quoted_record_provides_transcript_in_its_own_component(self):
        class Record:
            pass

        bridge = make_bridge({"stt_enabled": True})
        bridge.server.capabilities["message_segments"] = True
        bridge._transcribe = mock.AsyncMock(return_value="原语音的内容")
        quote = Reply("100")
        quote.id = "earlier"
        voice = Record()
        quote.chain = [Plain("之前"), voice]
        event = FakeEvent("这段语音", messages=[quote, Plain("这段语音")])
        self.assertTrue(await bridge._push_event(event))
        audio = bridge.server.pushed[0]["segments"][0]["children"][1]
        self.assertEqual(audio["type"], "record")
        self.assertEqual(audio["fields"]["text"], "原语音的内容")
        self.assertEqual(audio["fields"]["media_state"], "described")

    async def test_quoted_image_uses_delivery_plan_and_attachment_slot(self):
        for mode in ("image", "describe", "off"):
            with self.subTest(mode=mode):
                bridge = make_bridge({"image_mode": mode})
                bridge.server.capabilities.update(images=True, message_segments=True)
                bridge._describe_image = mock.AsyncMock(return_value=("识别到图片里的猫", ""))
                quote = Reply("100")
                quote.id = "earlier"
                quote.chain = [Image("https://example.com/quoted.png")]
                event = FakeEvent("看看这张", messages=[quote, Plain("看看这张")], raw={"message": [
                    {"type": "reply", "data": {"id": "earlier"}},
                    {"type": "text", "data": {"text": "看看这张"}},
                ]})
                self.assertTrue(await bridge._push_event(event))
                packet = bridge.server.pushed[0]
                fields = packet["segments"][0]["children"][0]["fields"]
                if mode == "image":
                    self.assertEqual(fields["attachment"], "0")
                    self.assertEqual(packet["images"][0]["mime"], "image/png")
                    self.assertTrue(packet["quote_image_history"])
                else:
                    self.assertNotIn("images", packet)
                    self.assertNotIn("quote_image_history", packet)
                    self.assertNotIn("attachment", fields)
                    self.assertIn("图片里的猫" if mode == "describe" else "已关闭", fields["text"])

    async def test_image_slots_keep_direct_image_first_and_quote_rest(self):
        bridge = make_bridge({"image_mode": "image"})
        bridge.server.capabilities.update(images=True, message_segments=True)
        direct = Image("https://example.com/direct.png")
        quote = Reply("100")
        quote.id = "earlier"
        quote.chain = [Image("https://example.com/first.png"), Image("https://example.com/second.png")]
        event = FakeEvent("Coo 看图", messages=[quote, direct, Plain("Coo 看图")])
        self.assertTrue(await bridge._push_event(event))
        packet = bridge.server.pushed[0]
        self.assertEqual(len(packet["images"]), 2)
        self.assertEqual(packet["segments"][1]["fields"]["attachment"], "0")
        children = packet["segments"][0]["children"]
        self.assertEqual(children[0]["fields"]["attachment"], "1")
        self.assertNotIn("attachment", children[1]["fields"])
        self.assertIn("上限", children[1]["fields"]["text"])

    async def test_all_component_kinds_and_forward_scope(self):
        from importlib import import_module

        components = import_module(f"{main_mod.__package__}.message_segments")
        from unittest.mock import AsyncMock

        bridge = make_bridge()
        bridge.server.capabilities["message_segments"] = True
        chain = [
            {"type": kind, "data": (
                {"id": "forward-123"} if kind == "forward" else {"text": "fixture"}
            )}
            for kind in components.LABELS
        ]
        event = FakeEvent("fixture", message_id="outer", raw={"message": chain})
        event.bot = types.SimpleNamespace(call_action=AsyncMock(return_value={
            "messages": [{
                "sender": {"nickname": "转发人", "user_id": 42},
                "message": [{"type": "text", "data": {"text": "转发正文"}}],
            }] * 9,
        }))
        self.assertTrue(await bridge._push_event(event))
        packet = bridge.server.pushed[0]
        self.assertEqual([part["type"] for part in packet["segments"]], list(components.LABELS))

        args = {"message_id": "outer", "path": [list(components.LABELS).index("forward") + 1]}
        other = FakeEvent("other", group_id="2", message_id="other")
        self.assertTrue(await bridge._push_event(other))
        denied, _ = await bridge._on_tool_call(components.READ_COMPONENT_TOOL, args, "other", scope=other.unified_msg_origin)
        self.assertFalse(denied)
        event.bot.call_action.assert_not_called()

        ok, result = await bridge._on_tool_call(components.READ_COMPONENT_TOOL, args, "outer", scope=event.unified_msg_origin)
        self.assertTrue(ok)
        self.assertIn("转发正文", result)
        self.assertIn("offset=8", result)
        ok, result = await bridge._on_tool_call(
            components.READ_COMPONENT_TOOL, {**args, "offset": 8}, "outer", scope=event.unified_msg_origin
        )
        self.assertTrue(ok)
        self.assertIn("第 9–9 条", result)
        event.bot.call_action.assert_awaited_once_with(
            "get_forward_msg", id="forward-123", message_id="forward-123", self_id=999
        )

    async def test_join_original_order_with_resolved_quote_and_failed_files(self):
        bridge = make_bridge()
        bridge.server.capabilities["message_segments"] = True
        quote = types.SimpleNamespace(
            type="Reply", id="42", sender_nickname="引用作者", chain=[Plain("旧正文")],
        )
        event = FakeEvent("当前正文", messages=[quote, Plain("当前正文")], raw={"message": [
            {"type": "reply", "data": {"id": "42"}},
            {"type": "file", "data": {"name": r"C:\private\示例.zip", "url": "https://example.com/token"}},
            {"type": "text", "data": {"text": "当前正文"}},
            {"type": "new_type", "data": {"alt": "未知组件"}},
        ]})
        self.assertTrue(await bridge._push_event(event))
        parts = bridge.server.pushed[0]["segments"]
        self.assertEqual([part["type"] for part in parts], ["reply", "file", "plain", "unknown"])
        self.assertEqual(parts[0]["children"][0]["fields"]["text"], "旧正文")
        self.assertEqual(parts[0]["fields"]["name"], "引用作者")
        self.assertEqual(parts[1]["fields"], {"name": "示例.zip", "media_state": "metadata"})
        self.assertEqual(parts[3]["fields"]["kind"], "new_type")


class ListenHostTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_listen_host_defaults_to_loopback(self):
        bridge = make_bridge()
        self.assertEqual(bridge._listen_host(), "127.0.0.1")

    async def test_explicit_listen_host_is_kept(self):
        bridge = make_bridge({"listen_host": "0.0.0.0"})
        self.assertEqual(bridge._listen_host(), "0.0.0.0")

    async def test_page_restart_uses_loopback_default(self):
        bridge = make_bridge({"listen_host": ""})
        bridge.server.host = "0.0.0.0"
        with fake_responses():
            result = await bridge.page_restart()
        self.assertEqual(result["status"], 200)
        self.assertEqual(bridge.server.host, "127.0.0.1")


class VoiceTestTests(unittest.IsolatedAsyncioTestCase):
    async def test_tts_disabled_does_not_send_any_message(self):
        bridge = make_bridge({"tts_enabled": False})
        with fake_responses(), fake_request("POST", {"text": "你好"}):
            result = await bridge.page_voice_test()
        self.assertEqual(result["status"], 400)
        self.assertIn("tts_unavailable", result["error"])
        self.assertEqual(bridge.delivery.sent, [])

    async def test_voice_failure_never_falls_back_to_text(self):
        bridge = make_bridge({"tts_enabled": True})
        bridge.delivery = FakeDelivery(result=(False, "TTS 失败", False, False, "tts_unavailable"))

        async def provider(_kind):
            return object()

        bridge._voice_provider = provider
        with fake_responses(), fake_request("POST", {"text": "你好"}):
            result = await bridge.page_voice_test()
        self.assertEqual(result["status"], 500)
        self.assertEqual(len(bridge.delivery.sent), 1)
        self.assertFalse(bridge.delivery.sent[0][1]["allow_text_fallback"])
        self.assertIn("TTS 失败", result["error"])

    async def test_safe_voice_test_selects_only_owner_and_rechecks_before_send(self):
        owner = "default:FriendMessage:111"
        bridge = make_bridge({"tts_enabled": True, "owner_tool_qq": "111",
                              "owner_sessions": {"default": {"user_id": "111", "umo": owner}}}, delivery=FakeDelivery([
            {"key": "group:1", "umo": "default:GroupMessage:1"}, {"key": "private:111", "umo": owner},
        ]))
        bridge.server.mode = "safe"
        bridge._voice_provider = mock.AsyncMock(return_value=object())
        with fake_responses(), fake_request("POST", {"text": "你好"}):
            result = await bridge.page_voice_test()
        self.assertEqual(result["status"], 200)
        args, kwargs = bridge.delivery.sent[0]
        self.assertEqual(args[-1], owner)
        self.assertTrue(kwargs["may_dispatch"]())
        bridge.config["owner_tool_qq"] = "222"
        self.assertFalse(kwargs["may_dispatch"]())
        with fake_responses(), fake_request("POST", {"text": "你好"}):
            result = await bridge.page_voice_test()
        self.assertEqual(result["status"], 400)
        self.assertEqual(len(bridge.delivery.sent), 1)


class TrustedToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_qq_admin_does_not_depend_on_desktop_authorization(self):
        bridge = make_bridge(context=FakeContext(admins=["111"]))
        bridge.server.tool_owner = ""
        event = FakeEvent(sender_id="111")
        bridge._turns[(event.unified_msg_origin, "t1")] = (time.time(), event)
        await bridge._on_tool_call("test_tool", {}, "t1", scope=event.unified_msg_origin)
        self.assertTrue(bridge.tools.executed[-1][2])

    async def test_qq_admin_sender_is_trusted(self):
        bridge = make_bridge(context=FakeContext(admins=["111"]))
        event = FakeEvent(sender_id="111")
        bridge._turns[(event.unified_msg_origin, "t1")] = (time.time(), event)
        ok, _text = await bridge._on_tool_call("run_shell", {}, "t1", scope=event.unified_msg_origin)
        self.assertTrue(ok)
        self.assertEqual(bridge.tools.executed[-1][2], True)

    async def test_qq_member_sender_is_not_trusted(self):
        bridge = make_bridge(context=FakeContext(admins=["111"]))
        event = FakeEvent(sender_id="222")
        bridge._turns[(event.unified_msg_origin, "t1")] = (time.time(), event)
        await bridge._on_tool_call("run_shell", {}, "t1", scope=event.unified_msg_origin)
        self.assertEqual(bridge.tools.executed[-1][2], False)

    async def test_desktop_actor_uses_astrbot_bound_identity(self):
        bridge = make_bridge({"owner_tool_qq": "111"}, context=FakeContext(admins=["111"]))
        event = FakeEvent(sender_id="111")
        with mock.patch.object(owner_event, "build_owner_event", return_value=event) as build:
            ok, _ = await bridge._on_tool_call("run_shell", {}, "", {"kind": "desktop"})
        self.assertTrue(ok)
        self.assertIn("111", build.call_args.args)
        self.assertEqual(bridge.tools.executed[-1][2], True)

    async def test_owner_event_construction_failure_does_not_leak_exception(self):
        bridge = make_bridge(context=FakeContext(admins=["111"]))
        secret = "SECRET https://cdn.example.com/hook?sig=topsecret"
        with mock.patch.object(
            owner_event,
            "build_owner_event",
            side_effect=RuntimeError(secret),
        ):
            ok, text = await bridge._on_tool_call(
                "run_shell", {}, "", {"kind": "desktop"}
            )
        self.assertFalse(ok)
        self.assertIn("主人事件构造失败", text)
        self.assertNotIn("SECRET", text)
        self.assertNotIn("example.com", text)
        self.assertEqual(bridge.tools.executed, [])

    async def test_world_role_owner_fields_are_ignored(self):
        bridge = make_bridge(context=FakeContext(admins=["111"]))
        ok, text = await bridge._on_tool_call(
            "run_shell",
            {},
            "",
            {"kind": "desktop", "id": "222", "role": "admin", "owner": True},
        )
        self.assertFalse(ok)
        self.assertIn("未获得 AstrBot", text)
        self.assertEqual(bridge.tools.executed, [])


class SendPassThroughTests(unittest.IsolatedAsyncioTestCase):
    async def test_send_returns_five_tuple_with_code_and_note(self):
        bridge = make_bridge()
        bridge.delivery = FakeDelivery(
            result=(False, "发送失败，详情见插件日志", False, False, "send_failed")
        )
        result = await bridge._on_send_from_cortico(
            "group", "1", "hi", "", "default", umo="default:GroupMessage:1"
        )
        self.assertEqual(len(result), 5)
        self.assertFalse(result[0])
        self.assertEqual(result[1], "发送失败，详情见插件日志")
        self.assertEqual(result[4], "send_failed")


class ConfigCompatibilityTests(unittest.TestCase):
    def test_modes_preserve_enabled_legacy_deployments_and_fresh_install_is_safe(self):
        self.assertEqual(make_bridge()._initial_bridge_mode(), "full")
        self.assertEqual(make_bridge({"enabled": False})._initial_bridge_mode(), "safe")
        self.assertEqual(make_bridge({"bridge_mode": "safe"})._initial_bridge_mode(), "safe")

    def test_schema_preserves_admin_mode_and_migration_state_on_reload(self):
        schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
        previous = {"enabled": True, "bridge_mode": "safe", "desktop_auth_reviewed": True,
                    "owner_tool_qq": "111", "owner_tool_token": "fixture-old-token"}
        # AstrBot 的配置加载器只留下 schema 中声明的键。
        loaded = {key: previous.get(key, field["default"]) for key, field in schema.items()}
        bridge = make_bridge(loaded)
        self.assertEqual(bridge._initial_bridge_mode(), "safe")
        self.assertFalse(bridge._legacy_desktop_needs_review())
        self.assertTrue(schema["owner_tool_token"]["secret"])
        self.assertTrue(schema["owner_tool_token"]["invisible"])

    def test_schema_defaults_preserve_legacy_group_policy(self):
        schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
        defaults = {key: value["default"] for key, value in schema.items()}
        bridge = make_bridge({**defaults, "observe_groups": ["123456"], "observe_all": True})
        self.assertEqual(bridge._group_scope(), "allowlist")
        self.assertEqual(bridge._group_allow(), ["123456"])
        self.assertEqual(bridge._group_mode(), "all")

    def test_already_injected_scope_does_not_widen_legacy_allowlist(self):
        bridge = make_bridge({"group_scope": "all", "group_allow": [], "observe_groups": ["123456"]})
        self.assertEqual(bridge._group_scope(), "allowlist")
        self.assertEqual(bridge._group_allow(), ["123456"])
        bridge.config.update({"group_scope": "allowlist", "group_allow": ["456789"]})
        self.assertEqual(bridge._group_allow(), ["456789"])

    def test_invalid_port_uses_default(self):
        bridge = make_bridge()
        for port in (None, 0, True, 1.5, -1, 65536, float("inf")):
            with self.subTest(port=port):
                bridge.config["listen_port"] = port
                self.assertEqual(bridge._port(), 17999)
        bridge.config["listen_port"] = "18000.0"
        self.assertEqual(bridge._port(), 18000)


class PageBackendTests(unittest.IsolatedAsyncioTestCase):
    async def test_full_approval_requires_the_reviewed_scope(self):
        bridge = make_bridge()
        bridge.server.approval = "pending-fixture"
        reviewed = bridge._approval_scope_version(bridge._full_mode_scope())
        payload = {"action": "approve", "approval": bridge.server.approval, "scopeVersion": reviewed, "confirmed": True}
        # Tool inventory can change independently of the bridge plugin configuration.
        bridge.tools.active_rows = [{"name": "weather", "risk": "read"}]
        with fake_responses(), fake_request("POST", payload):
            self.assertEqual((await bridge.page_access())["status"], 400)
        self.assertEqual(bridge.server.approved, [])
        payload["scopeVersion"] = bridge._approval_scope_version(bridge._full_mode_scope())
        with fake_responses(), fake_request("POST", payload):
            self.assertEqual((await bridge.page_access())["status"], 200)
        self.assertEqual(bridge.server.approved, ["pending-fixture"])

    async def test_legacy_owner_is_carried_over_without_giving_shared_token_desktop_rights(self):
        bridge = make_bridge({"owner_tool_qq": "111", "owner_tool_token": "fixture-legacy-token"})
        with fake_responses(), fake_request("GET"):
            info = (await bridge.page_access())["data"]
        self.assertEqual(info["ownerPlatform"], "default")
        self.assertTrue(info["legacyDesktopNeedsReview"])
        self.assertFalse(info["desktopTools"])
        self.assertNotIn("fixture-legacy-token", repr(info))
        updates = {"action": "save", "ownerQQ": "111", "ownerPlatform": "default", "desktopTools": True,
                   "desktopTargets": ["default:GroupMessage:1"]}
        with fake_responses(), fake_request("POST", updates):
            self.assertEqual((await bridge.page_access())["status"], 400)
        self.assertFalse(bridge.config.get("desktop_tools_enabled"))
        with fake_responses(), fake_request("POST", {**updates, "confirmed": True}):
            updated = (await bridge.page_access())["data"]
        self.assertTrue(updated["desktopTools"])
        self.assertFalse(updated["legacyDesktopNeedsReview"])
        self.assertEqual(updated["desktopTargets"], ["default:GroupMessage:1"])

    async def test_safe_mode_binding_preserves_stored_full_mode_targets(self):
        bridge = make_bridge({"owner_tool_qq": "111", "desktop_tools_enabled": True,
                              "desktop_send_targets": ["default:GroupMessage:1"]})
        bridge.server.mode = "safe"
        updates = {"action": "save", "ownerQQ": "222", "ownerPlatform": "default", "desktopTools": True,
                   "desktopTargets": [], "confirmed": True}
        with fake_responses(), fake_request("POST", updates):
            updated = (await bridge.page_access())["data"]
        self.assertEqual(updated["desktopTargets"], ["default:GroupMessage:1"])
        self.assertFalse(updated["desktopTools"])

    async def test_status_has_token_set_but_no_plaintext(self):
        bridge = make_bridge({"token": "secret"})
        bridge.server.host = "127.0.0.1"

        async def voice_info(_kind):
            return {"id": ""}

        bridge._voice_info = voice_info
        with fake_responses():
            result = await bridge.page_status()
        data = result["data"]
        self.assertNotIn("token", data)
        self.assertTrue(data["tokenSet"])
        self.assertEqual(list(data).count("listening"), 1)

    async def test_token_get_still_returns_plaintext(self):
        bridge = make_bridge({"token": "secret"})
        bridge.server.host = "127.0.0.1"
        with fake_responses():
            result = await bridge.page_token()
        self.assertEqual(result["data"]["token"], "secret")
        self.assertTrue(result["data"]["tokenSet"])

    async def test_tools_returns_granted_without_exec_under_star(self):
        rows = [
            {"name": "read_tool", "risk": "read", "active": True},
            {"name": "exec_tool", "risk": "exec", "active": True},
        ]
        tools = FakeTools(
            allow=["*"], catalog=rows, enabled=[rows[0]]
        )
        bridge = make_bridge(tools=tools)
        with fake_responses():
            result = await bridge.page_tools()
        data = result["data"]
        self.assertTrue(data["all"])
        self.assertEqual(data["granted"], ["read_tool"])
        self.assertNotIn("exec_tool", data["granted"])


class ConsoleSettingsTests(unittest.IsolatedAsyncioTestCase):
    async def test_connection_settings_confirm_and_restore_existing_listener(self):
        bridge = make_bridge({"enabled": True, "listen_host": "0.0.0.0", "listen_port": 17999, "token": "fixture-token"})
        bridge.server.listening = True
        calls = []

        async def start():
            calls.append(bridge.server.port)
            bridge.server.listening = bridge.server.port == 17999
            return bridge.server.listening

        bridge.server.start = start
        draft = {"enabled": True, "host": "0.0.0.0", "port": 18000, "maxSends": 15}
        with fake_responses(), fake_request("POST", draft):
            reply = await bridge.page_connection_settings()
        self.assertEqual(reply["status"], 409)
        self.assertEqual(calls, [])

        with fake_responses(), fake_request("POST", {**draft, "confirmed": True}):
            reply = await bridge.page_connection_settings()
        self.assertEqual(reply["status"], 500)
        self.assertEqual(calls, [18000, 17999])
        self.assertTrue(bridge.server.listening)
        self.assertEqual(bridge.config["listen_port"], 17999)
        self.assertEqual(bridge.config.saves, 0)

    async def test_connection_settings_reject_non_loopback_without_token(self):
        bridge = make_bridge({"enabled": False, "token": ""})
        draft = {"enabled": True, "host": "0.0.0.0", "port": 17999, "maxSends": 30, "confirmed": True}
        with fake_responses(), fake_request("POST", draft):
            reply = await bridge.page_connection_settings()
        self.assertEqual(reply["status"], 400)
        self.assertFalse(bridge.server.listening)
        self.assertFalse(bridge.config["enabled"])

    async def test_qq_settings_keep_implicit_owner_when_adding_an_instance(self):
        context = FakeContext()
        context.get_config = lambda: {"platform": [
            {"id": "default", "type": "aiocqhttp"},
            {"id": "qq-two", "type": "aiocqhttp"},
            {"id": "discord", "type": "discord"},
        ]}
        bridge = make_bridge({"owner_tool_qq": "111"}, context=context)
        draft = {"platforms": ["default", "qq-two"], "aliases": ["Coo"]}
        with fake_responses(), fake_request("POST", draft):
            reply = await bridge.page_qq_settings()
        self.assertEqual(reply["status"], 409)
        with fake_responses(), fake_request("POST", {**draft, "confirmed": True}):
            reply = await bridge.page_qq_settings()
        self.assertEqual(reply["status"], 200)
        self.assertEqual(bridge.config["owner_platform"], "default")
        self.assertEqual(bridge._instance_owner("default"), ("111", ""))

    async def test_image_settings_offer_existing_providers_and_reject_unknown_ids(self):
        context = FakeContext()
        context.get_all_providers = lambda: [types.SimpleNamespace(provider_config={"id": "fixture-vision"})]
        bridge = make_bridge(context=context)
        with fake_responses(), fake_request("POST", {"mode": "describe", "provider": "missing"}):
            reply = await bridge.page_image_settings()
        self.assertEqual(reply["status"], 400)
        with fake_responses(), fake_request("POST", {"mode": "describe", "provider": "fixture-vision"}):
            reply = await bridge.page_image_settings()
        self.assertEqual(reply["data"]["provider"], "fixture-vision")
        self.assertEqual(bridge.config["image_mode"], "describe")

    async def test_tool_risks_require_full_mode_and_explicit_confirmation(self):
        tools = FakeTools(allow=["explicit_tool"], catalog=[{"name": "fixture_tool"}])
        tools.declared_risks = lambda: {}
        bridge = make_bridge(tools=tools)
        bridge.server.mode = "safe"
        draft = {"entries": ["fixture_tool:read"], "autoReviewed": True}
        with fake_responses(), fake_request("POST", {**draft, "confirmed": True}):
            reply = await bridge.page_tool_risks()
        self.assertEqual(reply["status"], 403)
        bridge.server.mode = "full"
        with fake_responses(), fake_request("POST", draft):
            reply = await bridge.page_tool_risks()
        self.assertEqual(reply["status"], 409)
        with fake_responses(), fake_request("POST", {**draft, "confirmed": True}):
            reply = await bridge.page_tool_risks()
        self.assertEqual(reply["status"], 200)
        self.assertEqual(bridge.config["tools_risks"], ["fixture_tool:read"])
        self.assertEqual(bridge.config["tools_allow"], ["*", "explicit_tool"])


class DiscordRouteTests(unittest.IsolatedAsyncioTestCase):
    def test_platform_report_matches_configured_instances_and_their_enablement(self):
        context = FakeContext()
        context.get_config = lambda: {"platform": [
            {"id": "default", "type": "aiocqhttp", "enable": True},
            {"id": "qq-disabled", "type": "aiocqhttp", "enable": False},
            {"id": "web", "type": "webchat", "enable": True},
            {"id": "discord", "type": "discord", "enable": True},
        ]}
        bridge = make_bridge({
            "bridge_platforms": ["default", "qq-disabled", "missing", "web"],
            "owner_tool_qq": "111", "owner_platform": "default",
        }, context=context)
        self.assertEqual(bridge._platform_routes(), [
            {"id": "default", "channel": "qq", "adapterType": "aiocqhttp", "name": "default", "enabled": True, "ownerBound": False},
            {"id": "qq-disabled", "channel": "qq", "adapterType": "aiocqhttp", "name": "qq-disabled", "enabled": False, "ownerBound": False},
            {"id": "web", "channel": "webchat", "adapterType": "webchat", "name": "web", "enabled": False, "ownerBound": False},
            {"id": "discord", "channel": "discord", "adapterType": "discord", "name": "discord", "enabled": False, "ownerBound": False},
        ])

    async def test_instances_bind_and_forward_real_private_umos_without_sharing_scope(self):
        bridge = self.make_discord_bridge()
        bridge.server.mode = "safe"

        def qq_private():
            event = FakeEvent("你好", private=True, sender_id="111", message_id="701")
            event.unified_msg_origin = "default:FriendMessage:qq-native:another-segment"
            return event

        forged = self.event(private=True)
        forged.unified_msg_origin = qq_private().unified_msg_origin
        await bridge.observe_all_messages(forged)
        self.assertEqual(bridge.config.saves, 0)

        await bridge.observe_all_messages(qq_private())
        await bridge.observe_all_messages(self.event(private=True))
        self.assertEqual(bridge.config["owner_sessions"], {
            "default": {"user_id": "111", "umo": qq_private().unified_msg_origin},
            "my-discord": {"user_id": "111", "umo": "my-discord:FriendMessage:600"},
        })
        self.assertFalse(bridge._event_allowed(forged))

        for event in (qq_private(), self.event(private=True)):
            await bridge.observe_all_messages(event)
            await bridge.on_llm_request(event, None)
        self.assertEqual([frame["umo"] for frame in bridge.server.pushed], [
            qq_private().unified_msg_origin, "my-discord:FriendMessage:600",
        ])

        with fake_responses(), fake_request("POST", {"action": "save", "ownerQQ": "222", "ownerPlatform": "default"}):
            result = await bridge.page_access()
        self.assertEqual(result["status"], 200)
        self.assertFalse(bridge._event_allowed(qq_private()))
        self.assertTrue(bridge._event_allowed(self.event(private=True)))
        bridge.server.mode = "full"
        masquerading = qq_private()
        masquerading.get_platform_id = lambda: "my-discord"
        self.assertFalse(bridge._event_allowed(masquerading))

    def make_discord_bridge(self, *, supports_discord=True, bound=""):
        context = FakeContext()
        context.get_config = lambda **_: {
            "admins_id": ["111"],
            "platform": [{"id": "my-discord", "type": "discord", "enable": True}],
        }
        bridge = make_bridge({
            "group_mode": "all",
            "owner_tool_qq": "111",
            "discord_routes": {"my-discord": {
                "enabled": True, "owner_id": "111", "owner_umo": bound,
                "channels": ["my-discord:GroupMessage:600"],
            }},
        }, context=context)
        bridge.server.supports_multiple_platforms = supports_discord
        return bridge

    @staticmethod
    def event(*, private=False, session="600", sender="111", mention=False):
        event = FakeEvent("Coo，你好", private=private, group_id=session, sender_id=sender, message_id="701")
        kind = "FriendMessage" if private else "GroupMessage"
        event.unified_msg_origin = f"my-discord:{kind}:{session}"
        event.get_platform_id = lambda: "my-discord"
        event.get_platform_name = lambda: "discord"
        event.is_mentioned = lambda: mention
        return event

    async def test_safe_mode_binds_native_dm_for_the_configured_sender(self):
        bridge = self.make_discord_bridge()
        bridge.server.mode = "safe"
        await bridge.observe_all_messages(self.event(private=True, sender="222"))
        self.assertEqual(bridge.config.saves, 0)

        await bridge.observe_all_messages(self.event(private=True))
        self.assertEqual(bridge.config.saves, 1)
        self.assertIn("my-discord:FriendMessage:600", bridge._owner_dms())
        self.assertNotIn("my-discord:FriendMessage:111", bridge._owner_dms())
        self.assertFalse(bridge._event_allowed(self.event(private=True, session="111")))
        self.assertFalse(bridge._event_allowed(self.event(private=True, sender="222")))
        self.assertFalse(bridge._event_allowed(self.event(mention=True)))

        owner_message = self.event(private=True)
        await bridge.observe_all_messages(owner_message)
        await bridge.on_llm_request(owner_message, None)
        frame, = bridge.server.pushed
        self.assertEqual(frame["conv"]["id"], "600")
        self.assertEqual(frame["sender"]["id"], "111")
        self.assertEqual(frame["channel"], "discord")
        self.assertTrue(frame["owner"])
        self.assertFalse(frame["can"]["screenshot"])

    async def test_full_mode_requires_channel_allowlist_and_real_mention(self):
        bridge = self.make_discord_bridge(supports_discord=False)
        await bridge.observe_all_messages(self.event(mention=True))
        self.assertEqual(bridge.server.pushed, [])
        bridge.server.supports_multiple_platforms = True
        await bridge.observe_all_messages(self.event())
        await bridge.observe_all_messages(self.event(session="601", mention=True))
        self.assertEqual(bridge.server.pushed, [])
        command = self.event(mention=True)
        command.is_slash_command = lambda: True
        await bridge.observe_all_messages(command)
        self.assertEqual(bridge.server.pushed, [])

        message = self.event(mention=True)
        await bridge.observe_all_messages(message)
        await bridge.on_llm_request(message, None)
        frame, = bridge.server.pushed
        self.assertEqual(frame["umo"], "my-discord:GroupMessage:600")
        self.assertTrue(frame["mentioned"])
        self.assertEqual(frame["role"], "member")
        self.assertFalse(frame["can"]["screenshot"])

    def test_admin_access_changes_require_confirmation_and_invalidate_dm_on_owner_change(self):
        bridge = self.make_discord_bridge(bound="my-discord:FriendMessage:600")
        change = {"platform": "my-discord", "enabled": True, "ownerId": "222", "channels": ["600"]}
        with self.assertRaisesRegex(ValueError, "确认"):
            bridge._save_discord_route(change)
        self.assertTrue(bridge._save_discord_route({**change, "confirmed": True}))
        self.assertEqual(bridge.config["discord_routes"]["my-discord"]["owner_umo"], "")
        self.assertIsNone(bridge._discord_routes().get("default"))


class NativeInstanceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        context = FakeContext()
        context.get_config = lambda **_: {"admins_id": ["111"], "platform": [
            {"id": "default", "type": "aiocqhttp", "enable": True},
            {"id": "work-chat", "type": "wecom_ai_bot", "enable": True},
            {"id": "other-work", "type": "wecom_ai_bot", "enable": True},
        ]}
        self.bridge = make_bridge({
            "group_scope": "allowlist", "group_allow": ["default:GroupMessage:70"],
            "owner_tool_qq": "111", "desktop_tools_enabled": True,
        }, context=context)
        self.bridge.server.capabilities["instance_policies"] = True
        self.bridge._migrate_instance_policies()

    def event(self, instance="work-chat", *, sender="zhangsan", private=True):
        session = f"wecomai:{'zhangsan' if private else 'group_1'}"
        event = FakeEvent("你好", private=private, sender_id=sender, message_id="first")
        event.unified_msg_origin = f"{instance}:{'FriendMessage' if private else 'GroupMessage'}:{session}"
        event.get_platform_id = lambda: instance
        event.send = mock.AsyncMock()
        return event

    async def save(self, instance, **policy):
        row = next(row for row in self.bridge._instance_view()["instances"] if row["id"] == instance)
        self.bridge._save_instance_policy({
            "instance": instance, "version": row["version"], "policy": policy, "confirmed": True,
        })

    async def test_discovery_and_mixed_mode_bindings(self):
        bridge = self.bridge
        routes = {row["id"]: row for row in bridge._platform_routes()}
        self.assertTrue(routes["default"]["enabled"])
        self.assertEqual(routes["default"]["mode"], "full")
        self.assertFalse(routes["work-chat"]["enabled"])
        self.assertFalse(bridge._event_allowed(self.event()))
        await self.save("work-chat", enabled=True, owner_id="zhangsan")
        self.assertEqual(bridge.server.mode, "full")
        self.assertEqual(bridge._scope_mode(self.event().unified_msg_origin), "safe")
        for irrelevant in (self.event(private=False), self.event(sender="wrong"), self.event("other-work")):
            await bridge.observe_all_messages(irrelevant)
        self.assertFalse(bridge._instance_owner("work-chat")[1])
        event = self.event()
        await bridge.observe_all_messages(event)
        self.assertEqual(bridge._instance_owner("work-chat"), ("zhangsan", event.unified_msg_origin))
        await bridge.observe_all_messages(event)
        await bridge.on_llm_request(event, None)
        self.assertEqual(bridge.server.pushed[-1]["channel"], "wecom_ai_bot")
        self.assertEqual(bridge.server.pushed[-1]["conv"]["id"], "wecomai:zhangsan")
        self.assertFalse(bridge._event_allowed(self.event("other-work")))
        await self.save("work-chat", owner_id="lisi")
        self.assertFalse(bridge._event_allowed(event))

    async def test_cross_instance_approval_and_fixed_webhook_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "完整原生会话 UMO"):
            await self.save("work-chat", enabled=True, group_allow=["other-work:GroupMessage:wecomai:group_1"])
        with self.assertRaisesRegex(ValueError, "主动发送未验证"):
            await self.save("work-chat", enabled=True, mode="full", desktop_targets=[self.event().unified_msg_origin])
        platform = {"id": "work-chat", "enable": True, "type": "wecom_ai_bot",
                    "only_use_webhook_url_to_send": True, "msg_push_webhook_url": "https://example.invalid/"}
        self.bridge.context.get_config = lambda **_: {"platform": [platform]}
        with self.assertRaisesRegex(ValueError, "固定 Webhook"):
            await self.save("work-chat", enabled=True)

    async def test_reply_is_tied_to_the_native_event_and_its_umo(self):
        bridge = self.bridge
        await self.save("work-chat", enabled=True, owner_id="zhangsan")
        event = self.event()
        await bridge.observe_all_messages(event)
        await bridge.observe_all_messages(event)
        await bridge.on_llm_request(event, None)
        args = ("private", "wecomai:zhangsan", "回复", "", "work-chat")
        result = await bridge._on_send_from_cortico(*args, umo=event.unified_msg_origin, turn="bad")
        self.assertFalse(result[0])
        result = await bridge._on_send_from_cortico(*args, umo=event.unified_msg_origin, turn="first")
        self.assertTrue(result[0], result)
        self.assertIs(bridge.delivery.sent[-1][1]["reply_event"], event)
        result = await bridge._on_send_from_cortico(*args, umo=event.unified_msg_origin)
        self.assertFalse(result[0])

    async def test_fresh_install_does_not_enable_default_bot(self):
        bridge = make_bridge({"enabled": False}, context=self.bridge.context)
        bridge._migrate_instance_policies()
        self.assertTrue(bridge.config["instance_policy_migrated"])
        self.assertEqual(bridge.config["instance_routes"], {})
        self.assertFalse(bridge._instance_enabled("default"))
