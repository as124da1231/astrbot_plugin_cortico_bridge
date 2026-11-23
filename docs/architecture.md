# 架构：桥、World 与边界

这份文档说明「消息为什么这样走」，不重复安装步骤。

---

## 1. 两个包

```text
astrbot_plugin_cortico_bridge   AstrBot 侧，Python
cortico-world-astrbot           Coo 侧，TypeScript
```

插件是**服务端**，World 是**客户端**。  
桥只负责连接、收发和策略；人格、记忆、要不要说话由 Coo 决定。

---

## 2. 一条消息的完整链路

```text
QQ 消息
  → AstrBot 平台适配器
  → 插件事件层（最后执行）
      指令 / 自轮询插件先跑
      通知类直接推
      mention 命中：直接推 + stop
      普通消息只做标记（all 模式）
  → LLM 阶段
      插件钩子（第一个执行）
      推帧 + stop
  → WebSocket
  → World driver
  → world.onBridgeMessage
  → pushEvent
  → 模型
  → astrbot_send_message / astrbot_call_tool
  → WebSocket
  → 插件发送/工具
  → AstrBot send_message(umo) / 工具执行
  → QQ
```

`mention` 的主路径是**事件层直接推送**，不依赖 LLM 管线顺序；推送成功后置 `_cortico_pushed` 并 `stop_event()`，LLM 层不会二次推送。

---

## 3. 三个钩子

| 钩子 | 优先级 | 作用 |
|---|---|---|
| `on_astrbot_loaded` | 默认 | 所有插件加载完后重推工具清单 |
| `event_message_type(ALL)` | `-sys.maxsize` | 最后执行；指令判断、通知、标记 |
| `on_llm_request` | `sys.maxsize` | 第一个执行；推 Coo + stop |

### 事件层

```text
指令（wake_prefix 或已注册指令名）
  → 交给 AstrBot
通知（撤回 / 戳一戳 / 入群…）
  → 桥已连接且范围允许：推送并 stop
  → 桥断开或范围外：不拦，其他插件仍能看到
桥断开
  → 不唤醒、不标记
群范围 allowlist 且不在名单
  → 不进入
group_mode=mention
  → @ 自己 / 回复自己 / 命中 name_aliases：直接推 + stop
  → @全体不算点名
  → 普通闲聊不进入
group_mode=all / 私聊
  → set_extra("_cortico_llm_owned", "group" / "private")
  → 不推、不 stop
```

`mention` 命中时同时设置 `is_at_or_wake_command = True`，让 AstrBot 4.29.1 的后续阶段也把这一轮当作已唤醒；真正起作用的是这个字段，不是 `is_wake`。

### LLM 层

```text
没有 _cortico_llm_owned → 不是 Coo 的轮次，放行
是指令                   → 交给 AstrBot
已经直接推过 / 已 stop   → 不重复推
有标记                   → 推帧 + stop
```

`stop_event()` 之后，后面的 `on_llm_request` 注入全部跳过。  
只在 Coo 接管的轮次生效，其他插件的 LLM 轮次不受影响。

---

## 4. 指令怎么切

三条规则：

```text
1. wake_prefix 前缀（默认 /）
2. 原始消息组件里的前缀（唤醒检查会剥掉 message_str 的前缀）
3. 已注册指令名（无前缀私聊指令）
```

第三条只在 `is_at_or_wake_command` 为真时生效，避免群聊误判。  
指令名从 AstrBot 的 handler 注册表读取，缓存 30 秒。

执行顺序：

```text
wakepro / 注册指令 / 自轮询插件
  → 我们的事件层（最后）
  → LLM 层
```

谁先 stop，谁赢；没人 stop，才归 Coo。

---

## 5. UMO

### 格式

```text
platform:MessageType:session_id
```

例：

```text
default:GroupMessage:123456789
default:FriendMessage:100000001
```

规则：

```text
只切前两个冒号，session 里允许冒号
平台/类型/会话为空 → 非法
最大 512
```

### 注册表 v2

```json
{
  "version": 2,
  "umos": {
    "default:GroupMessage:123456789": {
      "umo": "default:GroupMessage:123456789",
      "platform": "default",
      "kind": "group",
      "id": "123456789",
      "name": "",
      "firstSeen": 0,
      "lastSeen": 0,
      "lastMessageId": "",
      "lastSender": ""
    }
  },
  "legacy": {
    "default|group|123456789": "default:GroupMessage:123456789"
  }
}
```

```text
上限 500
30 天 TTL
原子写 + 权限 600
旧 v1 迁移时备份 umos.v1.json
```

### 单通道

发送只认 `umo`：

```text
missing_umo        这一帧没有 umo
invalid_umo        格式不对
unknown_umo        桥没见过或已过期
umo_conv_mismatch  umo 和 conv 不一致
```

没有「按 conv 猜」的回退。旧 World 必须一起升级。

### 5.1 图片

```text
image_plan()
  off                          → 只留 [图片]
  对端 capabilities.images=true
    auto / image               → 传原图
    describe                   → 转述
  对端不能收图
    describe / auto            → 有视觉模型就转述
    image                      → 强制传原图
                                 没有就写明确原因，不静默丢图
```

上限：

```text
单张原图（World 侧）  6MB
单张 base64          8MB
单帧合计             10MB
WebSocket 单帧       16MB
```

视觉调用 25 秒超时；超时、异常、空描述都只记日志并写 `[图片|转述失败：原因]`，不影响这一轮的文字回复。

---

### 5.2 消息组件与媒体

桥按 QQ 消息原序构建组件树。正文、引用和转发都用同一种组件，引用与转发把原消息放在子层。组件位置由外层消息号和编号路径共同标识。桥端保留临时资源来源，Coo 保存可展示字段及已经收到的图片附件。

| 入口 | 图片 | 语音 |
| --- | --- | --- |
| 消息正文 | 随消息发送原图或视觉转述 | 随消息发送转写 |
| 引用 | 按同一图片规则发送，Coo 可从同一会话补用已保存的原图 | 引用可用时转写 |
| 合并转发 | 分页保留位置，打开图片组件时获取原图或转述 | 打开语音组件时转写 |

文件、视频和音乐当前提供适配器返回的资料字段。媒体回执明确区分已附图、已转述或转写、可按需读取、只有资料、已关闭和不可用。转发分页只整理内容和媒体位置，读取具体图片与语音才触发下载。已保存的引用和图片走 Coo 历史。未下载的转发媒体仍依赖桥的短期消息索引、QQ 文件可用期和当前模型设置。

需要回归正文直发、引用原文、转发嵌套、资源过期和跨会话拒绝。核对结果时要验证 Coo 实际收到了图片附件或转写，单有「图片」或「语音」标签只证明识别了组件类型。

## 6. 握手

```text
Authorization: Bearer <token>     首选
hello.token                       兼容保留
hello.v                           必须是 2
```

只给帧内 token 会记提示日志。token 建议 ASCII：HTTP 头放不了非 ASCII，
自定义中文 token 时 World 只发帧内 token。

---

## 7. 安全

```text
默认监听 127.0.0.1
回环 + 空 token   允许，不校验（本机免密）
非回环 + 空 token 拒绝绑定
回环 + 有 token   校验
非回环 + token    校验
```

来源：

```text
带 Origin 的 WebSocket → close 4403
URL 带 token/access_token → close 4401
```

凭据：

```text
Authorization: Bearer <token>   首选
hello.token                     兼容保留
两者都有时必须一致
```

版本：

```text
hello.v 必须是 2
缺省按 1，其他值 close 4004
```

状态门：

```text
enabled=false
  → 不开放监听
  → 发送返回 door_closed
  → 工具调用拒绝
```

token 生成：

```text
控制台点「生成（轮换）」
→ secrets.token_urlsafe(32)
→ 写回配置
→ 重开监听
→ 复制给 Coo
```

`GET /status` 只回 `tokenSet`，不回明文；复制 / 轮换 / 清除都走 `/token`。

日志只记录 token 位数，不打印全文。

---

## 8. 工具链

```text
模型调 astrbot_call_tool(name, args)
  → World driver.callTool(name, args, turn, actor)
  → 帧 {t:'tool', rid, name, args, turn, actor?}
  → 桥层身份校验
       turn 和 actor 都没有   → missing_identity
       turn 和 actor 同时有   → ambiguous_identity
       actor 不是 owner+id    → bad_actor
  → 插件 _on_tool_call
       QQ 轮次: turn → _turns 里的 event
       Coo 轮次: turn 空 + actor {kind:'owner', id}
         → id 必须在 admins_id
         → 合成主人私聊事件（role=admin）
         → event.send 落到主人私聊
       1. enabled
       2. 身份有效
       3. tools_allow
       4. exec 需显式点名且 trusted
       5. tool.call(ContextWrapper(event))
  → tool-ack
  → 模型
```

边界：

```text
QQ turn 不存在 / 超过 600 秒 → 拒绝
Coo 没有 actor               → 拒绝，让 Coo 去连接页填主人 QQ
actor 不在 admins_id         → 拒绝
工具不在白名单               → 拒绝
exec 只被 * 覆盖             → 拒绝，要求显式点名
exec 调用者不是管理员/主人   → 拒绝
权限拒绝 / 工具停用          → 如实失败，不回“成功”
管理员工具仍受 AstrBot 权限层约束
Coo 轮次不借上一条 QQ 消息，也不伪造群上下文
```

`trusted` 只由插件按 `admins_id` 计算；World 帧里的 `role` / `owner` 不参与判定。

---

## 9. Coo 侧边界

World 维护：

```text
turnChannel      'qq' / undefined
turnMultiConv    同一批有多个人话 QQ 会话
turnConv         当前 QQ 会话
turnMsgId        当前人话消息 id
```

只按本 World 的 `astrbot.message` 事件建立轮次（1.3.6），其他 World 的输入不参与判断。

规则：

```text
只有 QQ 消息         → QQ 出口开，可隐式回触发会话
非 QQ 轮次           → 只有填了主人 QQ（且在 admins_id 里）+ 显式 conv_id/reply_to + 已知会话时才开
多个人话 QQ 会话     → 隐式发送拒绝，要求 conv_id / reply_to
QQ 轮次已过期        → 拒绝，不退回 owner / lastConv
```

`pickConv` 对两种轮次都执行：

```text
conv_id → reply_to 查 knownMsgConv → 多会话拒绝 → turnConv（只有 QQ 轮次有）
```

发送不回退 `lastConv`；历史查询允许 `lastConv`。

`qqTurnActive()` 还有 2 分钟兜底，防止 `onTurnEnded` 没来。

---

## 10. 和内置 qq 的对照

| 维度 | 内置 qq | 我们 |
|---|---|---|
| 多会话 | 永远显式 `to` | 多会话才显式 |
| 单会话 | 也要显式 | 默认当前会话 |
| 主动发送 | draft + confirm | 直接发 |
| 草稿生命周期 | 每轮作废 | 无草稿 |
| 桌面轮次发 QQ | 允许（显式 + 确认） | 允许（主人 QQ + 显式目标 + 已知会话） |
| 目标检查 | World watch list | 插件 UMO 注册表 + 策略 |

共同点：**不猜**。  
不同点：**是否每一步都要说清、要不要确认**。

---

## 11. 已知交互

### 唤醒插件（wakepro）

`astrbot_plugin_wakepro` 的优先级是 99999，先于我们。  
它判「不唤醒」时直接 `stop_event()`，Coo 收不到；这是它的职责，不是桥的问题。

### `/llm off`

会话级静音，AstrBot 和 Coo 一起停。  
指令不受影响；通知仍作为上下文进入，但不触发回复。

### 其他插件的 LLM 注入

Coo 接管的轮次里会被 `stop_event()` 跳过：

```text
bilibili 跨端记忆
splitter 颜文字提示
live_stream_companion 的直播上下文 / Live2D 标签
内置群聊上下文
```

需要这些信息时，走工具让 Coo 主动取，不做自动注入。

---

## 12. 测试与验证

```bash
# 插件
python -m unittest discover -s tests

# World
pnpm typecheck
pnpm test
```

当前：

```text
插件 unittest     以当前运行结果为准
World Vitest      全部通过
World typecheck   通过
前端 app.js       node --check 通过
```

用例数量随版本变化，以实际输出为准。

实机验证：

```text
私聊来回
群 @ / 回复 / 称呼来回
工具调用（QQ 发言者 / Coo 主人）
exec 只被 * 覆盖时拒绝
图片原图 / 转述 / 无视觉模型时的明确原因
桌宠轮次：没填主人 QQ 时拒绝；填了主人 QQ + 显式目标时主动发送（1.3.6 新增，仅单测）
多会话轮次显式指定
```
