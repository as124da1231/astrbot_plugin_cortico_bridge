# 文档索引

## 第一次部署

- [1.3.13 新手图文教程：安装、实例接入和每项设置](beginner-guide.md)
- [旧版 QQ 部署参考](tutorial.md)

首次部署按新手教程操作，从安装两端扩展到绑定主人、测试私聊、开放群聊和授权工具。

## 理解实现

- [架构：桥、World 与边界](architecture.md)

消息链路、三个钩子、指令切分、UMO、图片、安全、工具链、Coo 侧边界。

- [参考：配置、错误码、状态与接口](reference.md)

配置默认值、错误码、状态字段、Web API、协议帧、文件权限、日志关键字。

- [接入安全模型](security.md)

回环免密、Origin 拒绝、非回环令牌和会话权限。

## 跨仓库契约

- [UMO 桥协议契约](https://github.com/as124da1231/cortico-world-astrbot/blob/main/docs/umo-bridge-contract.md)
- [Coo World 边界](https://github.com/as124da1231/cortico-world-astrbot/blob/main/docs/coo-world-boundary.md)
- [Coo 侧实现细节](https://github.com/as124da1231/cortico-world-astrbot/blob/main/docs/coo-side.md)

## 按角色阅读

```text
部署的人     beginner-guide → reference
维护的人     architecture → reference → security
改 World 的人 architecture → coo-side → 两个契约
```

## 当前验证

```text
插件 unittest     以当前运行结果为准
World Vitest      全部通过
World typecheck   通过
前端 app.js       node --check 通过
```

用例数量随版本变化，以实际输出为准。
