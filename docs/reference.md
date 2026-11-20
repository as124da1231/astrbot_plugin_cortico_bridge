# 参考：配置、错误码、状态与接口

---

## 1. 插件配置

全部在 AstrBot 的插件配置页，`_conf_schema.json` 是唯一来源。

| 键 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `enabled` | bool | `false` | 接管开关。关着时不开放监听，发送返回 `door_closed` |
| `bridge_platforms` | list | `["default"]` | 只接管这些平台（UMO 第一段）。`webchat`/`console`/`default1` 不在列表里就不碰 |
| `group_scope` | string | `all` | `all` / `allowlist` |
| `group_allow` | list | `[]` | 指定群；支持 UMO 或旧群号，保存时归一化 |
| `group_mode` | string | `mention` | `mention` 只接 @/称呼；`all` 每条群消息都接 |
| `name_aliases` | list | `[]` | 除 @ 外算叫到自己的称呼 |
| `listen_host` | string | `127.0.0.1` | 同机用回环；跨机才改 `0.0.0.0` |
| `listen_port` | int | `17999` | 桥监听端口 |
| `token` | string | `""` | 回环可空；非回环必填 |
| `max_sends_per_minute` | int | `30` | 发送限流；`0` 不限 |
| `image_mode` | string | `auto` | `auto` / `image` / `describe` / `off` |
| `image_vision_provider` | string | `""` | 转述图片时用的视觉 Provider |
| `tts_enabled` | bool | `false` | 允许 Coo 发语音 |
| `tts_provider_id` | string | `""` | 指定 TTS Provider；空则用当前 Provider |
| `tts_max_chars` | int | `120` | 超过就改文字 |
| `stt_enabled` | bool | `false` | 把 QQ 语音转成文字 |
| `stt_provider_id` | string | `""` | 指定 STT Provider |
| `tools_allow` | list | `[]` | Coo 可调用的 AstrBot 工具；`*` 表示全部 |
| `private_mode` | string | `all` | `off` / `allowlist` / `all` |
| `private_allow` | list | `[]` | 私聊白名单 |
| `private_admin_bypass` | bool | `true` | 管理员即使不在名单也能私聊 |
| `private_deny` | string | `ignore` | `ignore` 静默；`astrbot` 交给 AstrBot |
| `observe_all` | bool | `false` | 旧版 B 模式，自动迁移，勿改 |
| `observe_groups` | list | `[]` | 旧版群列表，自动迁移，勿改 |

---

## 2. 错误码

### 发送

| code | 含义 |
|---|---|
| `missing_umo` | 帧里没有 `umo`，单通道拒绝 |
| `invalid_umo` | UMO 格式不合法 |
| `unknown_umo` | 桥没见过这个 UMO，或超过 30 天 |
| `umo_conv_mismatch` | 帧里的 `umo` 和 `conv` 不一致 |
| `rate_limited` | 超过每分钟发送上限 |
| `door_closed` | 插件未启用，发送门关着 |

`ack.error` 放 code；`ack.note` 放中文说明。  
超时或连接异常会带 `uncertain`，表示「可能已经发出」。

### 工具

| 情况 | 返回 |
|---|---|
| 没有 `turn` | 「缺少触发消息 id」 |
| `turn` 过期 | 「这一轮的消息已经过期」 |
| 工具不在 `tools_allow` | 「没有在插件的 tools_allow 里」 |
| 工具不存在/停用 | 对应原因 |

---

## 3. 状态字段

`GET /status` 和 SSE `snapshot` 主要字段：

```text
connected          是否已有客户端
listening          是否在监听
listenError        监听失败原因
host / port        监听地址
loopback           host 是否回环
token              token 全文（仅 status，SSE 不带）
tokenSet           是否有 token
client             客户端地址
connectedFor       已连接秒数
lastFrameAgo       最后一帧距今秒数
version            插件版本
enabled            接管开关
observeAll         B 模式
groups / groupScope / groupAllow / groupMode
privateMode / privateAllow / privateAdminBypass / privateDeny
bridgePlatforms
aliases
maxSends
imageMode / peerImages
ttsEnabled / ttsMaxChars / ttsProvider
sttEnabled / sttProvider
toolCount
stats              pushed/sent/failed/skipped/vision/voices/images
toolLog            最近工具回执
conversations      最近会话（UMO）
recent             最近消息流水
originRefused      带 Origin 被拒次数
queryTokenRefused  URL 带 token 被拒次数
```

---

## 4. Web API

前缀：`/astrbot_plugin_cortico_bridge/`

| 路径 | 方法 | 用途 |
|---|---|---|
| `status` | GET | 状态与配置 |
| `groups` | GET/POST | 群聊策略 |
| `private` | GET/POST | 私聊策略 |
| `tools` | GET/POST | 工具白名单 |
| `voice` | GET/POST | 语音配置 |
| `voice/preview` | POST | 合成试听 |
| `voice/test` | POST | 发试听到最近会话 |
| `events` | GET | SSE 实时流 |
| `restart` | POST | 重开监听 |
| `token` | GET/POST | 查看/生成/清除 token |
| `reset` | POST | 清空统计与流水 |

`token` 的 POST body：

```json
{ "action": "generate" }
{ "action": "clear" }
```

---

## 5. 文件与权限

```text
配置
  <AstrBot>/data/config/astrbot_plugin_cortico_bridge_config.json

会话表
  <AstrBot>/data/plugin_data/astrbot_plugin_cortico_bridge/umos.json
  <AstrBot>/data/plugin_data/astrbot_plugin_cortico_bridge/umos.v1.json
```

```text
umos.json      600
umos.v1.json   600，迁移时只备份一次
```

---

## 6. 常见日志

```text
Cortico Bridge: 已在 127.0.0.1:17999 等待 Cortico 连接
Cortico Bridge: 未启用，不开放监听口
Cortico Bridge: Cortico 已连接 <地址>
Cortico Bridge: Cortico 已断开
Cortico Bridge: 已推送 conv=private:100000001 ...
Cortico Bridge: 已发出 conv=private:100000001 40 字
Cortico Bridge: 执行工具 query_lyrics_by_name（来自消息 ...）
Cortico Bridge: 指令交给 AstrBot —— '/help'
Cortico Bridge: 跳过非接管平台 webchat（bridge_platforms=default）
Cortico Bridge: 会话表已迁移到 UMO v2，共 5 条
Cortico Bridge: 已备份旧会话表到 .../umos.v1.json
Cortico Bridge: 已生成新的访问 token
```

---

## 7. 测试

```bash
# 插件
cd astrbot_plugin_cortico_bridge
python -m py_compile bridge.py deliver.py main.py tools.py
python -m unittest discover -s tests

# World
cd cortico-world-astrbot
pnpm typecheck
pnpm test
```

前端语法：

```bash
node --check views/status/app.js
```
