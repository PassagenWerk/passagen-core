# Passagen Core

Passagen Core 是 Passagen 论文库的共享业务层。它负责 PDF 导入、元数据、全文解析、
Abstract clean、结构化 Summary、Outline、SQLite 数据和受管理 artifact，并为 CLI 与 Web
提供一致的处理和查询能力。

Core 本身不提供命令行或 HTTP 服务。一般用户应选择以下入口：

- [Passagen CLI](../passagen-cli/)：导入和批量处理论文。
- [Passagen Web](../passagen-web/)：通过浏览器管理和阅读论文库。

## 功能

- 按 SHA-256 导入和去重 PDF。
- 从 PDF、Crossref、arXiv 和 GROBID 合并论文元数据。
- 使用 GROBID 或 PyMuPDF 提取统一全文结构。
- 保留 Author Abstract 原文，并生成经过校验的 LLM-assisted cleaned view。
- 生成结构化英文 Summary 和技术 Outline。
- 管理可恢复的 processing run、论文状态、标签、集合和 artifact。
- 提供数据库迁移、完整性校验和安全的文件访问规则。

## 处理流程

```text
PDF import
  -> Metadata
  -> Full text
  -> Abstract clean (non-blocking)
  -> Summary
  -> Outline
```

Abstract clean 失败只产生 warning，不阻塞 Summary 和 Outline。其他持久化阶段失败时，论文
保留在最后成功状态，修复配置或外部服务后可以继续处理。

## 安装

从源码使用 Core：

以下 URL 可替换为你使用的 GitHub、GitLab 或 Gitea 镜像地址：

```bash
git clone https://github.com/PassagenWerk/passagen-core.git
cd passagen-core
uv sync --frozen
```

CLI 和 Web 的源码环境会通过相邻 checkout 使用 Core，无需单独启动服务。

## 配置

CLI 和 Web 读取同一个 `<data-dir>/passagen.yaml`。DeepSeek、GROBID、Crossref、arXiv、
pipeline 参数和环境变量覆盖方式见[共享配置指南](docs/user/configuration.md)。

## 常见问题

- 找不到 `passagen` 命令：Core 没有可执行入口，请安装 Passagen CLI。
- Web 或 CLI 报 schema 不兼容：升级到相互兼容的版本，并通过 CLI 运行 migration。
- Artifact 缺失或 hash 不匹配：恢复完整 data directory 备份，不要只替换数据库。
- Provider 连接失败：使用 CLI `passagen check` 检查 DeepSeek、GROBID 和 metadata provider。

## 文档

- [用户文档索引](docs/README.md)
- [共享配置](docs/user/configuration.md)
- [数据、备份与恢复](docs/user/operations.md)
- [设计](docs/development/design.md)
- [架构](docs/development/architecture.md)
- [Roadmap](docs/roadmap/README.md)

## 许可证

[GNU Affero General Public License v3.0](LICENSE)，SPDX 标识为 `AGPL-3.0-only`。
