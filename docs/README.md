# Passagen Core Documentation

仓库文档同时发布到多个 Git 远端。README 中的跨仓库链接使用以 /PassagenWerk/ 开头的站点根路径，docs 内文件以纯文本注明目标仓库与路径，保证各远端解析一致。

## 用户文档

- [Shared configuration](user/configuration.md)
- [Data, backup, and recovery](user/operations.md)

## 开发文档

- [Design](development/design.md)
- [Architecture](development/architecture.md)
- [Code style](development/code-style.md)

## Roadmap

- [Roadmap index](roadmap/README.md)
- [Completed capabilities](roadmap/completed/core-platform.md)
- [Planned capabilities](roadmap/planned/collection-research-and-exploration.md)

## 版本约定

CLI 和 Web 的 minor 版本与其要求的 Core minor 保持一致：同一条 `0.5.x` 线上的三个包互相
兼容。patch 版本各自独立演进；Core 的 minor 升级触发适配器至少一次对齐发布。

Adapter documentation:

- Passagen CLI 仓库的 docs/ 目录
- Passagen Web 仓库的 docs/ 目录
