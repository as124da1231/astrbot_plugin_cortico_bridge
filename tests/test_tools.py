"""工具门回归：risk fail-closed（T-001）、权限拒绝（T-002）、异常脱敏（D-005）。

tools.py 用包内相对导入，所以按插件包导入；astrbot 只 stub 到用得到的层。
"""

import asyncio
import importlib.util
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))


class _Logger:
    def __init__(self):
        self.records = []

    def _add(self, level, args):
        self.records.append((level, " ".join(str(a) for a in args)))

    def debug(self, *args, **kwargs):
        self._add("debug", args)

    def info(self, *args, **kwargs):
        self._add("info", args)

    def warning(self, *args, **kwargs):
        self._add("warning", args)

    def error(self, *args, **kwargs):
        self._add("error", args)


logger = _Logger()

astrbot = types.ModuleType("astrbot")
api = types.ModuleType("astrbot.api")
api.logger = logger
api.AstrBotConfig = dict
astrbot.api = api

event_mod = types.ModuleType("astrbot.api.event")


class _MessageChain:
    def __init__(self):
        self.chain = []

    def append(self, item):
        self.chain.append(item)


event_mod.MessageChain = _MessageChain
event_mod.AstrMessageEvent = object
event_mod.filter = types.SimpleNamespace()
api.event = event_mod

star_mod = types.ModuleType("astrbot.api.star")
star_mod.Context = object
star_mod.Star = object
star_mod.StarTools = types.SimpleNamespace(get_data_dir=lambda _name: "")
api.star = star_mod

# execute() 在函数内 import 这两个包装类。
core = types.ModuleType("astrbot.core")
core.__path__ = []
agent_pkg = types.ModuleType("astrbot.core.agent")
agent_pkg.__path__ = []
run_context = types.ModuleType("astrbot.core.agent.run_context")


class ContextWrapper:
    def __init__(self, ctx):
        self.ctx = ctx


run_context.ContextWrapper = ContextWrapper
agent_context = types.ModuleType("astrbot.core.astr_agent_context")


class AstrAgentContext:
    def __init__(self, context=None, event=None):
        self.context = context
        self.event = event


agent_context.AstrAgentContext = AstrAgentContext
core.agent = agent_pkg

sys.modules["astrbot"] = astrbot
sys.modules["astrbot.api"] = api
sys.modules["astrbot.api.event"] = event_mod
sys.modules["astrbot.api.star"] = star_mod
sys.modules["astrbot.core"] = core
sys.modules["astrbot.core.agent"] = agent_pkg
sys.modules["astrbot.core.agent.run_context"] = run_context
sys.modules["astrbot.core.astr_agent_context"] = agent_context

# 不依赖仓库目录名：把插件根目录注册为合成包 `plugin`
spec = importlib.util.spec_from_loader("plugin", None, is_package=True)
spec.submodule_search_locations = [str(ROOT)]
sys.modules.setdefault("plugin", importlib.util.module_from_spec(spec))

from plugin import tools as tools_mod  # noqa: E402
from plugin.tools import ToolRegistry  # noqa: E402

# 全量 discover 时 main.py 可能先加载 tools，把 logger 绑到别的 stub 上。
tools_mod.logger = logger


class FakeTool:
    def __init__(self, name, description="", active=True, result=None, error=None):
        self.name = name
        self.description = description
        self.active = active
        self.result = result if result is not None else f"{name} ok"
        self.error = error
        self.calls = []

    async def call(self, wrapped, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.result


class _FullSet:
    def __init__(self, tools):
        self._tools = {tool.name: tool for tool in tools}

    def get_tool(self, name):
        return self._tools.get(name)


class _Manager:
    def __init__(self, tools):
        self.func_list = list(tools)
        self._full = _FullSet(tools)

    def get_full_tool_set(self):
        return self._full


class _Ctx:
    def __init__(self, tools):
        self._manager = _Manager(tools)

    def get_llm_tool_manager(self):
        return self._manager


class _BrokenCtx:
    def get_llm_tool_manager(self):
        raise RuntimeError("catalog unavailable")


class _Result:
    def __init__(self, text, success=None):
        self.text = text
        if success is not None:
            self.success = success

    def get_plain_text(self):
        return self.text


def _tools():
    return [
        FakeTool("run_shell", "execute shell command"),
        FakeTool("play_song", "play a song"),
        FakeTool("query_weather", "get weather"),
    ]


def _registry(allow, tools=None, risks=("play_song:write", "query_weather:read")):
    config = {"tools_allow": allow, "tools_risks": list(risks)}
    tools = tools if tools is not None else _tools()
    return ToolRegistry(_Ctx(tools), lambda: config), tools


class RiskGateTests(unittest.TestCase):
    def test_unreviewed_tools_require_explicit_grant_and_trust(self):
        tool = FakeTool("maintenance", "Read system status")
        registry, _ = _registry(["*"], [tool], risks=())
        self.assertEqual(registry.frame()["items"], [])
        self.assertFalse(asyncio.run(registry.execute("maintenance", {}, object(), trusted=True))[0])
        registry, _ = _registry(["maintenance"], [tool], risks=())
        self.assertFalse(asyncio.run(registry.execute("maintenance", {}, object()))[0])
        self.assertEqual(tool.calls, [])

    def test_risk_for_uses_complete_catalog_and_fails_closed(self):
        registry, _tools_list = _registry(["*"])
        self.assertEqual(registry.risk_for("run_shell"), "exec")
        self.assertEqual(registry.risk_for("play_song"), "write")
        self.assertEqual(registry.risk_for("query_weather"), "read")
        self.assertEqual(registry.risk_for("missing_tool"), "exec")

    def test_broken_catalog_fails_closed(self):
        registry = ToolRegistry(_BrokenCtx(), lambda: {"tools_allow": ["*"]})
        self.assertEqual(registry.risk_for("query_weather"), "exec")

    def test_star_excludes_exec_from_frame(self):
        registry, _tools_list = _registry(["*"])
        names = [item["name"] for item in registry.frame()["items"]]
        self.assertNotIn("run_shell", names)
        self.assertIn("play_song", names)
        self.assertIn("query_weather", names)

    def test_explicit_exec_is_still_listed_under_star(self):
        registry, _tools_list = _registry(["*", "run_shell"])
        names = [item["name"] for item in registry.frame()["items"]]
        self.assertIn("run_shell", names)

    def test_star_exec_is_refused_without_explicit_name(self):
        registry, tools = _registry(["*"])
        ok, text = asyncio.run(
            registry.execute("run_shell", {}, object(), trusted=True)
        )
        self.assertFalse(ok)
        self.assertIn("显式", text)
        self.assertEqual(tools[0].calls, [])

    def test_explicit_exec_refused_for_untrusted_actor(self):
        registry, tools = _registry(["run_shell"])
        ok, text = asyncio.run(
            registry.execute("run_shell", {}, object(), trusted=False)
        )
        self.assertFalse(ok)
        self.assertIn("执行权限", text)
        self.assertEqual(tools[0].calls, [])

    def test_explicit_exec_allowed_for_trusted_actor(self):
        registry, tools = _registry(["run_shell"])
        ok, _text = asyncio.run(
            registry.execute("run_shell", {"cmd": "ls"}, object(), trusted=True)
        )
        self.assertTrue(ok)
        self.assertEqual(tools[0].calls, [{"cmd": "ls"}])

    def test_star_read_write_is_unchanged(self):
        registry, _tools_list = _registry(["*"])
        self.assertTrue(
            asyncio.run(registry.execute("query_weather", {}, object()))[0]
        )
        self.assertTrue(
            asyncio.run(registry.execute("play_song", {}, object()))[0]
        )

    def test_unknown_tool_is_refused(self):
        registry, _tools_list = _registry(["*"])
        ok, _text = asyncio.run(
            registry.execute("not_installed", {}, object(), trusted=True)
        )
        self.assertFalse(ok)

    def test_docstring_documents_both_identity_paths(self):
        doc = tools_mod.__doc__ or ""
        self.assertIn("QQ turn", doc)
        self.assertIn("Coo owner actor", doc)


class PermissionResultTests(unittest.TestCase):
    def _run(self, result, allow=None):
        tool = FakeTool("query_weather", "get weather", result=result)
        registry, _tools_list = _registry(allow or ["query_weather"], [tool])
        return asyncio.run(
            registry.execute("query_weather", {}, object())
        )

    def test_permission_text_is_failure(self):
        ok, text = self._run("你没有权限使用此工具")
        self.assertFalse(ok)
        self.assertIn("权限", text)

    def test_object_success_false_is_failure(self):
        ok, _text = self._run(_Result("denied", success=False))
        self.assertFalse(ok)

    def test_object_without_success_checks_plain_text(self):
        ok, _text = self._run(_Result("你没有权限"))
        self.assertFalse(ok)

    def test_help_text_with_not_allowed_is_not_misjudged(self):
        ok, text = self._run(
            "Usage: /help. This command is not allowed in private chat."
        )
        self.assertTrue(ok)
        self.assertIn("not allowed", text)

    def test_exception_text_is_sanitized(self):
        logger.records.clear()
        tool = FakeTool(
            "query_weather",
            "get weather",
            error=RuntimeError("C:/secret/config.json?key=SECRET"),
        )
        registry, _tools_list = _registry(["query_weather"], [tool])
        ok, text = asyncio.run(registry.execute("query_weather", {}, object()))
        self.assertFalse(ok)
        self.assertNotIn("SECRET", text)
        self.assertNotIn("C:/secret", text)
        self.assertFalse(any("SECRET" in item for _lvl, item in logger.records))


if __name__ == "__main__":
    unittest.main()
