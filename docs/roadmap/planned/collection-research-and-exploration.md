# Roadmap: Collection Research and Exploration

# Collection Research and Exploration

**状态：计划中**

本文档定义为 collection 增加基于外部 LLM
的对比、综述、自定义研究和对话问答能力，并由 CLI 和 Web 以适合各自场景的方式提供入口。

LLM、上下文构建、引用、
持久化和错误恢复必须由 Core 实现，CLI 和 Web 不各自维护一套 prompt 或编排逻辑。

## 产品目标

用户可以针对一个 collection：

- 生成跨论文的文献综述。
- 对比方法、数据集、实验设计、结果和局限。
- 分析研究空白和可能的后续方向。
- 提供自定义 prompt，生成其他有来源约束的研究报告。
- 创建持续对话，并要求 LLM 根据 collection 内容回答问题。
- 从报告或回答中的引用跳转到对应 paper 和证据。

这些能力应同时适用于脚本化批处理和交互式研究，但不要求 CLI 与 Web 的 UI 完全一致。

## 入口职责

### CLI

CLI 面向自动化和一次性调用：

```bash
passagen collection review <collection-id>
passagen collection compare <collection-id>
passagen collection research <collection-id> --prompt-file <path>
passagen collection ask <collection-id> <question>
passagen collection reports <collection-id>
passagen report show <report-id>
```

CLI 同步等待 Core operation，显示结构化进度和 token 汇总，并支持将 Markdown/JSON 结果
重定向或导出。首版不要求实现持续的交互式终端聊天界面。

### Web

Web 面向阅读、整理和交互探索，在 collection workspace 中增加：

```text
Papers | Reports | Explore
```

- `Reports` 创建 review、comparison、research gaps 或 custom report，查看状态和历史版本。
- `Explore` 创建 conversation、保留消息历史、展示引用并跳转论文。
- Paper 和 collection 页面显示相关任务状态及失败原因。

Web 不暴露一组普通用户必须依次点击的 metadata、parse、summarize、outline 按钮。论文
处理入口使用 `Process/Continue`，高级操作才允许选择 `Reprocess from...`。

## Core 服务边界

建议增加两个明确的应用能力，而不是一个包含所有 LLM 行为的泛化接口：

```python
CollectionResearchService
ConversationService
```

示例 contract：

```python
research.create_report(
    collection_id=collection_id,
    kind=ReportKind.REVIEW,
    prompt=user_prompt,
)

conversation = conversations.create(collection_id)
answer = conversations.ask(conversation.id, question)
```

两个 service 共同依赖 Core 的 collection context builder、retrieval contract、LLM provider、
run recorder 和 diagnostic artifact store。它们不依赖 Typer、FastAPI 或 React。

## Collection 快照

每次 report 和 conversation 回答都必须基于明确的输入快照，不能只记录一个可变的
`collection_id`。快照至少包含：

- Collection ID、名称及成员顺序。
- 每个 paper ID、title 和处理状态。
- 使用的 summary/extracted artifact ID、Schema 版本和 SHA-256。
- collection 和 paper artifact 的更新时间。
- context builder、prompt 和 retrieval 版本。

快照用于：

- 判断报告是否因 collection 成员或 paper artifact 更新而过期。
- 重现一次调用实际使用的来源。
- 保证 citation 只能引用当次输入包含的 paper。
- 允许 conversation 明确选择继续使用旧快照或刷新上下文。

## Report 模型

首版支持：

| Kind | 用途 |
|---|---|
| `review` | 跨论文文献综述 |
| `comparison` | 方法、数据和实验结果对比 |
| `gaps` | 局限、矛盾和研究空白 |
| `custom` | 用户提供附加 prompt 的研究任务 |

报告记录至少包含：

```text
id
collection_id
kind
status
title
user_prompt
input_snapshot
provider/model
prompt/context version
token usage
report artifact paths
error
created/started/completed timestamps
```

报告 artifact 建议保存在：

```text
data/collections/<collection-id>/reports/<report-id>/
  input.json
  evidence.json
  report.json
  report.md
```

数据库保存索引、状态和相对路径，大型 evidence 和输出继续保存在文件中。

## Conversation 模型

对话是独立于 report 的有状态能力：

```text
conversations
  id
  collection_id
  title
  snapshot/version
  created_at
  updated_at

conversation_messages
  id
  conversation_id
  role
  content
  run_id
  model/token usage
  created_at

message_citations
  message_id
  paper_id
  artifact kind/hash
  locator
  excerpt
```

用户问题和最终回答属于产品数据；provider raw response、完整 request 和解析错误属于关联
run 的诊断 artifact。删除 conversation 时，由 Core 的统一保留策略决定是否级联删除诊断
内容，CLI 和 Web 不能采用不同规则。

## 上下文构建

禁止简单拼接 collection 内所有 `extracted.json` 后发送给 LLM。上下文随论文数量增长会
迅速超限，也会降低证据质量并造成不可控成本。

建议使用分层流程：

1. 验证 collection 非空，并确认候选 paper 已具有所需 artifact。
2. 优先从经过校验的 `summary.json` 构建每篇论文的基础表示。
3. 根据报告类型或用户问题选择相关 paper、section 和事实。
4. 需要细节时从 `extracted.json` 补充带 locator 的原文片段。
5. 对大型 collection 先生成或复用每篇 paper 的 evidence，再进行跨论文 synthesis。
6. 最终输出结构化 report/answer，并验证 citation。

首版不要求向量数据库。可以使用 title、summary 字段、关键词和 SQLite FTS5 完成检索，并
将 retrieval 定义为可替换的 Core protocol。实际数据规模证明需要后，再引入 embedding 或
vector backend。

## 引用与可信性

Report 和回答必须返回结构化 citation，而不是只生成不可验证的 Markdown。例如：

```json
{
  "answer": "The papers use different evaluation settings...",
  "citations": [
    {
      "paper_id": "paper-id",
      "artifact_kind": "extracted_json",
      "locator": "section:evaluation",
      "excerpt": "..."
    }
  ]
}
```

Core 保存前验证：

- Paper 属于当次 collection 快照。
- Artifact ID/hash 与快照一致。
- Locator 指向存在的 section、page 或结构化 summary 字段。
- Excerpt 可以在对应证据中找到或按明确规则匹配。
- 输出中使用的 citation key 均有定义。

Web 将 citation 渲染为 paper、summary 或 PDF 跳转；CLI 将其渲染为 Markdown 脚注或明确
的 paper ID/locator。模型无法提供足够依据时，应允许回答“不确定”，而不是生成无来源结论。

## 执行与 API

Collection report 通常包含多次 LLM 调用，Web 不应在普通 HTTP handler 中同步执行完整
任务。Core 创建可持久化 run，Web 后台 runner 执行并更新状态。

建议 API：

```http
POST /api/collection-runs
GET  /api/collection-runs/{run-id}
GET  /api/collection-runs/{run-id}/events
GET  /api/collections/{collection-id}/reports
GET  /api/reports/{report-id}

POST /api/conversations
GET  /api/conversations/{conversation-id}
POST /api/conversations/{conversation-id}/messages
```

创建长任务返回 `202 Accepted` 和 `run_id`。前端首版可以轮询状态，之后按需要增加 SSE。
对话回答如果只需一次短调用可以同步；一旦包含 retrieval、多轮 synthesis 或耗时不可控，
同样使用 run contract。

首版后台执行可以是单进程、单 worker 队列，不需要分布式任务系统，但任务状态必须持久化，
并拒绝同一 collection/report 的冲突执行。服务重启后 queued/running 任务必须进入明确的可恢复
或已中断状态，不能永久显示为运行中。

## 日志与诊断

本功能沿用 Core 拆分阶段建立的统一规则：

- Core 为每次 report 或 answer 创建 run，并为每次模型调用记录 call。
- Prompt、request、raw response、parsed response、citation validation error 保存到
  `data_dir/runs/<run-id>/llm/<call-id>/`。
- 数据库保存 provider、model、token、耗时、finish reason、状态和 artifact 相对路径。
- CLI 只渲染进度和汇总；Web 只提供状态、诊断查看权限和后台 runner 日志。
- HTTP access log、终端日志和 system journal 不保存完整 prompt、response、论文正文或问题。
- API key 和 Authorization header 在所有记录中禁止出现。
- Report 和 conversation 的保留、导出和删除规则由 Core 统一实现。

## 失败与恢复

- 单篇 evidence 失败时记录具体 paper 和原因，不静默跳过。
- 是否允许“部分 collection 报告”必须由调用参数明确指定，默认要求所有输入有效。
- 成功的、版本匹配的 paper evidence 可以在重试时复用。
- 最终 synthesis 或 citation 校验失败不删除已经完成的 evidence。
- Collection 在运行中被修改时，本次运行继续使用创建时快照，新结果标记其快照版本。
- Provider timeout、响应截断、Schema 错误和 citation 错误使用稳定错误类型。
- Web 和 CLI 对同一种 Core 错误可以采用不同展示，但不能改变恢复语义。

## 实施顺序

1. 定义 collection snapshot、report、citation 和 run contract。
2. 实现基于 summary 的 context builder 和分层 report synthesis。
3. 持久化 report、evidence、LLM call 和诊断 artifact。
4. 提供 CLI review/comparison/custom research 入口并验证批处理工作流。
5. 增加 Web 单 worker runner、collection run API 和 Reports 页面。
6. 定义 retrieval protocol并实现不依赖向量数据库的首版检索。
7. 增加一次性 collection ask，然后扩展为持久化 conversation 和 Explore 页面。
8. 根据真实 collection 规模、质量和成本评估是否引入 embedding、SSE 或并发 worker。

## 验收条件

- CLI 和 Web 调用同一个 Core service，生成一致的 snapshot、report Schema 和 citation。
- Collection 重名不会影响操作，所有接口使用 collection ID。
- 大型 collection 不通过简单全文拼接构建 prompt，并具有明确上下文预算。
- Report 保存输入快照、prompt/version、model、token、evidence 和引用。
- Collection 或 paper artifact 更新后，旧报告可以被判定为 stale。
- 回答中的每个 citation 都可以解析到当次快照中的 paper 和 artifact。
- Web 长任务返回 `202`，页面可以查看 queued/running/completed/failed 状态。
- 服务中断不会产生永久 running 的任务，重试可以复用已完成的中间 evidence。
- CLI 支持生成和导出报告，Web 支持查看历史报告和引用跳转。
- Conversation 保存消息历史，并能明确刷新或保留 collection 快照。
- LLM 诊断可通过 run ID 定位，普通日志和诊断 artifact 均不包含 API key。
- Core、CLI 和 Web 均具有不访问真实外部 LLM 的单元及集成测试。

## 暂缓事项

在真实使用证明需要前，不实现：

- 分布式任务队列和多节点 worker。
- 默认依赖向量数据库。
- 自主 agent、开放式工具调用或自动访问互联网。
- 自动根据模型输出修改 collection 或 paper metadata。
- 无 citation 的自由生成模式。
- 多用户协作、配额和计费系统。
