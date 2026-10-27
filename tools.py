"""给 Cortico 用的工具白名单与调用。

三件事：
- 从 AstrBot 的运行时工具表读出「有哪些工具」，按来源插件分组、打风险标签；
- 白名单直接读插件配置里的 `tools_allow`（AstrBot 配置页），不另存一份；
- 按名字执行工具，走 AstrBot 的 `_PermissionGuardedTool`，权限判断照旧用触发消息的事件。

身份规则：工具用**触发这一轮的那条消息**的事件执行，不用主人顶替。
群友点歌就是群友的身份，管理员工具会被 AstrBot 自己的权限层拦下。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Callable

from astrbot.api import logger

from .bridge import MAX_TOOL_RESULT

# 工具清单缓存时间：运行时工具表不会频繁变，没必要每轮都扫。
CATALOG_TTL = 30.0


def _s(value: Any) -> str:
    return "" if value is None else str(value)


def plugin_of(module_path: str) -> str:
    """`data.plugins.astrbot_plugin_x.main` → `astrbot_plugin_x`。"""
    parts = [p for p in module_path.split(".") if p]
    for part in parts:
        if part.startswith("astrbot_plugin_"):
            return part
    return parts[-2] if len(parts) >= 2 else (module_path or "未知来源")


def _plugin_labels() -> dict[str, str]:
    """插件目录名 → 插件自己声明的显示名。读不到就返回空表，调用方兜底。"""
    try:
        from astrbot.core.star.star import star_registry
    except Exception:  # noqa: BLE001
        return {}
    labels: dict[str, str] = {}
    for meta in star_registry:
        root = _s(getattr(meta, "root_dir_name", "")).strip()
        if not root:
            continue
        label = _s(getattr(meta, "display_name", "")).strip()
        if not label:
            label = _s(getattr(meta, "name", "")).strip()
        if label:
            labels[root] = label
    return labels


def plugin_label(name: str) -> str:
    """兜底名字：astrbot_plugin_music → music。"""
    short = name.replace("astrbot_plugin_", "").replace("_", " ").strip()
    return short or name or "未知来源"


def risk_of(name: str, description: str) -> str:
    """粗分三档：exec / write / read。分错不要紧，重要的是默认全关。"""
    text = f"{name} {description}".lower()
    exec_words = ("shell", "exec", "python", "browser", "click", "script", "terminal")
    write_words = (
        "send",
        "reply",
        "post",
        "upload",
        "delete",
        "write",
        "set_",
        "create",
        "bind",
        "block",
        "poke",
        "play",
        "trigger",
        "steal",
        "comment",
        "like",
    )
    if any(w in text for w in exec_words):
        return "exec"
    if any(w in text for w in write_words):
        return "write"
    return "read"


def _signature(parameters: Any) -> str:
    """把 JSON Schema 压成一行参数签名：必填原名，可选带 `?`。"""
    if not isinstance(parameters, dict):
        return ""
    props = parameters.get("properties")
    if not isinstance(props, dict) or not props:
        return ""
    required = parameters.get("required")
    need = set(required) if isinstance(required, list) else set()
    parts = []
    for key in props:
        name = _s(key)
        if not name:
            continue
        parts.append(name if name in need else f"{name}?")
    return ", ".join(parts)


class ToolRegistry:
    """工具清单 + 白名单（配置驱动）+ 执行。"""

    def __init__(self, context, config: Callable[[], dict]) -> None:
        self.context = context
        self._config = config
        self._catalog: list[dict] = []
        self._catalog_at = 0.0

    # ---------- 白名单 ----------

    def allow_list(self) -> list[str]:
        """配置里的 `tools_allow`；`*` 表示全部。空列表表示一个都不给。"""
        raw = self._config().get("tools_allow", [])
        if isinstance(raw, str):
            items = raw.replace("，", ",").replace("\n", ",").split(",")
        elif isinstance(raw, list):
            items = [_s(x) for x in raw]
        else:
            items = []
        out: list[str] = []
        for item in items:
            name = item.strip()
            if name and name not in out:
                out.append(name)
        return out

    def allows(self, name: str) -> bool:
        allow = self.allow_list()
        return "*" in allow or name in allow

    # ---------- 清单 ----------

    def catalog(self, force: bool = False) -> list[dict]:
        now = time.time()
        if not force and self._catalog and now - self._catalog_at < CATALOG_TTL:
            return self._catalog
        rows: list[dict] = []
        labels = _plugin_labels()
        try:
            tools = self.context.get_llm_tool_manager().func_list
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Cortico Bridge: 读工具表失败 —— {exc}")
            return self._catalog
        for tool in tools:
            name = _s(getattr(tool, "name", ""))
            if not name:
                continue
            desc = " ".join(_s(getattr(tool, "description", "")).split())
            plugin = plugin_of(_s(getattr(tool, "handler_module_path", "")))
            rows.append(
                {
                    "name": name,
                    "desc": desc[:300],
                    "plugin": plugin,
                    "plugin_label": labels.get(plugin) or plugin_label(plugin),
                    "risk": risk_of(name, desc),
                    "active": bool(getattr(tool, "active", True)),
                    "sig": _signature(getattr(tool, "parameters", None)),
                }
            )
        rows.sort(key=lambda r: (r["plugin"], r["name"]))
        self._catalog = rows
        self._catalog_at = now
        return rows

    def enabled_catalog(self, force: bool = False) -> list[dict]:
        return [r for r in self.catalog(force=force) if self.allows(r["name"]) and r["active"]]

    def frame(self) -> dict:
        """握手后发给 World 的工具清单（只发启用且可用的）。

        强制重建：插件启动时别的插件可能还没注册完工具，缓存 30 秒会把旧清单发出去。
        """
        return {
            "t": "tools",
            "items": [
                {
                    "name": r["name"],
                    "risk": r["risk"],
                    "desc": r["desc"][:120],
                    "sig": r.get("sig", ""),
                }
                for r in self.enabled_catalog(force=True)
            ],
        }

    def startup_summary(self) -> str:
        """启动时打一行，方便在 AstrBot 配置页照着填。"""
        catalog = self.catalog(force=True)
        allow = self.allow_list()
        names = "、".join(r["name"] for r in catalog) or "（无）"
        picked = "全部（*）" if "*" in allow else (f"{len(allow)} 个" if allow else "一个都不给")
        return f"可调工具 {len(catalog)} 个，已选 {picked}；清单：{names}"

    # ---------- 执行 ----------

    async def execute(
        self,
        name: str,
        args: dict | None,
        event,
        timeout: float = 60.0,
    ) -> tuple[bool, str]:
        """按名字执行一个工具。返回 (成功, 文本)。"""
        if not self.allows(name):
            return False, f"工具 {name} 没有在插件的 tools_allow 里"
        try:
            from astrbot.core.agent.run_context import ContextWrapper
            from astrbot.core.astr_agent_context import AstrAgentContext

            tool = self.context.get_llm_tool_manager().get_full_tool_set().get_tool(name)
            if tool is None:
                return False, f"没有这个工具：{name}"
            if not getattr(tool, "active", True):
                return False, f"工具 {name} 在 AstrBot 里是停用的"
            wrapped = ContextWrapper(AstrAgentContext(context=self.context, event=event))
            result = await asyncio.wait_for(
                tool.call(wrapped, **(args or {})),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            return False, f"{name} 超过 {int(timeout)} 秒没有返回"
        except Exception as exc:  # noqa: BLE001
            reason = str(exc).strip() or type(exc).__name__
            logger.warning(f"Cortico Bridge: 工具 {name} 执行失败 —— {reason}")
            return False, reason
        return True, _render(result)


def _render(result: Any) -> str:
    """把工具的返回值收敛成给模型看的一段文字。"""
    if result is None:
        # 有些工具（点歌、发图）自己把内容发出去，只返回 None；别让模型以为失败了。
        return "（执行完了，没有文字返回。会自己发消息的工具，内容已经直接发出去了。）"
    if isinstance(result, str):
        return result[:MAX_TOOL_RESULT]
    get_text = getattr(result, "get_plain_text", None)
    if callable(get_text):
        try:
            text = _s(get_text()).strip()
        except Exception:  # noqa: BLE001
            text = ""
        if text:
            return text[:MAX_TOOL_RESULT]
        return "（工具只返回了非文字内容，它自己已经发出去了）"
    try:
        return _s(result)[:MAX_TOOL_RESULT]
    except Exception:  # noqa: BLE001
        return "（工具返回了无法展示的内容）"
