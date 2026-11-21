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
| `hello` 的 `v` 不是 1 | 拒绝，close 4004 |
| `Authorization` 与帧内 token 不一致 | 拒绝，close 4003 |
| `enabled=false` | 不开放监听；发送与工具调用返回 `door_closed` |

## 凭据通道

```text
Authorization: Bearer <token>     首选，凭据不进帧
hello.token                       兼容保留，升级期继续接受
```

两者同时出现时必须一致；只给帧内 token 会记一条提示日志。  
等 World 都升级完，可以把帧内 token 去掉。

token 建议用 ASCII（控制台生成的 `token_urlsafe` 就是）。HTTP 头放不了非 ASCII，
自定义的中文 token 会自动退回帧内路径；比较按 UTF-8 字节，不会抛 `TypeError`。

## 协议版本

```text
World 在 hello 里发 v: 1
插件只接受 1
缺省按 1 处理；其他值 close 4004
同一连接重复 hello 也会重新校验 token 和版本
```

版本不匹配时宁可断开，也不让「能连上但行为不对」的错配继续跑。  
将来升到 v2 时，旧 World 会收到明确的 4004，而不是静默发错消息。

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
- 主人身份须通过独立工具令牌授权。Coo 只在回环 WebSocket 或 WSS 上提交令牌，桥以收到的连接凭据和本地配置判定权限。工具令牌须与桥 token 不同。
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
Header + 帧内 token 不一致 → close 4003
hello.v=2            → close 4004
```
