"""给 Cortico 用的工具白名单与调用。

三件事：
- 从 AstrBot 的运行时工具表读出「有哪些工具」，按来源插件分组、打风险标签；
- 白名单直接读插件配置里的 `tools_allow`（AstrBot 配置页），不另存一份；
- 按名字执行工具，走 AstrBot 的 `_PermissionGuardedTool`。

身份规则有两条路径：
- QQ turn：用**触发这一轮的那条 QQ 消息**的事件执行，身份是发言者；群友点歌就是
  群友自己的身份，管理员工具由 AstrBot 权限层拦。
- Coo owner actor：Coo 对话轮次在「连接」里填了主人 QQ 时，允许以该 owner 身份执行；
  `trusted` 由 main 按 `_admin_ids()` 计算，本模块不信任 World 传来的 role/owner 字段。

`*` 只自动包含 read/write；exec 必须显式点名，且要求 `trusted=True`。
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import Any, Callable

from astrbot.api import logger

from .bridge import MAX_TOOL_RESULT

# 工具清单缓存时间：运行时工具表不会频繁变，没必要每轮都扫。
CATALOG_TTL = 30.0

# 只在执行结果里兜底识别「权限拒绝」。刻意不收 `not allowed`：帮助文本里
# 也常有这句话，会误判成失败；优先看原始结果的 `success` 字段。
_DENIED = re.compile(
    r"(权限|无权|permission\s+denied|access\s+denied|forbidden|not\s+permitted)",
    re.IGNORECASE,
)


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

    def declared_risks(self) -> dict[str, str]:
        raw = self._config().get("tools_risks")
        entries = raw if isinstance(raw, list) else []
        risks = {}
        for entry in entries:
            name, _, risk = _s(entry).rpartition(":")
            name, risk = name.strip(), risk.strip().lower()
            if name and risk in ("read", "write", "exec"):
                risks[name] = risk
        return risks

    # ---------- 清单 ----------

    def catalog(self, force: bool = False) -> list[dict]:
        risks = self.declared_risks()
        now = time.time()
        if not force and self._catalog and now - self._catalog_at < CATALOG_TTL:
            return [{**row, "risk": risks.get(row["name"], "exec")} for row in self._catalog]
        rows: list[dict] = []
        labels = _plugin_labels()
        try:
            tools = self.context.get_llm_tool_manager().func_list
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Cortico Bridge: 读工具表失败 —— {exc}")
            return []
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
                    "risk": risks.get(name, "exec"),
                    "active": bool(getattr(tool, "active", True)),
                    "sig": _signature(getattr(tool, "parameters", None)),
                }
            )
        rows.sort(key=lambda r: (r["plugin"], r["name"]))
        self._catalog = rows
        self._catalog_at = now
        return rows

    def _catalog_row(self, name: str) -> dict | None:
        """从完整 catalog（不是 enabled_catalog）按 name 取一行。"""
        target = _s(name).strip()
        if not target:
            return None
        for row in self.catalog():
            if row.get("name") == target:
                return row
        return None

    def risk_for(self, name: str) -> str:
        """查一个工具的风险；catalog 读失败/查不到一律按 `exec`（fail-closed）。"""
        row = self._catalog_row(name)
        if row is None:
            return "exec"
        return _s(row.get("risk")) or "exec"

    def enabled_catalog(self, force: bool = False) -> list[dict]:
        """已启用且可下发的工具。

        优先级：显式点名永远包含（即使 risk=exec）；只写了 `*` 时排除 exec，
        避免把执行类工具悄悄放给普通群友。
        """
        allow = self.allow_list()
        star = "*" in allow
        picked: list[dict] = []
        for row in self.catalog(force=force):
            if not row.get("active"):
                continue
            name = row.get("name", "")
            if name in allow:
                picked.append(row)
            elif star and row.get("risk") != "exec":
                picked.append(row)
        return picked

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
        trusted: bool = False,
    ) -> tuple[bool, str]:
        """按名字执行一个工具。返回 (成功, 文本)。

        `trusted` 只由 main 按 `_admin_ids()` 计算；本模块不信任 World 传来的
        role/owner 字段。exec 工具必须在 `tools_allow` 里显式点名，且 trusted。
        """
        name = _s(name).strip()
        allow = self.allow_list()
        explicit = name in allow
        if not name or not ("*" in allow or explicit):
            return False, f"工具 {name or '（空名）'} 没有在插件的 tools_allow 里"

        row = self._catalog_row(name)
        if row is None:
            return False, f"工具 {name} 不在可用清单里，不能执行"
        risk = row["risk"]
        if risk == "exec":
            if not explicit:
                return False, f"工具 {name} 是执行类，需要显式加入 tools_allow"
            if not trusted:
                return False, f"工具 {name} 是执行类，当前身份没有执行权限"

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
            # 插件异常可能包含服务地址和凭据，只记录异常类型。
            logger.warning(
                "Cortico Bridge: 工具 %s 执行失败（%s）", name, type(exc).__name__
            )
            return False, f"工具 {name} 执行失败，详情见插件日志"

        # T-002：权限拒绝可能以对象字段或字符串返回，不能一律算成功。
        if getattr(result, "success", None) is False:
            text = _render(result).strip()
            return False, text or f"工具 {name} 执行失败"
        text = _render(result)
        if _DENIED.search(text or ""):
            return False, text
        return True, text


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
