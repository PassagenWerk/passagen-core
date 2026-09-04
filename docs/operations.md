# Passagen Core Operations

本文档定义所有适配器共享的数据、provider、pipeline、artifact 和诊断运行语义。CLI 的具体
命令见
[`passagen-cli/docs/operations.md`](../../passagen-cli/docs/operations.md)，Web 服务操作见
[`passagen-web/docs/release.md`](../../passagen-web/docs/release.md)。

## Data Directory

Core 接收调用方显式提供的 `data_dir` 和 database path，不读取用户 Home 或决定当前工作
目录。一个完整数据目录包含：

```text
data/
  passagen.db
  pdfs/
  papers/
  backups/
```

数据库中的 artifact 路径相对 `data_dir` 保存。路径解析必须阻止目录逃逸，并校验文件
存在性、大小和可用时的 SHA-256。

跨机器迁移时复制完整 `data_dir`，不能只复制数据库。调用方应在没有活动写操作时创建
SQLite 一致性备份，并在目标机器执行 artifact 完整性检查。

## Schema And Migrations

- SQLAlchemy model 和 Alembic revision 只由 Core 提供。
- Migration 只向前执行，已经发布的 revision 不修改。
- 新数据库升级到 head；受支持的 legacy schema 经结构和完整性检查后原地登记版本。
- 数据库版本高于当前 Core 支持版本时立即拒绝启动。
- 迁移不重建、静默删除或重新导入用户业务数据。

CLI 和 Web 必须使用同一 Core 版本访问一个数据目录，不能各自携带不同的 Schema 定义。

## Provider Configuration

Core 使用经过校验的 provider 配置，不决定 YAML、环境变量或 HTTP request 的入口形式。

GROBID 配置包括 `base_url` 和 timeout。`pymupdf` 不依赖 GROBID；`grobid` 要求服务可用；
`auto` 按 Core 的 parser 策略选择和降级。

OpenAI-compatible LLM 配置包括：

```yaml
providers:
  llm:
    base_url: https://api.openai.com/v1
    model: gpt-4o-mini
    api_key_env: PASSAGEN_API_KEY
    timeout_seconds: 120
    disable_thinking: false
```

Core 只读取调用方解析后的配置，并从指定环境变量取得 token。API key 只进入
Authorization header，不得写入配置输出、普通日志、数据库或诊断 artifact。

## Prompt Templates

Core 内置 facts、summary、repair 和 outline 的版本化模板，并支持调用方提供外部路径。
模板使用 Python `string.Template`：

```text
facts:   $schema, $chunk
summary: $schema, $identity, $facts
repair:  $schema, $validation_error, $candidate
outline: $schema, $summary
```

字面 `$` 写为 `$$`。Core 校验模板可读性、占位符和输出 Schema。Facts cache key 包含模板
SHA-256；summary、repair 或 outline 模板发生变化后，调用方应显式请求重建已有最终产物。

## Pipeline Recovery

Paper 按以下阶段推进：

```text
discovered -> metadata_resolved -> parsed -> summarized -> outlined
```

- 状态只在对应阶段产物成功提交后推进。
- 失败保留最后成功状态和失败 processing run。
- 默认恢复从最后成功阶段继续，不自动无限重试。
- 强制重建从 metadata 开始，并按当前 Core 版本生成后续 artifact。
- 外部调用不发生在数据库写事务持有期间。
- 临时 artifact 先安全写入并校验，再通过短事务登记。

## Managed Artifacts

原始 PDF 按 SHA-256 内容寻址并视为不可变对象。Paper 生成内容保存在
`papers/<paper-id>/`，数据库保存 kind、相对路径、大小、hash 和版本信息。

调用方不能根据约定路径猜测 artifact，必须通过 Core catalog 或 artifact service 解析。
缺失、越界、hash 不匹配或 Schema 不兼容均返回稳定 Core 错误。

## Logging And Diagnostics

Core 使用标准库 `logging` 发出结构化事件，但不安装 handler、不设置全局级别，也不决定
终端、文件、journal 或 HTTP 展示。CLI 和 Web 分别配置宿主日志。

普通日志只记录 operation/run/call ID、状态、模型、token、耗时、错误类别和诊断路径。
完整 prompt、request、raw response、parsed response 和 validation error 属于诊断 artifact，
不应写入普通日志。

目标共享布局为：

```text
data/runs/<run-id>/
  run.json
  llm/<call-id>/
    request.json
    prompt.txt
    response.txt
    parsed.json
    error.json
```

在统一 run artifact store 完成前，现有 paper pipeline 仍可接收调用方提供的诊断目录；这
属于过渡 contract，不能扩展到新的 collection research 功能。新的 report 和 conversation
必须直接使用 Core 管理的 run/diagnostic contract。

## Retention And Security

- 永不保存 API key、Authorization header、完整环境变量或 provider 认证对象。
- Prompt 和 raw response 的保留策略应可配置并具有安全默认值。
- 删除 report、conversation 或 run 时，由 Core 定义关联诊断内容的级联规则。
- 清理过程必须同时维护数据库索引和文件，不能留下悬空引用。
- 普通日志可以独立轮转；它不是业务 artifact 的唯一副本。

## Failure Categories

Core 对调用方暴露稳定错误类别：

- 配置或输入校验错误。
- Schema 不兼容或数据库繁忙。
- Paper、collection、tag 或 artifact 不存在。
- 唯一约束和并发更新冲突。
- Provider timeout、认证、限流或无效响应。
- Artifact 读取、hash 或 Schema 校验失败。

CLI 将这些错误转换为消息和退出码，Web 转换为 HTTP response；适配器不能改变事务和恢复
语义。
