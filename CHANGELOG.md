# 更新日志

## 1.3.7

- 主人工具调用使用服务端指定的管理员和独立令牌。通配授权只覆盖已审查的读写工具，未审查工具需要显式授权。
- 保留旧群聊白名单，防止默认配置扩大接管范围。无效端口恢复默认并记录原因。
- 原图处理读取 AstrBot 的图片压缩大小。发送失败的错误码随本次结果返回。

## 1.3.6

### 修复

- **mention 唤醒**：默认 `group_mode=mention` 下，@ bot、回复 bot、命中 `name_aliases` 都会唤醒并直接交给 Coo；`@全体` 不算点名；非白名单群、普通闲聊、`/` 指令和桥断开时不进入。AstrBot 4.29.1 下设置 `is_at_or_wake_command`，并保留事件层直接推送兜底。
- **图片**：`auto` 在对端不能收图时改为视觉转述；没有视觉模型或调用失败会在正文写明确原因，不再静默丢图。`describe` / `off` 行为同步；视觉 provider 超时或异常不会中断消息。
- **工具风险门**：`tools_allow=["*"]` 只自动包含 read / write，不含 exec；exec 必须显式点名，且调用者必须是 AstrBot 管理员/主人；未知风险按 exec 处理。权限拒绝、工具停用、白名单外、轮次过期都会如实失败。
- **工具身份**：QQ 发言者与 Coo 主人两条路径；主人 QQ 必须在 `admins_id` 里，不再信任 World 帧里的 `role` / `owner`。
- **ack 代次**：发送回执缓存绑定连接代次，旧慢任务的回执不会污染新连接；同一 `rid` 重发仍复用上次结果。
- **发送错误**：`ack.error` 稳定为 `errors.py` 英文 code，`ack.note` 为中文说明；超时或未知异常带 `uncertain`。
- **控制台 token**：`GET /status` 不再返回 token 明文，只回 `tokenSet`；复制 / 轮换 / 清除改走 `/token`，保存成功后重开监听。
- **监听默认值**：`listen_host` 未配置时按 `127.0.0.1` 启动，与 schema 一致；非回环必须配 token。
- **通知与断线**：桥断开时撤回/入群等通知不再被插件吞掉；纯通知不建立 QQ 轮次。
- **会话表**：`umos.json` 坏值不再让插件起不来；无平台消息 id 时生成唯一 id，避免同文消息被吞。

### 文档

- README / tutorial / reference / architecture / security 按当前实现重写：协议帧补 `umo`、`tools` / `tool` / `tool-ack`、`ack`、`ready`、`hello` 版本；删除从未发送的 `notice` 帧示例，通知统一为 `msg` + `kind: "notice"`。
- `metadata.yaml` 与控制台五栏（概览 / 群聊 / 私聊 / 工具 / 语音）对齐；中英文 i18n 描述同步。
- 删除测试服部署进度与内部事故描述；测试数量改为“全部通过”，不再写死。
- 个人问题解答 QQ 只保留在 README 底部一处。
