"""桥传输层回归测试：代次、发送锁、并发上限、重复 hello、工具身份、错误码。

只覆盖 P-A 的必修项；不改 test_umo.py（那里仍是旧用例的基线）。
"""

import asyncio
import json
import sys
import types
import unittest
import typing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# bridge.py 只依赖 astrbot.api.logger；给一个静默 logger。
astrbot = types.ModuleType("astrbot")
api = types.ModuleType("astrbot.api")


class _Logger:
    def __getattr__(self, _name):
        return lambda *args, **kwargs: None


api.logger = _Logger()
astrbot.api = api
sys.modules.setdefault("astrbot", astrbot)
sys.modules.setdefault("astrbot.api", api)

import bridge  # noqa: E402
import errors as codes  # noqa: E402

SEND_FRAME = {
    "t": "send",
    "rid": "s-1",
    "conv": {"kind": "group", "id": "123456"},
    "text": "hi",
}


async def _ok_send(*_args, **_kwargs):
    return True, "", False, False, ""


class _Ws:
    """最小 WebSocket 替身：记录发出的帧和 close。"""

    def __init__(self):
        self.sent: list[str] = []
        self.closed: list[tuple[int, str]] = []

    async def send(self, data) -> None:
        self.sent.append(data)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed.append((code, reason))

    def frames(self) -> list[dict]:
        return [json.loads(item) for item in self.sent]

    def last(self) -> dict:
        return json.loads(self.sent[-1])


class _FakeWs(_Ws):
    """带握手/帧序列的连接；每次取帧让出一次事件循环，贴近真实 IO。"""

    def __init__(self, frames, headers=None, path: str = "/"):
        super().__init__()
        self._frames = list(frames)
        self.request = types.SimpleNamespace(headers=headers or {}, path=path)
        self.remote_address = ("127.0.0.1", 4321)

    async def recv(self):
        return self._frames.pop(0)

    def __aiter__(self):
        return self

    async def __anext__(self):
        await asyncio.sleep(0)
        if self._frames:
            return self._frames.pop(0)
        raise StopAsyncIteration


def _server(on_send=None, on_tool=None, token=""):
    return bridge.BridgeServer(
        host="127.0.0.1",
        port=0,
        token=token,
        on_send=on_send or _ok_send,
        on_tool=on_tool,
    )


class EpochTests(unittest.IsolatedAsyncioTestCase):
    """E-001：旧连接的慢任务不能把 ack 写进新连接。"""

    async def test_old_epoch_ack_is_dropped_after_reconnect(self):
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow_send(*_args, **_kwargs):
            started.set()
            await release.wait()
            return True, "", False, False, ""

        server = _server(on_send=slow_send)
        server._epoch = 1
        ws = _Ws()
        task = asyncio.create_task(
            server._handle_send(ws, dict(SEND_FRAME), epoch=1)
        )
        await started.wait()

        # 模拟新连接注册：代次 +1、ack 缓存清空。
        server._epoch = 2
        server._acks.clear()
        release.set()
        await task

        self.assertEqual(dict(server._acks), {})
        self.assertTrue(ws.last()["ok"])

    async def test_current_epoch_ack_is_cached(self):
        server = _server()
        server._epoch = 7
        ws = _Ws()
        await server._handle_send(ws, dict(SEND_FRAME), epoch=7)
        self.assertIn("s-1", server._acks)
        self.assertIsNotNone(server._cached_ack("s-1", 7))

    async def test_old_epoch_frame_is_not_executed(self):
        called = []

        async def on_send(*args, **_kwargs):
            called.append(args)
            return True, "", False, False, ""

        server = _server(on_send=on_send)
        server._epoch = 2
        ws = _Ws()
        await server._handle_send(ws, dict(SEND_FRAME), epoch=1)
        self.assertEqual(called, [])
        self.assertEqual(ws.sent, [])
        self.assertEqual(dict(server._acks), {})

    async def test_cached_ack_is_scoped_to_epoch(self):
        server = _server()
        server._epoch = 2
        ws = _Ws()
        await server._handle_send(ws, dict(SEND_FRAME), epoch=2)
        self.assertIsNotNone(server._cached_ack("s-1", 2))
        self.assertIsNone(server._cached_ack("s-1", 1))


class SendLockTests(unittest.IsolatedAsyncioTestCase):
    """E-002：发送锁跨连接保留，并按上限回收空闲锁。"""

    async def _handshake(self, server):
        async def no_refresh():
            return None

        server._refresh_ready_later = no_refresh
        ws = _FakeWs([json.dumps({"t": "hello", "v": 1})])
        await server._handle_client(ws)
        return ws

    async def test_lock_survives_new_connection(self):
        server = _server()
        await self._handshake(server)
        lock = server._lock_for("group:1")
        epoch = server._epoch

        await self._handshake(server)

        self.assertEqual(server._epoch, epoch + 1)
        self.assertIs(server._lock_for("group:1"), lock)

    async def test_idle_locks_are_recycled(self):
        server = _server()
        for index in range(300):
            server._lock_for(f"group:{index}")
        self.assertLessEqual(len(server._send_locks), bridge.SEND_LOCK_MAX)

    async def test_locked_lock_is_not_recycled(self):
        server = _server()
        keep = server._lock_for("group:keep")
        await keep.acquire()
        try:
            for index in range(300):
                server._lock_for(f"group:other:{index}")
            self.assertIs(server._send_locks.get("group:keep"), keep)
        finally:
            keep.release()


class InflightTests(unittest.IsolatedAsyncioTestCase):
    """E-003：send/tool 并发超限直接回 busy，不新建任务。"""

    async def test_second_send_gets_busy_at_limit(self):
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow_send(*_args, **_kwargs):
            started.set()
            await release.wait()
            return True, "", False, False, ""

        old_limit = bridge.MAX_INFLIGHT
        bridge.MAX_INFLIGHT = 1
        try:
            server = _server(on_send=slow_send)

            async def no_refresh():
                return None

            server._refresh_ready_later = no_refresh
            frames = [
                json.dumps({"t": "hello", "v": 1}),
                json.dumps(dict(SEND_FRAME, rid="s-1")),
                json.dumps(dict(SEND_FRAME, rid="s-2")),
            ]
            ws = _FakeWs(frames)
            task = asyncio.create_task(server._handle_client(ws))
            await started.wait()
            await asyncio.sleep(0)
            release.set()
            await task
            # 被取消的子任务在下一个循环 tick 才跑 finally。
            for _ in range(5):
                if server._inflight == 0:
                    break
                await asyncio.sleep(0)
        finally:
            bridge.MAX_INFLIGHT = old_limit

        acks = {frame["rid"]: frame for frame in ws.frames() if frame.get("t") == "ack"}
        self.assertEqual(acks["s-2"]["error"], codes.BUSY)
        self.assertEqual(acks["s-2"]["note"], "桥正忙，本条没有发出")
        self.assertNotIn("s-2", server._acks)
        self.assertEqual(server._inflight, 0)

    async def test_busy_tool_ack_uses_code(self):
        server = _server(on_tool=_ok_tool)
        ws = _Ws()
        await server._reject_busy(ws, {"t": "tool", "rid": "t-9"})
        self.assertEqual(
            ws.last(),
            {"t": "tool-ack", "rid": "t-9", "ok": False, "error": codes.BUSY},
        )


async def _ok_tool(*_args, **_kwargs):
    return True, "ok"


class HelloTests(unittest.IsolatedAsyncioTestCase):
    """B-001：重复 hello 复用完整校验（header+帧 token+版本）。"""

    async def test_repeat_hello_accepts_non_ascii_token(self):
        server = _server(token="口令")
        ws = _Ws()
        ok = await server._check_hello(
            ws, None, {"t": "hello", "token": "口令"}, "addr"
        )
        self.assertTrue(ok)
        self.assertEqual(ws.closed, [])

    async def test_repeat_hello_rejects_bad_version(self):
        server = _server(token="s3cret")
        ws = _Ws()
        ok = await server._check_hello(
            ws, None, {"t": "hello", "v": 99, "token": "s3cret"}, "addr"
        )
        self.assertFalse(ok)
        self.assertEqual(ws.closed, [(4004, "unsupported protocol version")])
        self.assertEqual(server.version_refused, 1)

    async def test_repeat_hello_accepts_header_only(self):
        server = _server(token="s3cret")
        ws = _Ws()
        ok = await server._check_hello(
            ws, {"authorization": "Bearer s3cret"}, {"t": "hello", "v": 1}, "addr"
        )
        self.assertTrue(ok)
        self.assertEqual(ws.closed, [])

    async def test_repeat_hello_rejects_header_frame_mismatch(self):
        server = _server(token="s3cret")
        ws = _Ws()
        ok = await server._check_hello(
            ws,
            {"authorization": "Bearer s3cret"},
            {"t": "hello", "v": 1, "token": "other"},
            "addr",
        )
        self.assertFalse(ok)
        self.assertEqual(ws.closed, [(4003, "bad token")])

    async def test_repeat_hello_does_not_reregister(self):
        server = _server()
        ws = _Ws()
        server._client = ws
        server._epoch = 3
        server.connected_at = 123.0
        server.client_address = "old-addr"

        await server._on_frame(ws, {"t": "hello", "v": 1, "capabilities": {"images": True}})

        self.assertIs(server._client, ws)
        self.assertEqual(server._epoch, 3)
        self.assertEqual(server.connected_at, 123.0)
        self.assertTrue(server.capabilities.get("images"))
        ready = [frame for frame in ws.frames() if frame.get("t") == "ready"]
        self.assertEqual(len(ready), 1)

    async def test_repeat_hello_bad_version_closes_without_ready(self):
        server = _server()
        ws = _Ws()
        server._client = ws

        await server._on_frame(ws, {"t": "hello", "v": 99})

        self.assertEqual(ws.closed, [(4004, "unsupported protocol version")])
        self.assertEqual([f for f in ws.frames() if f.get("t") == "ready"], [])


class CallbackTypeTests(unittest.TestCase):
    """P-006：SendCallback 注解与实际 5 元组一致。"""

    def test_send_callback_returns_five_tuple(self):
        args = typing.get_args(bridge.SendCallback)
        self.assertEqual(len(args), 2)
        # Callable[..., Awaitable[tuple[...]]]：先剥 Awaitable，再看元组。
        awaited = typing.get_args(args[1])[0]
        self.assertEqual(
            typing.get_args(awaited), (bool, str, bool, bool, str)
        )


class ParseAtTests(unittest.TestCase):
    """发送帧的 @ 校验：只接受 QQ 号或 all，去重，@全体 只留 all。"""

    def test_missing_or_empty(self):
        self.assertEqual(bridge._parse_at(None), ([], "", ""))
        self.assertEqual(bridge._parse_at([]), ([], "", ""))

    def test_numbers_and_duplicates(self):
        self.assertEqual(bridge._parse_at(["10001", "10001", 10002]), (["10001", "10002"], "", ""))

    def test_all_wins(self):
        self.assertEqual(bridge._parse_at(["10001", "all"]), (["all"], "", ""))

    def test_rejects_nickname(self):
        at, code, _note = bridge._parse_at(["小明"])
        self.assertEqual(at, [])
        self.assertEqual(code, "bad_at")

    def test_rejects_too_many(self):
        at, code, _note = bridge._parse_at([str(10000 + i) for i in range(11)])
        self.assertEqual(at, [])
        self.assertEqual(code, "bad_at")

    def test_rejects_non_list(self):
        _at, code, _note = bridge._parse_at("10001")
        self.assertEqual(code, "bad_at")


class ToolIdentityTests(unittest.IsolatedAsyncioTestCase):
    """E-008：缺身份/身份冲突/actor 非法由桥层拒绝。"""

    async def _call(self, frame):
        calls = []

        async def on_tool(name, args, turn, actor):
            calls.append((name, args, turn, actor))
            return True, "ok"

        server = _server(on_tool=on_tool)
        ws = _Ws()
        await server._handle_tool(ws, frame)
        return ws.last(), calls

    async def test_missing_identity_is_refused(self):
        payload, calls = await self._call({"t": "tool", "rid": "t-1", "name": "x"})
        self.assertEqual(payload["error"], codes.MISSING_IDENTITY)
        self.assertFalse(payload["ok"])
        self.assertEqual(calls, [])

    async def test_ambiguous_identity_is_refused(self):
        payload, calls = await self._call(
            {
                "t": "tool",
                "rid": "t-2",
                "name": "x",
                "turn": "m-1",
                "actor": {"kind": "owner", "id": "1"},
            }
        )
        self.assertEqual(payload["error"], codes.AMBIGUOUS_IDENTITY)
        self.assertEqual(calls, [])

    async def test_bad_actor_kind_is_refused(self):
        payload, calls = await self._call(
            {
                "t": "tool",
                "rid": "t-3",
                "name": "x",
                "actor": {"kind": "member", "id": "1"},
            }
        )
        self.assertEqual(payload["error"], codes.BAD_ACTOR)
        self.assertEqual(calls, [])

    async def test_non_dict_actor_is_refused(self):
        payload, calls = await self._call(
            {"t": "tool", "rid": "t-4", "name": "x", "actor": "owner"}
        )
        self.assertEqual(payload["error"], codes.BAD_ACTOR)
        self.assertEqual(calls, [])

    async def test_empty_actor_id_is_refused(self):
        payload, calls = await self._call(
            {
                "t": "tool",
                "rid": "t-5",
                "name": "x",
                "actor": {"kind": "owner", "id": "  "},
            }
        )
        self.assertEqual(payload["error"], codes.BAD_ACTOR)
        self.assertEqual(calls, [])

    async def test_turn_only_still_works(self):
        payload, calls = await self._call(
            {"t": "tool", "rid": "t-6", "name": "x", "turn": "m-1"}
        )
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["result"], "ok")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][2], "m-1")

    async def test_owner_actor_still_works(self):
        payload, calls = await self._call(
            {
                "t": "tool",
                "rid": "t-7",
                "name": "x",
                "actor": {"kind": "owner", "id": "100000001"},
            }
        )
        self.assertTrue(payload["ok"])
        self.assertEqual(calls[0][3], {"kind": "owner", "id": "100000001"})


class ToolErrorTests(unittest.IsolatedAsyncioTestCase):
    """T-004：工具回调异常只回稳定 code + 中文，不带异常原文。"""

    async def test_callback_exception_is_not_echoed(self):
        secret = "https://cdn.example.com/secret?token=SECRET"

        async def on_tool(_name, _args, _turn, _actor):
            raise RuntimeError(secret)

        server = _server(on_tool=on_tool)
        ws = _Ws()
        await server._handle_tool(
            ws, {"t": "tool", "rid": "t-8", "name": "x", "turn": "m-1"}
        )
        ack = ws.last()
        self.assertFalse(ack["ok"])
        self.assertEqual(ack["error"], "tool_failed")
        self.assertEqual(ack["note"], "工具执行失败，详情见插件日志")
        blob = json.dumps(ack, ensure_ascii=False)
        self.assertNotIn("SECRET", blob)
        self.assertNotIn("example.com", blob)

    async def test_business_failure_uses_code_and_note(self):
        """业务失败（权限/白名单）也走 error=code + note=中文。"""

        async def on_tool(_name, _args, _turn, _actor):
            return False, "工具 x 是执行类，需要显式加入 tools_allow"

        server = _server(on_tool=on_tool)
        ws = _Ws()
        await server._handle_tool(
            ws, {"t": "tool", "rid": "t-9", "name": "x", "turn": "m-1"}
        )
        ack = ws.last()
        self.assertFalse(ack["ok"])
        self.assertEqual(ack["error"], "tool_failed")
        self.assertIn("需要显式加入", ack["note"])


class SendErrorTests(unittest.IsolatedAsyncioTestCase):
    """T-004：发送校验失败给稳定 code + 中文 note；异常原文不回执。"""

    async def _ack(self, frame, on_send=None, epoch=None):
        server = _server(on_send=on_send)
        ws = _Ws()
        await server._handle_send(ws, frame, epoch)
        return ws.last()

    async def test_empty_message(self):
        ack = await self._ack(dict(SEND_FRAME, text=""))
        self.assertEqual(ack["error"], codes.EMPTY_MESSAGE)
        self.assertEqual(ack["note"], "正文和图片都是空的")

    async def test_voice_needs_text(self):
        ack = await self._ack(dict(SEND_FRAME, text="  ", voice=True))
        self.assertEqual(ack["error"], codes.VOICE_NEEDS_TEXT)

    async def test_voice_image_conflict(self):
        ack = await self._ack(
            dict(
                SEND_FRAME,
                voice=True,
                image={"mime": "image/png", "base64": "aGk="},
            )
        )
        self.assertEqual(ack["error"], codes.VOICE_IMAGE_CONFLICT)

    async def test_bad_image_type(self):
        ack = await self._ack(
            dict(SEND_FRAME, text="", image={"mime": "text/plain", "base64": "aGk="})
        )
        self.assertEqual(ack["error"], codes.IMAGE_BAD_TYPE)
        self.assertIn("text/plain", ack["note"])

    async def test_image_too_large(self):
        old = bridge.MAX_IMAGE_B64
        bridge.MAX_IMAGE_B64 = 4
        try:
            ack = await self._ack(
                dict(
                    SEND_FRAME,
                    text="",
                    image={"mime": "image/png", "base64": "aGVsbG8="},
                )
            )
        finally:
            bridge.MAX_IMAGE_B64 = old
        self.assertEqual(ack["error"], codes.IMAGE_TOO_LARGE)

    async def test_bad_session(self):
        ack = await self._ack(
            {"t": "send", "rid": "r", "conv": {}, "text": "hi"}
        )
        self.assertEqual(ack["error"], codes.BAD_SESSION)

    async def test_invalid_umo(self):
        ack = await self._ack(dict(SEND_FRAME, umo="bad"))
        self.assertEqual(ack["error"], codes.INVALID_UMO)
        self.assertEqual(ack["note"], "UMO 格式不正确")

    async def test_umo_conv_mismatch(self):
        ack = await self._ack(
            dict(SEND_FRAME, umo="default:FriendMessage:999")
        )
        self.assertEqual(ack["error"], codes.UMO_CONV_MISMATCH)
        self.assertEqual(ack["note"], "UMO 与会话参数不一致")

    async def test_send_queue_timeout(self):
        server = _server()
        lock = server._lock_for("group:123456")
        await lock.acquire()
        old = bridge.SEND_QUEUE_TIMEOUT
        bridge.SEND_QUEUE_TIMEOUT = 0.01
        try:
            ws = _Ws()
            await server._handle_send(ws, dict(SEND_FRAME))
            ack = ws.last()
        finally:
            bridge.SEND_QUEUE_TIMEOUT = old
            lock.release()
        self.assertEqual(ack["error"], codes.SEND_QUEUE_TIMEOUT)
        self.assertIn("排队", ack["note"])

    async def test_callback_exception_maps_to_send_failed(self):
        async def boom(*_args, **_kwargs):
            raise RuntimeError("https://api.example?key=SECRET at C:\\secret\\path")

        ack = await self._ack(dict(SEND_FRAME), on_send=boom)
        self.assertFalse(ack["ok"])
        self.assertEqual(ack["error"], codes.SEND_FAILED)
        self.assertTrue(ack.get("uncertain"))
        self.assertEqual(ack["note"], "发送失败，详情见插件日志")
        self.assertNotIn("SECRET", json.dumps(ack, ensure_ascii=False))
        self.assertNotIn("api.example", json.dumps(ack, ensure_ascii=False))

    async def test_callback_failure_keeps_code_and_note(self):
        async def fail(*_args, **_kwargs):
            return False, "平台发送超时，结果未知", False, True, codes.SEND_TIMEOUT

        ack = await self._ack(dict(SEND_FRAME), on_send=fail)
        self.assertEqual(ack["error"], codes.SEND_TIMEOUT)
        self.assertEqual(ack["note"], "平台发送超时，结果未知")

    async def test_legacy_failure_without_code_gets_send_failed(self):
        async def legacy(*_args, **_kwargs):
            return False, "老的失败说明", False, False, ""

        ack = await self._ack(dict(SEND_FRAME), on_send=legacy)
        self.assertEqual(ack["error"], codes.SEND_FAILED)
        self.assertEqual(ack["note"], "老的失败说明")

    async def test_success_keeps_note_without_error(self):
        async def ok_with_note(*_args, **_kwargs):
            return True, "语音回退为文字", False, False, ""

        ack = await self._ack(dict(SEND_FRAME), on_send=ok_with_note)
        self.assertTrue(ack["ok"])
        self.assertNotIn("error", ack)
        self.assertEqual(ack["note"], "语音回退为文字")


if __name__ == "__main__":
    unittest.main()
