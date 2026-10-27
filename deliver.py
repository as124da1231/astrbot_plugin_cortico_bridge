"""投递层：把 Cortico 要说的文字发到 QQ，并记住每个会话的 umo。

两件事分开：
- umo 登记：按「平台实例 + 会话」索引，避免两个 QQ 账号在同一个群时互相覆盖。
- 发送：带每分钟上限，失败一律返回 (False, 原因)，不抛。
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any, Callable

from astrbot.api import logger

CONV_KINDS = ("group", "private")

# 落盘间隔：群消息密集时别每来一条就写一次盘。
UMO_SAVE_INTERVAL = 10.0
# 最多记住多少个会话；和内存表一致。
UMO_MAX = 200


def _s(value: Any) -> str:
    return "" if value is None else str(value)


class Delivery:
    def __init__(self, context, config: Callable[[], dict], data_dir: str = ""):
        self._context = context
        self._config = config
        # key: "平台|类型|会话" -> (umo, 时间戳)
        self._umos: dict[str, tuple[str, float]] = {}
        self._send_stamps: list[float] = []
        # 重启后会话表为空会发不出去；把 umo 落到插件数据目录。
        self._umos_file = os.path.join(data_dir, "umos.json") if data_dir else ""
        self._last_save = 0.0
        self._load_umos()

    # ---------- umo 登记 ----------

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
        loaded = 0
        for key, value in raw.items():
            umo = ""
            ts = 0.0
            if isinstance(value, dict):
                umo = _s(value.get("umo")).strip()
                try:
                    ts = float(value.get("ts") or 0)
                except (TypeError, ValueError):
                    ts = 0.0
            elif isinstance(value, (list, tuple)) and len(value) >= 1:
                umo = _s(value[0]).strip()
                if len(value) > 1:
                    try:
                        ts = float(value[1])
                    except (TypeError, ValueError):
                        ts = 0.0
            if umo and "|" in str(key):
                self._umos[str(key)] = (umo, ts or 0.0)
                loaded += 1
        if len(self._umos) > UMO_MAX:
            keep = sorted(self._umos.items(), key=lambda kv: kv[1][1], reverse=True)[:UMO_MAX]
            self._umos = dict(keep)
        if loaded:
            logger.info(f"Cortico Bridge: 从磁盘恢复 {len(self._umos)} 个会话标识")

    def _save_umos(self, force: bool = False) -> None:
        if not self._umos_file:
            return
        now = time.time()
        if not force and now - self._last_save < UMO_SAVE_INTERVAL:
            return
        self._last_save = now
        try:
            os.makedirs(os.path.dirname(self._umos_file), exist_ok=True)
            payload = {
                key: {"umo": umo, "ts": ts} for key, (umo, ts) in self._umos.items()
            }
            tmp = self._umos_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=0)
            os.replace(tmp, self._umos_file)
        except Exception as exc:  # noqa: BLE001 - 落盘失败不影响发送
            logger.debug(f"Cortico Bridge: 会话表落盘失败 —— {exc}")

    def remember(self, platform_id: str, conv_kind: str, conv_id: str, umo: str) -> None:
        if not umo:
            return
        key = f"{platform_id}|{conv_kind}|{conv_id}"
        changed = self._umos.get(key, ("", 0.0))[0] != umo
        self._umos[key] = (umo, time.time())
        # 防止长期运行后无限增长
        if len(self._umos) > UMO_MAX:
            oldest = sorted(self._umos.items(), key=lambda kv: kv[1][1])[:50]
            for k, _ in oldest:
                self._umos.pop(k, None)
            changed = True
        if changed:
            self._save_umos()

    def umo_for(self, platform_id: str, conv_kind: str, conv_id: str) -> str | None:
        """找这个会话的 umo。

        优先精确匹配平台；没给平台（或平台已换）时退回到「最近见过的同会话」，
        这样 Cortico 不传 platform 也能用。
        """
        if platform_id:
            hit = self._umos.get(f"{platform_id}|{conv_kind}|{conv_id}")
            if hit:
                return hit[0]

        suffix = f"|{conv_kind}|{conv_id}"
        candidates = [
            (umo, ts) for key, (umo, ts) in self._umos.items() if key.endswith(suffix)
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda item: item[1])[0]

    def known_conversations(self) -> list[dict]:
        now = time.time()
        return [
            {
                "key": key,
                "umo": umo,
                "age_sec": round(now - ts),
            }
            for key, (umo, ts) in sorted(
                self._umos.items(), key=lambda kv: kv[1][1], reverse=True
            )
        ]

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
    ) -> tuple[bool, str, bool]:
        """发一条消息。返回 (成功, 备注, 是否真的用了语音)。

        语音是「尽量」：没开 TTS、没 Provider、太长或合成失败，都会退回文字，
        并在备注里说明，不把整条消息吞掉。
        """
        if conv_kind not in CONV_KINDS:
            return False, f"未知的会话类型 {conv_kind}", False
        umo = self.umo_for(platform_id, conv_kind, conv_id)
        if not umo:
            return False, "还没有收到过这个会话的消息，缺少可用的会话标识", False
        if not self.allow():
            return False, f"已达到每分钟 {self._limit()} 条的上限，本条没有发出", False

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
                    return False, "语音需要正文", False
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
                return False, "正文和图片都是空的", False

            ok = await self._context.send_message(umo, chain)
            if ok is False:
                return False, "AstrBot 没有找到该会话对应的平台", False
            return True, note, used_voice
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Cortico Bridge: 发送失败 —— {exc}")
            return False, str(exc), False
