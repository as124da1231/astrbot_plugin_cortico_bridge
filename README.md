# Cortico 桥

<img src="./logo.png" width="88" alt="Cortico 桥" align="right">

**当前版本：1.3.6**

问题解答 QQ：**1136066256**

把 AstrBot 收到的 QQ 消息转给 [Cortico](https://github.com/Pal-AI-Lab/Cortico) 的人格，再把它的回复发回群里。  
*Bridge QQ messages from AstrBot to [Cortico](https://github.com/Pal-AI-Lab/Cortico) persona and send replies back to QQ.*

**分工**：AstrBot 保留 QQ 连接、账号、白名单和所有已有插件；Cortico 只负责「说什么」。  
***Division of work***: *AstrBot maintains QQ connections, accounts, whitelists, and all existing plugins; Cortico is only responsible for what to say.*

Cortico 侧还有一个配套扩展 [`cortico-world-astrbot`](https://github.com/as124da1231/cortico-world-astrbot)，两个包按协议 v1 对接，**必须都装**。

项目地址：[as124da1231/astrbot_plugin_cortico_bridge](https://github.com/as124da1231/astrbot_plugin_cortico_bridge)。

已在 **AstrBot 4.29.1 + aiocqhttp** 上实机验证：文字、图片（单图 / 多图 / 引用图）、撤回与戳一戳通知、主人身份、断线重连。

## 群聊与私聊

配置里的会话键优先用 AstrBot 的 UMO：`平台:消息类型:会话ID`。

```text
群聊：default:GroupMessage:123456789
私聊：default:FriendMessage:100000001
```

- 群聊范围：`group_scope` 为 `all` / `allowlist`；`group_allow` 接受 UMO 或旧群号，指定群模式下列表为空不能保存。
- 群消息范围：`group_mode` 为 `mention` / `all`；`mention` 只投递 @ 或命中称呼的消息。
- 私聊：`private_mode` 为 `off` / `allowlist` / `all`；`private_allow` 接受 UMO 或旧 QQ 号。
- 私聊仍受 QQ 好友关系限制：bot 和对方不是好友时，QQ 会拒绝私聊回复。

## 已知边界

- 语音转写、引用回复（`reply_to`）、状态工具目前只做过单测，没有实机用例。
- 图片单张上限 12MB（base64），超过会被跳过并记一条 WARN。
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
4. Coo 在另一台机器上时，把 `listen_host` 改成 `0.0.0.0` 并设置 `token`，两端保持一致；Cortico 侧填 `ws://<服务器地址>:17999`。

依赖 `websockets >= 13`（AstrBot 官方镜像已自带）。

## 配置

全部走 AstrBot 原生配置页（`_conf_schema.json`），没有第二套界面。

| 键 | 默认 | 说明 |
|---|---|---|
| `enabled` | 关 | 接管开关。关着时插件完全不介入 |
| `bridge_platforms` | `default` | 只接管这些 AstrBot 平台；`webchat`/`console`/`default1` 等平台不会被拦截或推送 |
| `group_scope` | `all` | 群范围：`all` 全部群 / `allowlist` 指定群 |
| `group_allow` | 空 | 指定群名单，一行一个 UMO 或旧群号；只在 `group_scope=allowlist` 时生效，空名单不能保存 |
| `group_mode` | `mention` | 群消息范围：`mention` 只交 @ 或叫到称呼的；`all` 每条群消息都交 |
| `name_aliases` | 空 | 除 @ 之外算「叫到我」的称呼，例如 `Coo`、`库` |
| `listen_port` | `17999` | 桥监听的端口，需要在防火墙/安全组放行 |
| `listen_host` | `127.0.0.1` | 默认只允许本机连。Coo 在另一台机器上才填 `0.0.0.0`，并且必须同时设置 token |
| `token` | 空 | 握手校验。监听 `127.0.0.1` 时可以留空；监听任何非回环地址时必填，否则桥拒绝启动 |
| `max_sends_per_minute` | `30` | 防止失控刷屏，`0` 表示不限 |
| `image_mode` | `auto` | 群图片怎么给 Cortico：`auto` 能读图就传原图、否则转述；`image` 始终原图；`describe` 始终转述；`off` 只留占位 |
| `image_vision_provider` | 空 | 「转述」这条路用哪个视觉模型；留空自动挑一个带 vision / VL 的 |

### 群消息范围

- `mention`（默认）：只把被 @ 或叫到 `name_aliases` 的消息交给 Cortico。群里闲聊不进上下文。
- `all`：每条群消息都交过去，由 Cortico 自己决定要不要开口。更适合「住在这个群里」的用法，token 消耗也明显更高。

群范围由 `group_scope` 控制：`all` 是所有群，`allowlist` 只处理 `group_allow` 里的群。私聊不受群范围和群消息范围影响，由 `private_mode` 单独控制。

## 主人是谁

插件不维护第二份权限名单：AstrBot 的 `admins_id` 就是「主人」。

- 握手时把 `admins` 告诉 Cortico，界面上给这些人加 `(主人)` 标记；
- 只有主人说话的那一轮才允许 Cortico 截屏（配合 Coopanion 的 CUA）；
- 非管理员说话会立刻收回授权。

## 能力

| 方向 | 能力 |
|---|---|
| 收 | 文字、图片（原图或转述）、语音（走 AstrBot 已配置的 STT） |
| 发 | 文字、图片、语音（TTS，失败自动退回文字）、引用回复、每分钟上限 |
| 群通知 | 撤回、戳一戳（戳到自己显示「你」）、入群、退群、管理员变更、上传文件 |
| 历史 | Cortico 侧用事件库检索，跨重启保留 |

群通知不会单独唤醒 bot，只作为上下文随下一批消息送达；`[撤回]` 这类行也不需要回应。

## 控制台

插件详情页里是一个跟着 Cortico/Coopanion 视觉走的控制台：左侧分「概览 / 工具 / 语音」，纸面底色、细线、等宽字体，浅色深色都跟随 AstrBot。

- **概览**：连接状态、推送/发出/失败计数、会话流水（可按收到/发出/叫到筛）、最近 12 次工具调用、生效中的设置。配置仍然只读，改值请去 AstrBot 的配置页。
- **工具**：按来源插件分组，搜索工具名/参数/说明，按风险筛选，只看已选，组内全选。改完有固定保存条，可撤销；`Ctrl+S` 保存，`/` 聚焦搜索。停用的工具会标出来，最近用过的会带一个绿点。
- **语音**：STT/TTS Provider 状态、允许 Coo 用语音说话、试听、发到最近的 QQ 会话。语音按配置的 provider id 取，不依赖 AstrBot 的全局 TTS/STT 开关；没开、没 Provider、太长或失败都会自动退回文字。
- 两个维护按钮「重开监听 / 清空统计」留在页头；清空统计会一起清掉面板上的流水。

## 安全

- **默认只监听 `127.0.0.1`**。同机 Coo 不需要任何网络暴露。
- **本机免密**：只监听回环且 token 为空时，桥允许启动、不校验 token；浏览器仍会被 `Origin` 拒绝。状态页的 token 徽标在未设置时点击可生成，已设置时点击可复制。
- 监听非回环地址（`0.0.0.0`、网卡 IP、`::`）时必须设置 `token`，否则桥拒绝绑定，面板会显示「非回环监听必须设置 token」。
- token 是共享密钥，不是身份系统。它挡住不知道密钥的人，挡不住知道密钥的人。
- 浏览器发起的 WebSocket 一定带 `Origin`，桥没有网页客户端，所以**带 `Origin` 的连接一律拒绝**（close 4403）。
- 凭据不能进 URL：`?token=` / `?access_token=` 直接拒绝（close 4401）。
- 未握手前不会收到任何 QQ 消息，也不能发 `send` / `tool` 帧。
- **`enabled=关` 时不开放端口**；发送和工具调用也会被拒绝（`door_closed`）。
- 桥是明文 WebSocket。跨公网时建议再套 TLS 反代或走 VPN/专网。
- 截图走 Coopanion 的 CUA，只有主人在说话时放行。

## 开发

```bash
python -m py_compile main.py bridge.py deliver.py
```

服务器侧热更新：把整个目录覆盖到 `data/plugins/astrbot_plugin_cortico_bridge/` 后重载插件。

协议帧定义见 [cortico-world-astrbot 的 README](https://github.com/as124da1231/cortico-world-astrbot#桥的协议)。

## 许可

[MIT](LICENSE) © 2026 as124da1231
