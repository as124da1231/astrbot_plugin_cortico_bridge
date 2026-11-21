# 文档索引

## 第一次部署

- [从零接入：Cortico 桥 + Coo + QQ](tutorial.md)

从安装插件、配置 World，到第一条私聊、群聊和工具调用。

## 理解实现

- [架构：桥、World 与边界](architecture.md)

消息链路、三个钩子、指令切分、UMO、图片、安全、工具链、Coo 侧边界。

- [参考：配置、错误码、状态与接口](reference.md)

配置默认值、错误码、状态字段、Web API、协议帧、文件权限、日志关键字。

- [接入安全模型](security.md)

回环免密、Origin 拒绝、非回环 token、`/llm off` 的行为。

## 跨仓库契约

- [UMO 桥协议契约](https://github.com/as124da1231/cortico-world-astrbot/blob/main/docs/umo-bridge-contract.md)
- [Coo World 边界](https://github.com/as124da1231/cortico-world-astrbot/blob/main/docs/coo-world-boundary.md)
- [Coo 侧实现细节](https://github.com/as124da1231/cortico-world-astrbot/blob/main/docs/coo-side.md)

## 按角色阅读

```text
部署的人     tutorial → reference
维护的人     architecture → reference → security
改 World 的人 architecture → coo-side → 两个契约
```

## 当前验证

```text
插件 unittest     156 通过
World Vitest      全部通过
World typecheck   通过
前端 app.js       node --check 通过
```

用例数量随版本变化，以实际输出为准（1.3.6 实测 156）。
