# Cortico 桥

<img src="./logo.png" width="88" alt="Cortico 桥" align="right">

**当前版本：1.3.1**

把 AstrBot 收到的 QQ 消息转给 [Cortico](https://github.com/Pal-AI-Lab/Cortico) 的人格，再把它的回复发回群里。

**分工**：AstrBot 保留 QQ 连接、账号、白名单和所有已有插件；Cortico 只负责「说什么」。

Cortico 侧还有一个配套扩展 [`cortico-world-astrbot`](https://github.com/as124da1231/cortico-world-astrbot)，两个包按协议 v1 对接，**必须都装**。

项目地址：[as124da1231/astrbot_plugin_cortico_bridge](https://github.com/as124da1231/astrbot_plugin_cortico_bridge)。

已在 **AstrBot 4.29.1 + aiocqhttp** 上实机验证：文字、图片（单图 / 多图 / 引用图）、撤回与戳一戳通知、主人身份、断线重连。

## 已知边界

- 语音转写、引用回复（`reply_to`）、状态工具目前只做过单测，没有实机用例。
- 图片单张上限 12MB（base64），超过会被跳过并记一条 WARN。

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

## 安装

1. 在 AstrBot 插件市场搜索「Cortico 桥」，或把本仓库放进 `data/plugins/`。
2. 重启 AstrBot，打开插件配置。
3. 填 `token`（随便一串，两端要一致），按需打开「接管对话」。
4. 在 Cortico 侧填同一个 `token` 和本机的 `ws://<服务器地址>:17999`。

依赖 `websockets >= 13`（AstrBot 官方镜像已自带）。

## 配置

全部走 AstrBot 原生配置页（`_conf_schema.json`），没有第二套界面。

| 键 | 默认 | 说明 |
|---|---|---|
| `enabled` | 关 | 接管开关。关着时插件完全不介入 |
| `observe_all` | 关 | B 模式：群里每条消息都交给 Cortico；关着是 A 模式，只交 @ 或叫到称呼的 |
| `observe_groups` | 空 | 接管的群号，一行一个。留空表示所有群。**A/B 模式都生效**，名单外的群仍归 AstrBot |
| `name_aliases` | 空 | 除 @ 之外算「叫到我」的称呼，例如 `Coo`、`库` |
| `listen_port` | `17999` | 桥监听的端口，需要在防火墙/安全组放行 |
| `listen_host` | `0.0.0.0` | 只允许本机连时填 `127.0.0.1` |
| `token` | 空 | 握手校验。**端口暴露在公网时不要留空** |
| `max_sends_per_minute` | `30` | 防止失控刷屏，`0` 表示不限 |
| `image_mode` | `auto` | 群图片怎么给 Cortico：`auto` 能读图就传原图、否则转述；`image` 始终原图；`describe` 始终转述；`off` 只留占位 |
| `image_vision_provider` | 空 | 「转述」这条路用哪个视觉模型；留空自动挑一个带 vision / VL 的 |

### 两种消息范围

- **A 模式（默认）**：只把被 @ 或叫到 `name_aliases` 的消息交给 Cortico。群里闲聊不进上下文。
- **B 模式**：每条群消息都交过去，由 Cortico 自己决定要不要开口。更适合「住在这个群里」的用法，token 消耗也明显更高。

私聊不受模式影响，一律交给 Cortico。

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

- 桥是明文 WebSocket。连接必须先发 `hello` 并通过 token 校验，未握手前不会收到任何 QQ 消息，也不能发 `send` / `tool` 帧。
- `token` 留空等于允许任何能连到端口的人接管桥；公网部署务必设置 token，并尽量用防火墙/安全组限制来源 IP。需要跨公网时建议再套一层 TLS 反代或在专网/VPN 里跑。
- 截图走 Coopanion 的 CUA，只有主人在说话时放行。

## 开发

```bash
python -m py_compile main.py bridge.py deliver.py
```

服务器侧热更新：把整个目录覆盖到 `data/plugins/astrbot_plugin_cortico_bridge/` 后重载插件。

协议帧定义见 [cortico-world-astrbot 的 README](https://github.com/as124da1231/cortico-world-astrbot#桥的协议)。

## 许可

[MIT](LICENSE) © 2026 as124da1231
