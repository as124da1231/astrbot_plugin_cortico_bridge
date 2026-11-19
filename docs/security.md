# 桥的接入安全模型

状态：已实施并部署到测试服（2026-11-20）。

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
| `enabled=false` | 不开放监听；发送与工具调用返回 `door_closed` |

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
测试服（0.0.0.0 + 48 位 token） → connected=True
```
