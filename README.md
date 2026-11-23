# Cortico 桥

<img src="./logo.png" width="88" alt="Cortico 桥" align="right">

**当前版本 1.3.9**

把 AstrBot 收到的 QQ 消息转给 [Cortico](https://github.com/Pal-AI-Lab/Cortico) 的人格，再把它的回复发回群里。  
*Bridge QQ messages from AstrBot to [Cortico](https://github.com/Pal-AI-Lab/Cortico) persona and send replies back to QQ.*

**分工**：AstrBot 保留 QQ 连接、账号、白名单和所有已有插件；Cortico 只负责「说什么」。  
***Division of work***: *AstrBot maintains QQ connections, accounts, whitelists, and all existing plugins; Cortico is only responsible for what to say.*

Cortico 侧的配套扩展是 [`cortico-world-astrbot`](https://github.com/as124da1231/cortico-world-astrbot)，两端使用协议 v3。在 Coo「连接」页发起切换，开放完整模式需要 AstrBot 管理员查看范围并确认。

项目地址：[as124da1231/astrbot_plugin_cortico_bridge](https://github.com/as124da1231/astrbot_plugin_cortico_bridge)。

QQ 消息组件按原始顺序交给配套 Coo 扩展。引用可带适配器取到的原文，合并转发可逐层查看。Coo 可按需读取授权会话里仍能下载的语音原文件，文字转写由 AstrBot 的语音设置控制。

### 从旧版升级

备份插件配置及 `plugin_data/astrbot_plugin_cortico_bridge` 后，将两端一起更新到 1.3.9。旧协议对端会被拒绝，升级窗口内 QQ 由 AstrBot 原流程处理。旧群名单和已保存的桥地址继续生效。完整模式下可在插件「群聊」页检查实际范围。

已有主人 QQ 和记下的会话可沿用。旧版若使用独立主人工具令牌，在「概览」核对现有绑定，再确认一枚桥接令牌是否可以使用桌面主人工具。桌面主动发送可添加要授权的具体会话。已有工具名单可以沿用。

两种模式均使用原版 Coo 的主会话。安全模式允许指定主人在私聊中继续桌面对话。完整模式开放已授权的群和私聊，QQ 回复可能提到 Coo 桌面对话内容。

## 群聊与私聊

配置里的会话键优先用 AstrBot 的 UMO：`平台:消息类型:会话ID`。

```text
群聊：default:GroupMessage:123456789
私聊：default:FriendMessage:100000001
```

- 群范围：`group_scope` 为 `all` / `allowlist`；`group_allow` 接受 UMO 或旧群号，指定群模式下列表为空不能保存。
- 群消息范围：`group_mode` 为 `mention` / `all`，默认 `mention`。
- 私聊：`private_mode` 为 `off` / `allowlist` / `all`；`private_allow` 接受 UMO 或旧 QQ 号。
- 私聊仍受 QQ 好友关系限制：bot 和对方不是好友时，QQ 会拒绝私聊回复。

### `mention` 会接哪些群消息

默认 `group_mode=mention`，只有下面几种会被唤醒并**直接交给 Coo**：

- @ 了 bot；
- 回复了 bot 的消息；
- 正文里出现 `name_aliases` 里的称呼（例如 `Coo`、`库`）。

不会接的情况：

- `At(qq="all")`（@全体）不算点名，避免整群被唤醒；
- `group_scope=allowlist` 时，不在 `group_allow` 里的群不进；
- 群里没有点名 bot 的普通闲聊不进上下文；
- 以 `wake_prefix`（默认 `/`）开头的消息、以及命中已注册指令名的消息，交给 AstrBot；
- 桥没连上时一律不唤醒，消息照常留给 AstrBot。

`all`：群里每条消息都交过去，由 Cortico 自己决定要不要开口；token 消耗也明显更高。

## 图片

图片上限分三段，超过的图会被跳过，并在正文里留下明确原因：

```text
单张原图（World 侧）        6MB
单张 base64（插件接收）     8MB
单帧图片 base64 合计        10MB
WebSocket 单帧上限          16MB
```

`image_mode` 四种取值：

| 值 | 行为 |
|---|---|
| `auto`（默认） | 对端握手声明能收图就传原图；不能收图时尝试用视觉模型转述。没有可用视觉模型时，正文写 `[图片|对端模型不能读图，且没有可用的视觉模型转述]`，不静默丢图 |
| `image` | 强制传原图；适合明确知道对端能收图时使用。需要按对端能力自动转述时选择 auto |
| `describe` | 始终转述成文字；没有视觉模型或调用失败时写 `[图片|转述失败：原因]` |
| `off` | 只保留 `[图片]` 占位，不传原图、不调用视觉模型 |

视觉模型按 `image_vision_provider` 指定，留空自动挑一个带 `vision` / `VL` 的。单次识图最多等 25 秒；超时、异常或空描述只记日志并回一条失败原因，不会让整轮消息崩掉。

## 工具与身份

`tools_allow` 决定 Coo 能调哪些 AstrBot 工具：

- 空列表：一个都不给；
- `*`：只自动包含管理员在 `tools_risks` 里明确标为 read / write 的工具；
- 风险未标注的工具按 exec 管理。exec 必须显式点名，要求管理员身份和已获工具授权的连接；
- 工具清单和风险标签在插件启动日志里（搜「可调工具」），控制台「工具」栏也能看。

工具身份有两条路径，**都不信任 World 帧里传来的 `role` / `owner`**：

- **QQ 发言者**：这一轮由 QQ 消息触发时，用触发消息的事件执行，身份就是发言者；管理员工具由 AstrBot 权限层拦。
- **Coo 桌面**：在 AstrBot 插件「概览」绑定管理员 QQ，单独开启桌面主人工具。桌面主动发 QQ 使用机器人身份，另设目标。两项都使用同一枚桥接令牌。

权限拒绝、工具停用、不在白名单、这一轮已过期，都会**如实失败**并说明原因，不会回「成功」。工具异常原文只进日志，回给模型的是固定短句。

## 已知边界

- 语音转写、引用回复（`reply_to`）、状态工具目前只做过单测，没有实机用例。
- 首次连接桥最多等 12 秒；超时先继续启动，桥在后台重连，不会拖住后面的扩展。
- 发送确认超时会标成「结果未知」，不会自动重试；要重发前先确认对方有没有收到。
- 发图要求授权来自这一轮最后一条管理员消息；桌宠轮次和只收到通知的轮次不能复用旧授权。

## 它解决什么

Cortico 自带 QQ World 能直连 OneBot，但只做收发消息。如果你的 QQ 号上已经跑着一整套 AstrBot 插件（画图、点歌、战绩、媒体解析……），把这一套换掉代价很高。

这个插件让 AstrBot 继续当「手和脚」，只把「嘴」交给 Cortico：

```
QQ → OneBot → AstrBot（现有插件照常工作）
                     │ 本插件截住这一轮，推给 Cortico
                     ▼
              Cortico 想完 → 调工具 → 本插件发回 QQ
```

AstrBot 自己的模型不参与这些轮次；桥没连上时插件什么都不做，消息照常交给 AstrBot。

## 文档

- [从零接入教程](docs/tutorial.md)
- [架构与边界](docs/architecture.md)
- [配置、错误码与接口](docs/reference.md)
- [接入安全模型](docs/security.md)

## 安装

1. 在 AstrBot 插件市场搜索「Cortico 桥」，或把本仓库放进 `data/plugins/`。
2. 重启 AstrBot，打开插件配置。
3. 打开「接管对话」。同机 Coo 用默认的 `127.0.0.1`，不需要填 token。
4. 跨机连接时设置一枚桥接令牌。直连在 Coo 填 `ws://服务器IP:17999` 和同一个令牌。使用 TLS 代理时填 `wss://` 地址，代理与桥之间可走回环或私网。WS 本身是明文，两种连接均校验令牌。

依赖 `websockets >= 13`（AstrBot 官方镜像已自带）。

## 配置

全部走 AstrBot 原生配置页（`_conf_schema.json`），没有第二套界面。

| 键 | 默认 | 说明 |
|---|---|---|
| `enabled` | 关 | 接管开关。关着时插件完全不介入 |
| `bridge_platforms` | `default` | 只接管这些 AstrBot 平台；`webchat`/`console`/`default1` 等平台不会被拦截或推送 |
| `group_scope` | `all` | 群范围：`all` 全部群 / `allowlist` 指定群 |
| `group_allow` | 空 | 指定群名单，一行一个 UMO 或旧群号；只在 `group_scope=allowlist` 时生效，空名单不能保存 |
| `group_mode` | `mention` | 群消息范围：`mention` 只交 @ / 回复 / 命中称呼的；`all` 每条群消息都交 |
| `name_aliases` | 空 | 除 @ 之外算「叫到我」的称呼，例如 `Coo`、`库`；`mention` 下命中会唤醒并交给 Coo |
| `listen_port` | `17999` | 桥监听的端口。WSS 代理场景下，公网只需访问代理入口 |
| `listen_host` | `127.0.0.1` | 默认只允许本机连。直连时填写 Coo 可访问的地址，使用代理时填写代理可访问的地址 |
| `token` | 空 | 握手校验。监听 `127.0.0.1` 时可以留空；监听任何非回环地址时必填，否则桥拒绝启动 |
| `max_sends_per_minute` | `30` | 防止失控刷屏，`0` 表示不限 |
| `image_mode` | `auto` | 见上文「图片」 |
| `image_vision_provider` | 空 | 「转述」这条路用哪个视觉模型；留空自动挑一个带 vision / VL 的 |
| `tools_allow` | 空 | 一行一个工具名；通配授权只覆盖已审查的读写工具 |
| `tools_risks` | 空 | 一行一项 `工具名:read` 或 `工具名:write`；仅配置已确认行为的工具 |
| `owner_tool_qq` / `owner_platform` | 空 | 绑定安全模式的主人 QQ 与平台实例 |
| `desktop_tools_enabled` / `desktop_send_targets` | 关闭 / 空 | 分别授权完整模式的桌面主人工具及主动发送目标 |
| 语音设置 | 见参考 | [语音配置键](docs/reference.md#1-插件配置) |
| `private_mode` | `all` | `off` / `allowlist` / `all` |
| `private_allow` | 空 | 私聊白名单 |
| `private_admin_bypass` | 开 | 管理员即使不在私聊名单也能私聊 |
| `private_deny` | `ignore` | 名单外私聊：`ignore` 静默 / `astrbot` 交回 AstrBot |

群范围由 `group_scope` 控制：`all` 是所有群，`allowlist` 只处理 `group_allow` 里的群。私聊不受群范围和群消息范围影响，由 `private_mode` 单独控制。

## 主人是谁

插件不维护第二份权限名单：AstrBot 的 `admins_id` 就是「主人」。

- 握手时把 `admins` 告诉 Cortico，界面上给这些人加 `(主人)` 标记；
- 只有主人说话的那一轮才允许 Cortico 截屏（配合 Coopanion 的 CUA）；
- 非管理员说话会立刻收回授权；
- Coo 对话轮次需完成上方的主人工具授权。群聊与私聊的普通转发沿用原连接设置。

## 能力

| 方向 | 能力 |
|---|---|
| 收 | 文字、图片（原图或转述）、语音（走 AstrBot 已配置的 STT） |
| 发 | 文字、图片、语音（TTS，失败自动退回文字）、引用回复、每分钟上限 |
| 群通知 | 撤回、戳一戳（戳到自己显示「你」）、入群、退群、管理员变更、上传文件 |
| 历史 | Cortico 侧用事件库检索，跨重启保留 |

群通知不会单独唤醒 bot，只作为上下文随下一批消息送达；`[撤回]` 这类行也不需要回应。通知实际走 `msg` 帧的 `kind: "notice"`，桥断开时不会被插件吞掉。

## 控制台

插件详情页里是一个跟着 Cortico/Coopanion 视觉走的控制台，左侧分「概览 / 群聊 / 私聊 / 工具 / 语音」，纸面底色、细线、等宽字体，浅色深色都跟随 AstrBot。

- **概览**：连接状态、推送/发出/失败计数、会话流水（可按收到/发出/叫到筛）、最近 12 次工具调用、生效中的设置。配置仍然只读，改值请去 AstrBot 的配置页。
- **群聊**：`group_scope` / `group_allow` / `group_mode`，可直接保存。
- **私聊**：`private_mode` / `private_allow` / 管理员绕过。
- **工具**：按来源插件分组，搜索工具名/参数/说明，按风险筛选，只看已选，组内全选。改完有固定保存条，可撤销；`Ctrl+S` 保存，`/` 聚焦搜索。停用的工具会标出来，最近用过的会带一个绿点。
- **语音**：STT/TTS Provider 状态、允许 Coo 用语音说话、试听、发到最近的 QQ 会话。语音按配置的 provider id 取，不依赖 AstrBot 的全局 TTS/STT 开关；没开、没 Provider、太长或失败都会自动退回文字。
- 两个维护按钮「重开监听 / 清空统计」留在页头；清空统计会一起清掉面板上的流水。

## 状态接口与 token

`GET /status` 和 SSE `snapshot` **不再返回 token 明文**，只回 `tokenSet`（是否已设置）。

token 的复制 / 生成 / 清除都走 `POST /token`：

```json
{ "action": "generate" }
{ "action": "clear" }
```

- `generate`：`secrets.token_urlsafe(32)` 写回配置并重开监听；
- `clear`：清空 token，非回环监听会因缺 token 停止监听；
- 复制：控制台「概览」设置表有「复制 / 生成（轮换） / 清除」三个按钮，复制走 `GET /token`（需要 AstrBot 管理后台鉴权）；生成后会一次性显示新 token，方便复制。

## 安全

- **默认只监听 `127.0.0.1`**。同机 Coo 不需要任何网络暴露。
- **本机免密**：只监听回环且 token 为空时，桥允许启动、不校验 token；浏览器仍会被 `Origin` 拒绝。
- 监听非回环地址（`0.0.0.0`、网卡 IP、`::`）时必须设置 `token`，否则桥拒绝绑定，面板会显示「非回环监听必须设置 token」。
- token 是共享密钥，不是身份系统。它挡住不知道密钥的人，挡不住知道密钥的人。
- 浏览器发起的 WebSocket 一定带 `Origin`，桥没有网页客户端，所以**带 `Origin` 的连接一律拒绝**（close 4403）。
- 凭据不能进 URL：`?token=` / `?access_token=` 直接拒绝（close 4401）。
- 未握手前不会收到任何 QQ 消息，也不能发 `send` / `tool` 帧。
- **`enabled=关` 时不开放端口**；发送和工具调用也会被拒绝（`door_closed`）。
- 桥是明文 WebSocket。跨公网时建议再套 TLS 反代或走 VPN/专网。
- 截图走 Coopanion 的 CUA，只有主人在说话时放行。

## 协议帧

协议 v3，`hello.v` 必须是 3。令牌只放在 Upgrade 的 `Authorization: Bearer` 请求头。模式同步后，读写请求还需匹配连接的模式代次和 QQ 会话授权。完整字段和错误码见 [参考](docs/reference.md)。

| 帧 | 方向 | 关键字段 |
|---|---|---|
| `hello` | World → 插件 | `v`、`token`（兼容）、`capabilities.images` |
| `ready` | 插件 → World | `v`、`channel`、`self`、`admins`、`tts`、`conversations[]`（最多 20 条最近会话） |
| `msg` | 插件 → World | `umo`、`conv`、`sender`、`text`、`mentioned`、`images` / `image_errors`、通知用 `kind: "notice"` |
| `tick` | 插件 → World | `ts`（epoch 秒） |
| `send` | World → 插件 | `rid`、`umo`（必填）、`conv`（UMO 的投影，可省略）、`text`、`replyTo`、`image`、`voice`、`at`（群 @，最多 10 个，支持 `all`） |
| `ack` | 插件 → World | `rid`、`ok`、`voice`、失败时 `error`（英文 code）+ `note`（中文）、结果未知时 `uncertain` |
| `tools` | 插件 → World | `items[]`：`name`、`risk`、`desc`、`sig` |
| `tool` | World → 插件 | `rid`、`name`、`args`、`modeRevision`、`umo`、`turn` 或 `actor: {kind:"desktop"}` |
| `tool-ack` | 插件 → World | `rid`、`ok`、成功 `result`、失败 `error` + `note` |

发送只认 `umo`：没有 `umo` 回 `missing_umo`，格式不对回 `invalid_umo`，桥没见过回 `unknown_umo`，与 `conv` 冲突回 `umo_conv_mismatch`。`ack` 的 `error` 只放 `errors.py` 里的英文 code，人看的中文放 `note`。`at` 只对群生效，最多 10 个，支持 `["all"]`（@全体，World 侧需先打开 `worlds.astrbot.allowAtAll`）；语音不能和 `at` 同条（`voice_at_conflict`），格式不对或超量回 `bad_at`。

通知没有单独的 `notice` 帧，统一是 `msg` + `kind: "notice"`；`ref` 是可选的原消息 id。

## 开发

```bash
python -m py_compile main.py bridge.py deliver.py tools.py owner_event.py errors.py
python -m unittest discover -s tests
node --check views/status/app.js
```

服务器侧热更新：把整个目录覆盖到 `data/plugins/astrbot_plugin_cortico_bridge/` 后重载插件。

协议帧定义见 [参考](docs/reference.md) 与 [cortico-world-astrbot 的 README](https://github.com/as124da1231/cortico-world-astrbot#桥的协议)。

## 问题解答

交流 QQ 群 **1136066256**

## 许可

[MIT](LICENSE) © 2026 as124da1231
