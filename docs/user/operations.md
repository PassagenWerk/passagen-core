# 数据与恢复

本文档说明 CLI 和 Web 共享的数据目录、数据库、artifact、备份与恢复规则。具体命令见
Passagen CLI 仓库的 docs/user/operations.md，
Web 服务操作见
Passagen Web 仓库的 docs/user/operations.md。

## Data Directory

一个论文库由完整 data directory 表示：

```text
library/
  passagen.yaml
  passagen.db
  pdfs/
  papers/
  runs/
  backups/
```

数据库中的 artifact path 相对于 data directory 保存。迁移或恢复时必须复制整个目录，不能
只复制 `passagen.db`。

## 数据库

Core 使用 SQLite 和只向前执行的 migration。CLI 的 `passagen db init` 同时创建新数据库和
升级受支持的旧 schema。数据库 schema 高于当前 Core 支持版本时，CLI 和 Web 都会拒绝启动。

不要手工修改 migration 版本、paper status 或 artifact 索引。需要升级时先停止所有使用该
论文库的进程，再创建备份并运行 migration。

## Managed Artifacts

原始 PDF 按内容 hash 管理；解析结果、Abstract clean、Summary、Outline 和 processing run
诊断保存在 data directory 中，并由数据库记录类型、相对路径、版本、大小和 hash。

不要根据文件名猜测 artifact，也不要单独移动 `papers/` 或 `pdfs/`。CLI 和 Web 会拒绝读取
越界、缺失、hash 不匹配或 schema 不兼容的 artifact。

## 备份

推荐使用 CLI 创建 SQLite 一致性备份，并同时保留 managed files：

```bash
passagen --data-dir /path/to/library db backup
```

完整目录备份示例：

```bash
cp -a /path/to/library /path/to/backups/library-$(date +%Y%m%d-%H%M%S)
```

执行目录复制前应停止 Web 和正在写入的 CLI 命令。

## 跨机器迁移

1. 停止源机器上的 CLI/Web 写操作。
2. 复制完整 data directory。
3. 在目标机器安装兼容的 Core、CLI 和 Web 版本。
4. 运行 `passagen --data-dir PATH db init` 应用受支持的 migration。
5. 运行 `passagen --data-dir PATH artifacts check`。
6. 启动 Web 并抽查 PDF、Abstract、Summary 和 Outline。

## 失败恢复

Paper 状态只在阻塞式阶段成功提交后推进：

```text
discovered -> metadata_resolved -> parsed -> summarized -> outlined
```

Abstract clean 是显式但非阻塞的阶段，因此不新增 PaperStatus。处理失败后，默认 update 从最后
成功状态继续；reprocess 则按选择的阶段使对应下游 artifact 失效。Abstract-only reprocess
只刷新 cleaned view。

## 安全

- API key 只存在于进程环境和 outbound Authorization header。
- 配置、数据库、普通日志和诊断 artifact 不保存 API key。
- Web 默认只监听 loopback，写请求验证 browser origin。
- 一个 Web 进程独占一个 data directory；不要删除 lock file 绕过活动进程。
