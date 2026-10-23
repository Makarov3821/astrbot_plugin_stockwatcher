# AstrBot 插件接入审查（2026-10-20）

结论：当前包符合本地安装所需的核心结构与公开 API 用法；不是逐行复制模板，也尚未完成插件市场发布或真实平台联调。已对照官方说明、helloworld 模板、上级目录正在使用的 Vikunja 插件和实际 AstrBot 源码。

本次按 AGENTS.md 先检查共享仓库干净，再成功 `pull --ff-only`，由 `dffe922` 更新到 `fc6b326`（master，版本文件仍标为 4.29.1）。v0.5.0 交付时的 TLS 拉取失败记录保留为历史，不再代表此次审查状态。

| 核对项 | 结果和依据 |
| --- | --- |
| 插件名与目录 | `astrbot_plugin_stockwatcher` 小写、无空格；ZIP 外层目录内直接放 `main.py`，附 `metadata.yaml` |
| 插件类发现 | 主类继承 `astrbot.api.star.Star`，调用 `super().__init__(context)`；真实 `Star.__init_subclass__` 自动登记模块与类，因此无需额外的 `@register` |
| 模板差异 | helloworld 仓库仍带 `@register`，官方当前最小实例不带；Vikunja 同样继承 Star 而不使用该装饰器。单命令配内部解析器也可用，不强制改成 Vikunja 的 command_group |
| 指令接口 | 装饰器位于主类中，前两个参数为 self、event。真实 CommandFilter 接受无位置参数的主入口和后续子命令，保留事件原始文本供 shlex 解析。已补 handler 描述，便于宿主展示 |
| 配置 | `_conf_schema.json` 为宿主 schema，构造函数接收 AstrBotConfig；object/items、默认值和密钥 secret 标记符合文档。模型选择已加公开 `_special: select_provider`，留空沿用会话模型 |
| 依赖和网络 | `requirements.txt` 与 pyproject 均声明 httpx；异步 HTTP，不使用 requests，不在插件依赖中另装 AstrBot |
| 数据目录 | `StarTools.get_data_dir` 创建 `data/plugin_data/astrbot_plugin_stockwatcher`；数据库/备份不落入会被更新覆盖的插件源码目录 |
| 生命周期 | initialize 建立资源、持有单一后台任务；terminate 取消并等待任务，再关闭客户端和数据库。未开启监控时不请求外部行情 |
| 日志/模型/发送 | 使用 `astrbot.api.logger`、Context 的模型 API 与 send_message，接入方式与现有 Vikunja 插件一致；没有为相似外观引入无关的 Web API 或权限体系 |
| 打包 | 白名单 ZIP、逐文件及整包哈希，无本地源码软链接、虚拟环境、私有配置或数据库；大小远低于官方市场说明的 16MB 上限 |
| README 参考说明 | 已补官方接入资料和多角色研究灵感项目链接，符合官方关于注明参考来源的要求 |
| 市场发布信息 | 后续已按用户提供的地址补齐 `repo: https://github.com/Makarov3821/astrbot_plugin_stockwatcher`；实际本地加载器必填字段为 name、desc、version、author。许可选择与市场发布流程仍待完成 |

`short_desc`、logo 和 support_platforms 在文档中为可选字段。已添加简短描述；不会为凑齐模板而宣称所有平台均完成测试。特别是 Context.send_message 文档仍提示 QQ 官方平台的限制，是否能稳定主动推送应以你的平台实测为准，不能只凭 Vikunja 声明了某个平台就照抄保证。

## 验证范围

- 原有 172 项离线测试覆盖业务、入口替身、生命周期、HTTP 替身、打包和恢复。
- 额外 4 项开发环境核对直接加载共享源码中的真实 Star 注册实现和 CommandFilter，验证免 register 注册、主命令/别名及带引号的子命令文本。日志、存储混入类和 I/O 相关依赖由测试替身提供，不能称为完整运行中的 AstrBot。
- 这些额外测试在没有开发用软链接的检出中会明确跳过；软链接不是安装包运行依赖。
- 真实宿主加载、配置 UI、消息平台和真实模型/数据权限仍按 [安装测试说明](INSTALL_TEST.md) 验收。

## 对照资料

- [官方开发指南](https://docs.astrbot.app/dev/star/plugin-new.html)
- [官方最小实例](https://docs.astrbot.app/dev/star/guides/simple.html)
- [官方配置说明](https://docs.astrbot.app/dev/star/guides/plugin-config.html)
- [helloworld 入口](https://github.com/Soulter/helloworld/blob/master/main.py)
- [官方发布说明](https://docs.astrbot.app/dev/star/plugin-publish.html)
- [AstrBot 此次参考提交](https://github.com/AstrBotDevs/AstrBot/commit/fc6b326)
- 本地对照：`../astrbot_plugin_vikunja/main.py`、`metadata.yaml` 和 `_conf_schema.json`，仅作接入方式参考，未改动该插件。
