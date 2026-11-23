# 桥的接入安全模型

## 目标

```text
只有被授权的 Coo 能连进来
本机默认零配置
离开本机必须带密钥
浏览器页面连不进来
```

## 规则

| 场景 | 行为 |
|---|---|
| 默认监听 | `127.0.0.1`（未配置时也按回环启动） |
| 回环 + 空 token | 允许启动，不校验 token（本机免密） |
| 回环 + 有 token | 校验 token |
| 非回环 + 空 token | 拒绝绑定 |
| 非回环 + 有 token | 校验 token |
| 带 `Origin` 的连接 | 一律拒绝，close 4403 |
| URL 携带 `token` / `access_token` | 一律拒绝，close 4401 |
| `hello` 的 `v` 不是 3 | 拒绝，close 4004 |
| `Authorization` 令牌不匹配或 `hello` 带令牌 | 拒绝，close 4003 |
| `enabled=false` | 不开放监听；发送与工具调用返回 `door_closed` |

## 凭据通道

```text
Authorization: Bearer <token>
```

两种跨机连接均使用同一枚桥接令牌。WS 本身是明文，WSS 由代理提供 TLS，Coo 按填写的地址验证证书。旧的非 ASCII 令牌使用 AstrBot 管理页生成的新令牌替换。

## 协议版本

```text
World 在 hello 里发 v: 3
插件只接受 3
缺省或其他值 close 4004
同一连接重复 hello 也会重新校验 token 和版本
```

1.3.9 两端都使用 v3。模式协商完成前，消息继续走 AstrBot 的常规处理。

## 关闭码

| code | 含义 |
|---|---|
| 4001 | 握手超时（10 秒内没发 hello） |
| 4002 | 第一帧不是 hello |
| 4003 | token 不匹配 |
| 4004 | 协议版本不支持 |
| 4401 | URL 里带了 token |
| 4403 | 带了 Origin（浏览器） |

## token 生命周期

```text
空 token
  → 回环：本机免密
  → 非回环：拒绝启动

查看 / 复制
  → 控制台「概览」的「复制」按钮
  → GET /token（AstrBot 管理后台鉴权）
  → GET /status 只回 tokenSet，不回明文

生成
  → 控制台点「生成（轮换）」
  → secrets.token_urlsafe(32)，43 字符
  → 写回插件配置并重启监听
  → 新 token 一次性显示，复制到 Coo 的 worlds.astrbot.token

清除
  → 控制台点「清除」
  → 非回环会因缺 token 停止监听
```

日志只记录 token 位数，不打印全文。复制接口需要经过 AstrBot 管理后台。

## 工具与主人

- QQ 轮次使用 AstrBot 保存的真实发言者记录。Coo 对话使用桥端指定的主人 QQ，该号须在 `admins_id` 里。
- 桌面主人工具与主动发送分别由 AstrBot 管理员授权，两项均要求连接通过桥接令牌鉴权。旧的独立凭据保留用于识别待迁移配置和回退。
- 安全模式只接入绑定主人私聊。转为完整模式时，AstrBot 后台列出群、私聊、工具和桌面目标，管理员确认后才启用。
- `tools_allow=["*"]` 只含在 `tools_risks` 中明确标为 read / write 的工具。未审查的工具按 exec 处理。exec 需要显式点名、管理员身份和已获工具授权的连接。
- 权限拒绝会如实失败，不会包装成成功。

## 为什么回环可以免密

- 浏览器页面已经被 `Origin` 拦住。
- 同用户的本地进程本来就能读配置，token 挡不住它们。
- 这与 Ollama、LM Studio、NapCat 本机部署的信任模型一致。
- token 的真正价值在离开本机时：局域网、公网、跨机 Coo。

## 验证

```text
本机回环 + 空 token  → 握手成功，ready
本机回环 + Origin    → close 4403
非回环 + 空 token    → 拒绝绑定
非回环 + token       → 允许
?token=              → close 4401
令牌放进 hello        → close 4003
hello.v=2            → close 4004
```
