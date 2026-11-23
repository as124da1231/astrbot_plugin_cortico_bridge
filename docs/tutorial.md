# 从零接入：Cortico 桥 + Coo + QQ

这份教程面向第一次部署的人。目标只有一条：

```text
QQ 里的消息交给 Coo 想，QQ 里的回复由 Coo 说
AstrBot 继续管连接、账号、群/私聊策略和工具
```

---

## 0. 你需要

- 一台跑着 AstrBot 的机器，QQ 已经登录
- Coopanion / Coo，已安装 `cortico-world-astrbot`
- 知道两边是同一台机器，还是分开两台

同机和跨机的配置只在 `listen_host` 和 `token` 上有区别。

---

## 1. 安装 AstrBot 插件

把 `astrbot_plugin_cortico_bridge` 放进：

```text
<AstrBot>/data/plugins/astrbot_plugin_cortico_bridge
```

或在插件市场搜索安装，然后重启 AstrBot。

启动日志里应该看到：

```text
Cortico Bridge: 已在 127.0.0.1:17999 等待 Cortico 连接
```

如果看到「未启用，不开放监听口」，去配置页打开「接管对话」。

---

## 2. 配置插件

### 同机（Coo 和 AstrBot 在同一台电脑）

```text
enabled          开
listen_host      127.0.0.1
listen_port      17999
token            留空
bridge_platforms default
```

回环 + 空 token 是允许的：本机 Coo 直接连，不需要密钥。  
浏览器页面仍然会被 `Origin` 校验挡住。

### 跨机（Coo 在另一台机器）

```text
listen_host      Coo 或代理可达的地址（代理和桥同机可用 127.0.0.1）
token            必填
```

选择 `ws://` 直连，或通过启用 TLS 的 WebSocket 代理使用 `wss://`。插件控制台「概览」可生成或复制访问 token，复制后填到 Coo「连接」页的「桥接令牌（Token）」。WS 本身是明文。WSS 代理的证书须覆盖填写的 IP 或域名。
也可以自己填一串，两边保持一致。`GET /status` 不会回 token 明文，复制 / 轮换 / 清除都走 `/token`。

---

## 3. 安装 Coo 侧 World

在 Coo 的扩展页安装 `cortico-world-astrbot`，或在开发目录用 link 加载。

World 配置：

```text
wsUrl   同机：ws://127.0.0.1:17999
        跨机直连：ws://<服务器IP>:17999
        跨机加密：wss://<代理IP或域名>/<桥接路径>
token   和插件一致（同机可空）
```

World 只负责连接和展示；群/私聊策略、工具白名单都在插件里。

两端用同一版本，点击 Coo「连接」页的「保存并应用」。安全模式在 AstrBot 插件「概览」绑定主人私聊。需要桌面工具时，在 Coo 请求切换到完整模式，经 AstrBot 管理员确认后授权对应工具并绑定管理员 QQ。桌面主动发送单独选择目标。

---

## 4. 确认连接

插件状态页：

```text
已连接 / 等待连接 / 监听失败 / 未监听
```

Coo 的事件流里应该出现：

```text
astrbot.connected
```

如果一直是「等待连接」：

```text
1. 地址和端口是否指向 AstrBot 那台机器
2. 安全组/防火墙是否放行 17999
3. 跨机时 token 是否两边一致
4. Docker 部署时 Coo 要连宿主机地址，不是容器内的 127.0.0.1
```

---

## 5. 第一条消息

### 私聊

给 bot 的 QQ 发一句普通消息。

期望：

```text
插件日志：已推送 conv=private:<QQ号>
QQ 私聊：Coo 的回复
```

### 群聊

在群里 @ 一下 Coo、回复 Coo 的消息，或者叫配置里的称呼。

期望：

```text
插件日志：已推送 conv=group:<群号>
QQ 群：Coo 的回复
```

默认 `group_mode=mention`：只有 @ / 回复 / 命中 `name_aliases` 才会唤醒并直接交给 Coo；普通闲聊不进上下文。`@全体` 不算点名；`group_scope=allowlist` 时名单外的群不进；桥断开时也不唤醒，消息留给 AstrBot。`group_mode=all` 才会每条都进。

### 图片

图片按 `image_mode` 处理：

- 对端能收图时传原图（单张原图 6MB、base64 8MB、单帧合计 10MB）；
- 对端不能收图时，`auto` / `image` 会尝试视觉转述；没有视觉模型就在正文里写明原因；
- `describe` 始终转述，`off` 只留 `[图片]`；
- 视觉模型超时或报错只影响这一张图，不会中断整轮消息。

### 工具

在私聊里发：

```text
帮我查一下《晴天》的歌词
```

前提是 `tools_allow` 里有 `query_lyrics_by_name`。

期望：

```text
插件日志：执行工具 query_lyrics_by_name
QQ：歌词或失败原因
```

`tools_allow=["*"]` 只自动包含管理员在 `tools_risks` 中明确标记的 read / write。其他工具按 exec 管理，须显式点名、具备管理员身份，并使用已获得工具授权的连接。权限拒绝会如实失败，不会回“成功”。

---

## 6. 指令与静音

### 指令归 AstrBot

```text
/help、/cortico 等以 wake_prefix 开头的消息
→ AstrBot 处理，Coo 不接
```

私聊里不带 `/`、但命中已注册指令名的消息，也会交给 AstrBot。

### `/llm off`

`/llm off` 是这个会话的静音开关：

```text
AstrBot 的模型不跑
Coo 也收不到这一轮
指令仍然可以执行（包括 /llm on）
```

---

## 7. 常见问题

| 现象 | 先查 |
|---|---|
| 一直等待连接 | 地址、端口、安全组、token、Docker 网络 |
| 私聊没反应 | `private_mode` 是否 `off`；是否被 `/llm off` |
| 群里不回 | 是否 @ / 回复 / 叫到称呼；`@全体` 不算；`group_scope` / `group_allow`；`group_mode`；桥是否连接 |
| 图片看不到 | 对端是否声明支持图片；`image_mode`；是否配了视觉模型（看 `[图片|...]` 后的原因） |
| 回复出现在别的群 | 是否同一批里有多个 QQ 会话；让 Coo 用 `conv_id`/`reply_to` |
| 桌宠说的话跑到 QQ | 1.3.6 起非 QQ 轮次可以主动发，但必须「连接」里填了主人 QQ（且是 AstrBot 管理员），并用 `conv_id`/`reply_to` 指定已见过的会话；不想让它发就清空主人 QQ。也要确认 Coo 完整重启、加载了最新 World |
| 一直重连、提示「token 不匹配」 | 核对两端桥接令牌是否一致，反向代理是否保留 Upgrade 请求的 Authorization 头 |
| 一直重连、提示「协议版本不匹配」 | 升级较旧的一边；插件只接受 `v: 1` |
| 工具失败 | `tools_allow`、工具是否停用、这一轮消息是否超过 600 秒 |
| exec 工具调不了 | 核对是否为完整模式、工具是否显式点名，以及 QQ 发件人或桌面绑定的主人是否获 AstrBot 授权 |
| Coo 对话里调工具失败 | 连接设置里是否填了主人 QQ；该 QQ 是否在 `admins_id` |
| 指令被 Coo 接走 | 是否装了会 stop 事件的唤醒插件；看它是否先拦 |
| 面板显示旧样式 | 刷新页面；确认插件三个前端文件已更新 |

---

## 8. 安全清单

```text
默认只监听 127.0.0.1
跨机必须设 token
带 Origin 的 WebSocket 一律拒绝
URL 里带 token 一律拒绝
enabled=false 时不开放监听
日志不打印 token 全文
```

同机免密不是「没有安全」，而是「只信任本机 + 拦浏览器」。  
要防本机其他进程，生成 token；要防局域网，保持回环。

---

## 9. 升级与回滚

### UMO 会话表

第一次启动新版本时，旧的 `umos.json` 会自动迁移：

```text
umos.v1.json   迁移前备份，只保留一份
umos.json      v2，以 UMO 为主键
```

回滚时把 `umos.v1.json` 改回 `umos.json` 即可。

### World 代码

World 是 link 加载时，改完 `src/world.ts` 要**完整重启 Coo**。  
只重启 World 实例不一定清掉 ESM 模块缓存。

### 测试

```bash
# 插件
cd astrbot_plugin_cortico_bridge
python -m unittest discover -s tests

# World
cd cortico-world-astrbot
pnpm typecheck
pnpm test
```
