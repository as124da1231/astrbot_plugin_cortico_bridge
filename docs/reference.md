# 参考：配置、错误码、状态与接口

---

## 1. 插件配置

全部在 AstrBot 的插件配置页，`_conf_schema.json` 是唯一来源。

| 键 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `enabled` | bool | `false` | 接管开关。关着时不开放监听，发送和工具调用返回 `door_closed` |
| `bridge_platforms` | list | `["default"]` | 只接管这些平台（UMO 第一段）。`webchat`/`console`/`default1` 不在列表里就不碰 |
| `group_scope` | string | `inherit` | 有旧名单时沿用，默认接全部群。可在群聊页切换 `all` / `allowlist` |
| `group_allow` | list | `[]` | 指定群；支持 UMO 或旧群号，保存时归一化；`allowlist` 下不能为空 |
| `group_mode` | string | `inherit` | 默认仅点名。有旧 `observe_all` 时沿用；也可明确设置 `mention` / `all` |
| `name_aliases` | list | `[]` | 除 @ 外算叫到自己的称呼；`mention` 下命中会唤醒并交给 Coo |
| `listen_host` | string | `127.0.0.1` | 同机用回环；跨机才改 `0.0.0.0`，未配置时也按回环启动 |
| `listen_port` | int | `17999` | 桥监听端口 |
| `token` | string | `""` | 回环可空；非回环必填，否则拒绝绑定 |
| `max_sends_per_minute` | int | `30` | 发送限流；`0` 不限 |
| `image_mode` | string | `auto` | `auto` / `image` / `describe` / `off`，见下 |
| `image_vision_provider` | string | `""` | 转述图片时用的视觉 Provider；留空自动挑 vision / VL |
| `tts_enabled` | bool | `false` | 允许 Coo 发语音 |
| `tts_provider_id` | string | `""` | 指定 TTS Provider；空则用当前 Provider |
| `tts_max_chars` | int | `120` | 超过就改文字；`0` 不限 |
| `stt_enabled` | bool | `false` | 把 QQ 语音转成文字 |
| `stt_provider_id` | string | `""` | 指定 STT Provider |
| `tools_allow` | list | `[]` | 工具许可名单，通配项只包含已审查的读写工具 |
| `tools_risks` | list | `[]` | 管理员逐项配置工具名与风险，未标注按 exec |
| `owner_tool_qq` | string | 空 | 获授权的 AstrBot 管理员 QQ |
| `owner_platform` | string | 空 | 主人 QQ 绑定的平台实例 |
| `desktop_tools_enabled` | bool | false | 完整模式的桌面主人工具授权 |
| `desktop_send_targets` | list | 空 | 桌面主动发送目标 UMO |
| `private_mode` | string | `all` | `off` / `allowlist` / `all` |
| `private_allow` | list | `[]` | 私聊白名单 |
| `private_admin_bypass` | bool | `true` | 管理员即使不在名单也能私聊 |
| `private_deny` | string | `ignore` | 名单外私聊：`ignore` 静默；`astrbot` 交给 AstrBot |
| `observe_all` | bool | `false` | 旧版 B 模式，自动迁移，勿改 |
| `observe_groups` | list | `[]` | 旧版群列表，自动迁移，勿改 |

### `image_mode`

| 值 | 对端能收图 | 对端不能收图 |
|---|---|---|
| `auto` | 传原图 | 有视觉模型就转述；没有则正文写明确原因 |
| `image` | 传原图 | 强制传原图 |
| `describe` | 转述 | 转述 |
| `off` | 只留 `[图片]` | 只留 `[图片]` |

上限：单张原图 6MB（World 侧）、单张 base64 8MB（插件）、单帧合计 10MB、WebSocket 单帧 16MB。视觉调用超时 25 秒，失败只记日志并写 `[图片|转述失败：原因]`，不会抛到消息链路。

### `tools_allow`

```text
[]            一个都不给
["*"]         管理员在 tools_risks 标记的 read / write
["exec_x"]    显式点名，管理员身份和连接工具授权均须有效
```

工具身份：

- QQ 轮次：用触发消息的发言者身份；
- 桌面轮次以 `actor {kind:"desktop"}` 声明来源，AstrBot 校验服务端主人绑定及桥接令牌。
- World 帧里的 `role` / `owner` 不参与判定。

---

## 2. 错误码

`errors.py` 是唯一来源。发送 `ack.error` 放英文 code，`ack.note` 放中文说明；结果不确定时加 `uncertain: true`。

### 发送参数

| code | 含义 |
|---|---|
| `bad_image` | 图片字段格式不对，或 base64 为空 |
| `image_too_large` | 单张图片超过 8MB（base64） |
| `image_bad_type` | mime 不是 `image/*` |
| `empty_message` | 正文和图片都为空 |
| `voice_needs_text` | 语音消息没有正文 |
| `voice_image_conflict` | 语音和图片不能同一条 |
| `voice_at_conflict` | 语音和 `at` 不能同一条 |
| `bad_at` | `at` 不是 QQ 号数组、不是数字 QQ 号 / `"all"`，或超过 10 个 |
| `bad_session` | 会话 kind / id 不合法 |
| `missing_umo` | 帧里没有 `umo` |
| `invalid_umo` | UMO 格式不合法 |
| `unknown_umo` | 桥没见过这个 UMO，或超过 30 天 |
| `umo_conv_mismatch` | 帧里的 `umo` 和 `conv` 不一致 |

### 发送执行

| code | 含义 |
|---|---|
| `busy` | 并发帧超过上限，本帧没有执行 |
| `send_queue_timeout` | 同会话排队超过 5 秒，确定没发 |
| `send_failed` | 发送层异常，结果可能未知 |
| `send_timeout` | 平台发送超时，结果可能未知 |
| `platform_missing` | AstrBot 找不到对应平台 |
| `tts_unavailable` | 语音不可用（试听等不允许回退文字的场景） |

### 门禁

| code | 含义 |
|---|---|
| `rate_limited` | 超过每分钟发送上限 |
| `door_closed` | 插件未启用 |

### 工具帧（桥层直接拒绝）

| code | 含义 |
|---|---|
| `missing_identity` | `turn` 和 `actor` 都没有 |
| `ambiguous_identity` | `turn` 和 `actor` 同时存在 |
| `bad_actor` | `actor` 不是 `{kind:"desktop"}` |
| `owner_auth_required` | 当前连接未获得这个主人账号的工具授权 |
| `tool_failed` | 工具回调抛错；异常原文只进日志 |

工具回调里的业务失败（白名单、过期、权限）沿用下面的中文说明，放在 `tool-ack.error`；能定位到桥层时用上表的英文 code。

### 工具失败原因（按当前实现）

| 情况 | 回给 Coo 的说明 |
|---|---|
| 插件未启用 | `桥未启用，工具调用被拒绝` |
| 工具不在 `tools_allow` | `工具 X 没有在插件的 tools_allow 里` |
| exec 只被 `*` 覆盖 | `工具 X 是执行类，需要显式加入 tools_allow` |
| exec 但调用者不是管理员 | `工具 X 是执行类，当前身份没有执行权限` |
| 工具不存在 / 停用 | `没有这个工具：X` / `工具 X 在 AstrBot 里是停用的` |
| `turn` 不在表里或超过 600 秒 | `这一轮的消息已经过期，请重新说一次` |
| Coo 轮次没填主人 QQ | `这一轮没有 QQ 消息身份；要在这里调工具，先在 Coo 的连接设置里填主人 QQ` |
| 填的主人 QQ 不在管理员名单 | `X 不在 AstrBot 的管理员名单里` |
| 工具执行超时 | `X 超过 N 秒没有返回` |
| 工具抛异常 | `工具 X 执行失败，详情见插件日志` |
| 权限被拒 | 原样返回工具或 AstrBot 的拒绝说明，不改成成功 |

---

## 3. 状态字段

`GET /status` 和 SSE `snapshot` 主要字段：

```text
connected          是否已有客户端
listening          是否在监听
listenError        监听失败原因
host / port        监听地址
loopback           host 是否回环
tokenSet           是否已设置 token（不返回 token 明文）
client             客户端地址
connectedFor       已连接秒数
lastFrameAgo       最后一帧距今秒数（-1 表示还没有）
uptime             插件运行秒数
version            插件版本
enabled            接管开关
observeAll         group_mode 是否为 all
groups / groupScope / groupAllow / groupMode
privateMode / privateAllow / privateAdminBypass / privateDeny
bridgePlatforms
aliases
maxSends
admins             AstrBot 管理员名单
imageMode / peerImages
ttsEnabled / ttsMaxChars / ttsProvider
sttEnabled / sttProvider
toolCount
stats              pushed/sent/failed/skipped/vision/voices/images
toolLog            最近工具回执
sseClients         当前 SSE 订阅数
conversations      最近会话（UMO）
recent             最近消息流水
lastChannel / lastMessage / replyWith
originRefused      带 Origin 被拒次数
queryTokenRefused  URL 带 token 被拒次数
badTokenRefused    token 不匹配被拒次数
versionRefused     协议版本不匹配被拒次数
```

`token` 不再出现在 `status` / `snapshot` 里。复制、生成、清除都走 `/token`。

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
| `token` | GET/POST | 读取 / 生成 / 清除 token |
| `reset` | POST | 清空统计与流水 |

`token` 的 POST body：

```json
{ "action": "generate" }
{ "action": "clear" }
```

- `GET` 返回 token 全文，仅供已登录的 AstrBot 管理后台复制；`status` 只给 `tokenSet`。
- `generate` 生成 43 字符 `token_urlsafe(32)`，写回配置并重开监听；控制台会一次性显示新 token。
- `clear` 清空 token；非回环监听会因缺 token 停止监听。

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

## 6. 握手与关闭码

```text
第一帧必须是 hello，10 秒超时
令牌只读 Authorization: Bearer 请求头
hello.v 必须是 3；缺省或其他值 close 4004
同一连接重复 hello 会重新校验 token 与版本，只刷新能力并重推 ready
```

| code | 含义 |
|---|---|
| 4001 | 握手超时 |
| 4002 | 第一帧不是 hello |
| 4003 | token 不匹配 |
| 4004 | 协议版本不支持 |
| 4401 | URL 里带了 token |
| 4403 | 带了 Origin |

World 收到这些码会记一条「桥拒绝了这次连接」，并按退避继续重连。

---

## 7. 协议帧

| 帧 | 方向 | 字段 |
|---|---|---|
| `hello` | World → 插件 | `v: 3`、`capabilities.images`、`capabilities.message_segments`、`capabilities.component_media` |
| `ready` | 插件 → World | `v: 3`、`mode`、`modeRevision`、`channel`、`self {id,name}`、`admins[]`、`tts {ready,provider}`、`conversations[] {key,umo,platform,kind,id,name,age_sec}`（最多 20 条，按最近出现排序） |
| `msg` | 插件 → World | `channel`、`id`、`umo`、`ts`、`conv {kind,id,name}`、`sender {id,name}`、`text`、`mentioned`、`platform`、`role`、`owner`、`can`、`images[]`、`image_errors[]`、`segments[]` |
| `msg`（通知） | 插件 → World | 同上，`kind: "notice"`、`ref`，`can.screenshot=false` |
| `msg-ack` | World → 插件 | 回复 `msg.deliveryId` 和 `accepted`。明确拒收可交还 AstrBot，收不到回执视为可能已接管 |
| `tick` | 插件 → World | `ts`（epoch 秒） |
| `send` | World → 插件 | `rid`、`umo`（必填）、`conv`、`text`、`replyTo`、`platform`、`image {mime,base64}`、`voice`、`at[]` |
| `ack` | 插件 → World | `rid`、`ok`、`voice`、`error`、`note`、`uncertain` |
| `tools` | 插件 → World | `items[] {name,risk,desc,sig}` |
| `tool` | World → 插件 | `rid`、`name`、`args`、`turn` 或 `actor {kind:"desktop"}` |
| `tool-ack` | 插件 → World | `rid`、`ok`、`result`（成功）或 `error` / `note`（失败）。读取组件时可附 `images[]` |

`send` 只认 `umo`；`conv` 是投影，可省略。通知不单独发 `notice` 帧，统一走 `msg` + `kind: "notice"`。

`message_segments` 启用有序组件树。组件 `{type, fields, children?}` 的 `fields.attachment` 是同一帧图片数组的零基索引，查看组件的 `path` 则是从 1 开始的组件编号。仅声明 `component_media` 的客户端会收到工具回执图片，原有 `result` 文字字段仍保留。

`ready.conversations` 是桥记得的最近会话（含重启前恢复的），World 用它支持主动发送。最多 20 条，按最近出现排序；要发到没列出的会话，先在目标会话说一句，让桥记下它并进入快照，再发。

`send.at` 只对群生效：QQ 号数组，最多 10 个，`["all"]` 表示 @全体（World 侧要先打开 `worlds.astrbot.allowAtAll`，面板暂无开关）。私聊里 `at` 被忽略并在回执里说明；`voice` 和 `at` 不能同一条，回 `voice_at_conflict`；格式不对或超量回 `bad_at`。

QQ 轮次可以隐式回触发会话。桌面主动发送需要完整模式、桥接令牌和单独授予的目标会话，由机器人发送。

---

## 8. 常见日志

```text
Cortico Bridge: 已在 127.0.0.1:17999 等待 Cortico 连接
Cortico Bridge: 未启用，不开放监听口
Cortico Bridge: Cortico 已连接 <地址>
Cortico Bridge: Cortico 已断开
Cortico Bridge: 握手完成（对端能收图=False）
Cortico Bridge: 已推送 conv=private:100000001 ...
Cortico Bridge: 已发出 conv=private:100000001 40 字
Cortico Bridge: 执行工具 query_lyrics_by_name（来自 ...）
Cortico Bridge: 指令交给 AstrBot —— '/help'
Cortico Bridge: 跳过非接管平台 webchat（bridge_platforms=default）
Cortico Bridge: 会话表已迁移到 UMO v2，共 5 条
Cortico Bridge: 已备份旧会话表到 .../umos.v1.json
Cortico Bridge: 已生成新的访问 token
Cortico Bridge: 识图失败 provider=... —— ...
```

---

## 9. 测试

```bash
# 插件
python -m py_compile main.py bridge.py deliver.py tools.py owner_event.py errors.py
python -m unittest discover -s tests

# World
pnpm typecheck
pnpm test
```

前端语法：

```bash
node --check views/status/app.js
```

具体用例数量以实际输出为准，不在文档里写死。
