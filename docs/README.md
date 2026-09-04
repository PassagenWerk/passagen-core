# Passagen Core Documentation

本目录记录 CLI 和 Web 共享的领域、存储、provider、pipeline、artifact 和后续 collection
research 能力。

## Documents

| Goal | Document |
|---|---|
| 理解产品目标、处理流程和数据格式 | [`design.md`](design.md) |
| 判断共享业务代码应该放在哪里 | [`architecture.md`](architecture.md) |
| 查看已实现里程碑和基础 roadmap | [`roadmap.md`](roadmap.md) |
| 数据、migration、provider、artifact 和诊断运行语义 | [`operations.md`](operations.md) |
| 编写、测试和检查共享 Python 代码 | [`code-style.md`](code-style.md) |
| Summary 上下文预算和语义切块改进 | [`roadmap-context-improving.md`](roadmap-context-improving.md) |
| Collection 综述、研究和对话探索 | [`roadmap-collection-research-and-exploration.md`](roadmap-collection-research-and-exploration.md) |
| 会议论文发现与筛选 | [`roadmap-discovery-and-screening.md`](roadmap-discovery-and-screening.md) |
| Core 拆分决策和迁移记录 | [`history/roadmap-core-split.md`](history/roadmap-core-split.md) |

## Related Adapters

- [`passagen-cli/docs`](../../passagen-cli/docs/README.md)：命令、终端输出和 CLI 运维。
- [`passagen-web/docs`](../../passagen-web/docs/architecture.md)：HTTP、浏览器 UI 和 Web runtime。

文档必须区分当前实现和未来计划。Schema、artifact 或 pipeline contract 改变时同步更新
Core 文档和测试；CLI/Web 只记录各自的适配行为。
