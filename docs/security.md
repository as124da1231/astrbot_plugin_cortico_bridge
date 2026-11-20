# 桥的接入安全模型

状态：安全升级已部署到测试服（2026-11-20）；Header 凭据与协议版本门已本地完成，待测试服 SSH 恢复后部署。

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
| 默认监听 | `127.0.0.1` |
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

## 协议版本

```text
World 在 hello 里发 v: 1
插件只接受 1
缺省按 1 处理；其他值 close 4004
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

生成
  → 状态页 token 徽标点击生成
  → secrets.token_urlsafe(32)，43 字符
  → 写回插件配置并重启监听
  → 复制到 Coo 的 worlds.astrbot.token

清除
  → 状态页生成入口可清除
  → 非回环会因缺 token 停止监听
```

日志只记录 token 位数，不打印全文。状态页读取 token 需要经过 AstrBot 管理后台。

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
测试服（0.0.0.0 + 48 位 token） → connected=True
```
