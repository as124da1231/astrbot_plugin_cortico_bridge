"""投递层：把 Cortico 要说的文字发到 QQ，并记住每个会话的 umo。

两件事分开：
- umo 登记：按「平台实例 + 会话」索引，避免两个 QQ 账号在同一个群时互相覆盖。
- 发送：带每分钟上限，失败一律返回 (False, 原因)，不抛。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import time
from typing import Any, Callable

from astrbot.api import logger

CONV_KINDS = ("group", "private")

# 落盘间隔：群消息密集时别每来一条就写一次盘。
UMO_SAVE_INTERVAL = 10.0
# 最多记住多少个会话；和内存表一致。
UMO_MAX = 500
# 超过这个时间没再出现的会话，不能再作为发送目标。
UMO_TTL_SECONDS = 30 * 24 * 60 * 60
# 单次平台发送的上限；超时后结果就是「未知」，不能当成功，也不能当没发。
SEND_TIMEOUT = 20.0


def _s(value: Any) -> str:
    return "" if value is None else str(value)


class Delivery:
    def __init__(self, context, config: Callable[[], dict], data_dir: str = ""):
        self._context = context
        self._config = config
        # UMO 是主键；旧键 "平台|类型|会话" 只做迁移索引。
        self._umos: dict[str, dict] = {}
        self._legacy: dict[str, str] = {}
        self._send_stamps: list[float] = []
        # 重启后会话表为空会发不出去；把 umo 落到插件数据目录。
        self._umos_file = os.path.join(data_dir, "umos.json") if data_dir else ""
        self._last_save = 0.0
        self._dirty = False
        # 最近一次发送失败的结构化错误码；bridge 用它写 ack.error。
        self.last_error_code = ""
        self._load_umos()

    # ---------- umo 登记 ----------

    @staticmethod
    def _parse_umo(value: str):
        """platform:MsgType:session；session 里允许冒号。"""
        text = _s(value).strip()
        if not text or len(text) > 512:
            return None
        parts = text.split(":", 2)
        if len(parts) != 3:
            return None
        platform, msg_type, session = parts
        if not platform or not msg_type or not session:
            return None
        if msg_type not in ("GroupMessage", "FriendMessage"):
            return None
        kind = "group" if msg_type == "GroupMessage" else "private"
        return platform, kind, session

    def _load_umos(self) -> None:
        if not self._umos_file or not os.path.isfile(self._umos_file):
            return
        try:
            with open(self._umos_file, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Cortico Bridge: 读取会话表失败，按空表启动 —— {exc}")
            return
        if not isinstance(raw, dict):
            return

        migrated = False
        version = raw.get("version")
        umos = raw.get("umos") if isinstance(raw.get("umos"), dict) else {}
        legacy = raw.get("legacy") if isinstance(raw.get("legacy"), dict) else {}

        if version == 2 and umos:
            for key, item in umos.items():
                if not isinstance(item, dict):
                    continue
                umo = _s(item.get("umo") or key).strip()
                parsed = self._parse_umo(umo)
                if parsed is None:
                    continue
                platform, kind, conv_id = parsed
                try:
                    ts = float(item.get("lastSeen") or 0)
                except (TypeError, ValueError):
                    ts = 0.0
                self._umos[umo] = {
                    "umo": umo,
                    "platform": _s(item.get("platform") or platform),
                    "kind": _s(item.get("kind") or kind),
                    "id": _s(item.get("id") or conv_id),
                    "name": _s(item.get("name")),
                    "firstSeen": float(item.get("firstSeen") or ts or 0),
                    "lastSeen": ts,
                    "lastMessageId": _s(item.get("lastMessageId")),
                    "lastSender": _s(item.get("lastSender")),
                }
            for key, value in legacy.items():
                target = _s(value).strip()
                if target:
                    self._legacy[_s(key)] = target
        else:
            # v1：键是「平台|类型|会话」，值是 {umo, ts} 或 [umo, ts]。
            for key, value in raw.items():
                if key in ("version", "umos", "legacy"):
                    continue
                umo = ""
                ts = 0.0
                if isinstance(value, dict):
                    umo = _s(value.get("umo")).strip()
                    try:
                        ts = float(value.get("ts") or 0)
                    except (TypeError, ValueError):
                        ts = 0.0
                elif isinstance(value, (list, tuple)) and value:
                    umo = _s(value[0]).strip()
                    if len(value) > 1:
                        try:
                            ts = float(value[1])
                        except (TypeError, ValueError):
                            ts = 0.0
                if self._parse_umo(umo) is None or "|" not in _s(key):
                    continue
                parts = _s(key).split("|", 2)
                if len(parts) != 3:
                    continue
                platform, kind, conv_id = parts
                self._umos[umo] = {
                    "umo": umo,
                    "platform": platform,
                    "kind": kind,
                    "id": conv_id,
                    "name": "",
                    "firstSeen": ts,
                    "lastSeen": ts,
                    "lastMessageId": "",
                    "lastSender": "",
                }
                self._legacy[_s(key)] = umo
                migrated = True

        if version != 2 and migrated:
            backup = os.path.join(os.path.dirname(self._umos_file), "umos.v1.json")
            if not os.path.exists(backup):
                try:
                    shutil.copy2(self._umos_file, backup)
                    with contextlib.suppress(OSError):
                        os.chmod(backup, 0o600)
                    logger.info("Cortico Bridge: 已备份旧会话表到 %s", backup)
                except OSError as exc:  # noqa: BLE001
                    logger.warning("Cortico Bridge: 备份旧会话表失败 —— %s", exc)

        self._prune()
        if migrated:
            self._save_umos(force=True)
            logger.info(f"Cortico Bridge: 会话表已迁移到 UMO v2，共 {len(self._umos)} 条")
        elif self._umos:
            logger.info(f"Cortico Bridge: 从磁盘恢复 {len(self._umos)} 个 UMO")

    def _fresh(self, ts: float) -> bool:
        return bool(ts) and time.time() - ts <= UMO_TTL_SECONDS

    def _prune(self) -> None:
        if len(self._umos) > UMO_MAX:
            keep = sorted(
                self._umos.items(),
                key=lambda kv: float(kv[1].get("lastSeen") or 0),
                reverse=True,
            )[:UMO_MAX]
            self._umos = dict(keep)
        alive = set(self._umos)
        self._legacy = {k: v for k, v in self._legacy.items() if v in alive}

    def _save_umos(self, force: bool = False) -> None:
        if not self._umos_file:
            return
        now = time.time()
        if not force and now - self._last_save < UMO_SAVE_INTERVAL:
            self._dirty = True
            return
        try:
            os.makedirs(os.path.dirname(self._umos_file), exist_ok=True)
            payload = {
                "version": 2,
                "umos": {umo: dict(item) for umo, item in self._umos.items()},
                "legacy": dict(self._legacy),
            }
            tmp = self._umos_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=0)
            with contextlib.suppress(OSError):
                os.chmod(tmp, 0o600)
            os.replace(tmp, self._umos_file)
            self._last_save = now
            self._dirty = False
        except Exception as exc:  # noqa: BLE001
            self._dirty = True
            logger.debug(f"Cortico Bridge: 会话表落盘失败 —— {exc}")

    def flush(self) -> None:
        if self._dirty or self._last_save == 0:
            self._save_umos(force=True)

    def remember(
        self,
        platform_id: str,
        conv_kind: str,
        conv_id: str,
        umo: str,
        message_id: str = "",
        sender: str = "",
    ) -> None:
        target = _s(umo).strip()
        parsed = self._parse_umo(target)
        if parsed is None:
            return
        platform, kind, session = parsed
        now = time.time()
        entry = self._umos.get(target)
        if entry is None:
            entry = {
                "umo": target,
                "platform": platform,
                "kind": kind,
                "id": session,
                "name": "",
                "firstSeen": now,
                "lastSeen": now,
                "lastMessageId": "",
                "lastSender": "",
            }
            self._umos[target] = entry
        else:
            entry["platform"] = platform
            entry["kind"] = kind
            entry["id"] = session
            entry["lastSeen"] = now
        if message_id:
            entry["lastMessageId"] = _s(message_id)
        if sender:
            entry["lastSender"] = _s(sender)
        if platform_id and conv_kind and conv_id:
            self._legacy[f"{platform_id}|{conv_kind}|{conv_id}"] = target
        self._prune()
        self._save_umos()

    def has_umo(self, umo: str) -> bool:
        """桥最近见过的 UMO；只允许向这些会话发送。"""
        target = _s(umo).strip()
        entry = self._umos.get(target)
        return bool(entry and self._fresh(float(entry.get("lastSeen") or 0)))

    def umo_for(self, platform_id: str, conv_kind: str, conv_id: str) -> str | None:
        """由旧会话键找 UMO；只在最近见过的条目里找。

        精确命中也要检查 TTL；后缀匹配必须唯一，多个平台同时见过就返回 None，
        不按「最近」猜，避免 default/default1 混发。
        """
        if platform_id:
            hit = self._legacy.get(f"{platform_id}|{conv_kind}|{conv_id}")
            if hit and self.has_umo(hit):
                return hit
        suffix = f"|{conv_kind}|{conv_id}"
        candidates = {
            umo
            for key, umo in self._legacy.items()
            if key.endswith(suffix) and self.has_umo(umo)
        }
        if len(candidates) == 1:
            return next(iter(candidates))
        return None

    def known_conversations(self) -> list[dict]:
        now = time.time()
        rows = []
        for umo, item in self._umos.items():
            ts = float(item.get("lastSeen") or 0)
            if not self._fresh(ts):
                continue
            rows.append(
                {
                    "key": umo,
                    "umo": umo,
                    "platform": item.get("platform", ""),
                    "kind": item.get("kind", ""),
                    "id": item.get("id", ""),
                    "name": item.get("name", ""),
                    "age_sec": round(now - ts),
                }
            )
        return sorted(rows, key=lambda row: row["age_sec"])

    # ---------- 发送 ----------

    def _limit(self) -> int:
        try:
            return int(self._config().get("max_sends_per_minute", 30) or 0)
        except (TypeError, ValueError):
            return 30

    def _tts_enabled(self) -> bool:
        return bool(self._config().get("tts_enabled", False))

    def _tts_limit(self) -> int:
        try:
            return int(self._config().get("tts_max_chars", 120) or 0)
        except (TypeError, ValueError):
            return 120

    def allow(self) -> bool:
        limit = self._limit()
        if limit <= 0:
            return True
        now = time.time()
        self._send_stamps = [t for t in self._send_stamps if now - t < 60]
        if len(self._send_stamps) >= limit:
            return False
        self._send_stamps.append(now)
        return True

    def _find_provider(self, kind: str, provider_id: str):
        """按 id 从 AstrBot 的 provider 实例表里找；找不到返回 None。

        这是「只借注册表」的关键：不依赖 AstrBot 的「当前 provider」，
        也就不受 provider_tts_settings.enable 这个全局开关影响。
        """
        getter = getattr(self._context, f"get_all_{kind}_providers", None)
        if not callable(getter):
            return None
        try:
            providers = getter() or []
        except Exception:  # noqa: BLE001
            return None
        for provider in providers:
            try:
                if _s(getattr(provider.meta(), "id", "")) == provider_id:
                    return provider
            except Exception:  # noqa: BLE001
                continue
        return None

    async def _tts_provider(self, umo: str):
        """拿 TTS provider：配置指定优先，其次才是 AstrBot 的当前 provider。

        指定了 id 却找不到时直接返回 None，不偷偷换成别的 provider；
        否则面板显示 A、实际用 B，语音会变得不可预期。
        """
        wanted = _s(self._config().get("tts_provider_id", "")).strip()
        if wanted:
            found = self._find_provider("tts", wanted)
            if found is None:
                logger.warning(
                    "Cortico Bridge: 配置的 TTS provider %s 没有找到，本条不发语音" % wanted
                )
            return found
        getter = getattr(self._context, "get_using_tts_provider_async", None)
        if callable(getter):
            return await getter(umo=umo)
        return self._context.get_using_tts_provider(umo)

    async def send(
        self,
        platform_id: str,
        conv_kind: str,
        conv_id: str,
        text: str,
        reply_to: str = "",
        image: dict | None = None,
        voice: bool = False,
        umo: str = "",
    ) -> tuple[bool, str, bool, bool]:
        """发一条消息。返回 (成功, 备注, 是否真的用了语音, 结果是否未知)。

        语音是「尽量」：没开 TTS、没 Provider、太长或合成失败，都会退回文字，
        并在备注里说明，不把整条消息吞掉。
        """
        self.last_error_code = ""
        # 单通道：只认 UMO。旧帧的 conv 在升级时已经迁移成 UMO，不再运行时猜。
        target = _s(umo).strip()
        if not target:
            self.last_error_code = "missing_umo"
            return False, "这一帧没有带 umo，桥只按 umo 发送", False, False
        parsed = self._parse_umo(target)
        if parsed is None:
            self.last_error_code = "invalid_umo"
            return False, f"UMO 格式不正确：{target}", False, False
        platform_id, conv_kind, conv_id = parsed
        umo = target
        if not self.has_umo(umo):
            self.last_error_code = "unknown_umo"
            return False, f"未知会话 {umo}，桥没有见过这个会话", False, False
        if not self.allow():
            self.last_error_code = "rate_limited"
            return False, f"已达到每分钟 {self._limit()} 条的上限，本条没有发出", False, False

        used_voice = False
        note = ""
        try:
            from astrbot.api.message_components import Image, Plain, Record, Reply
            from astrbot.api.event import MessageChain

            chain = MessageChain()
            if reply_to:
                try:
                    chain.chain.append(Reply(id=reply_to))
                except Exception:  # 引用失败不影响正文
                    pass

            if voice:
                if not self._tts_enabled():
                    note = "TTS 没有在插件配置里打开，已改发文字"
                elif not text.strip():
                    return False, "语音需要正文", False, False
                elif self._tts_limit() > 0 and len(text) > self._tts_limit():
                    note = f"文字超过 {self._tts_limit()} 字，已改发文字"
                else:
                    try:
                        provider = await self._tts_provider(umo)
                        if provider is None:
                            wanted = _s(self._config().get("tts_provider_id", "")).strip()
                            note = (
                                f"配置的 TTS Provider {wanted} 不可用，已改发文字"
                                if wanted
                                else "AstrBot 没有可用的 TTS Provider，已改发文字"
                            )
                        else:
                            audio_path = await asyncio.wait_for(
                                provider.get_audio(text), timeout=30
                            )
                            if not audio_path:
                                note = "TTS 没有生成音频，已改发文字"
                            else:
                                chain.chain.append(Record.fromFileSystem(audio_path))
                                used_voice = True
                    except Exception as exc:  # noqa: BLE001
                        note = f"TTS 失败（{exc}），已改发文字"

            if text and not used_voice:
                chain.chain.append(Plain(text))
            if image:
                chain.chain.append(Image.fromBase64(_s(image.get("base64"))))
            if not chain.chain:
                return False, "正文和图片都是空的", False, False

            try:
                ok = await asyncio.wait_for(
                    self._context.send_message(umo, chain), timeout=SEND_TIMEOUT
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "Cortico Bridge: 平台发送超过 %.0f 秒没有确认，消息可能已经发出",
                    SEND_TIMEOUT,
                )
                return (
                    False,
                    f"平台发送超过 {SEND_TIMEOUT:.0f} 秒没有确认，消息可能已经发出",
                    False,
                    True,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"Cortico Bridge: 发送失败 —— {exc}")
                return False, str(exc), False, True
            if ok is False:
                return False, "AstrBot 没有找到该会话对应的平台", False, False
            return True, note, used_voice, False
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Cortico Bridge: 发送失败 —— {exc}")
            return False, str(exc), False, False
