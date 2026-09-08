# Paper and Collection Research and Exploration

**状态：进行中（Phase 0 已完成：contract、评估问题集与 migration 0006；Phase 1 已完成：单篇问答
Core 垂直切片；Phase 2 已完成：单篇 Web 体验与结构化归档；Phase 3 已完成：FTS5、重复问题
检测、复用与部分覆盖、force regenerate 和 stale；Phase 4 已完成：collection synthesis、
comparison 与 CLI；Phase 5 已完成：collection conversation、两级检索、report、通用 queued
 dispatch、CLI 与 Web workspace）**

本文档定义针对单篇 paper 和一组 paper（collection）的持久化对话问答、结构化问答归档、
collection 总结与对比，以及按问题选择 summary、outline、raw context 和历史问答的能力。

这是 Core、CLI 和 Web 共用的主规划。领域模型、上下文构建、检索、引用验证、持久化、
LLM 编排和错误恢复必须由 Core 实现；CLI 和 Web 只提供适合各自场景的适配器，不能各自
维护 prompt、去重逻辑或 context routing。

## 1. 产品目标

### 1.1 单篇 paper

用户可以：

- 创建一个或多个持续对话，并永久保留消息历史。
- 针对文章的研究问题、贡献、结构、方法、实现细节、实验数据和局限提问。
- 获得带 paper、章节、页码和证据摘录的回答，并跳转到对应 Summary、Outline 或 PDF。
- 将有价值的问答提升为结构化归档，供之后搜索、复用、导出或作为新问题的上下文。
- 在同义问题已经回答且来源仍然有效时复用旧答案，而不是重复消耗 LLM。

### 1.2 Collection

用户可以：

- 生成跨论文综述、共同主题、方法对比、实验对比、矛盾、研究空白和后续方向。
- 提交自定义研究问题，生成有来源约束的结构化报告。
- 创建持续对话，并针对整个 collection 或其中部分 paper 提问。
- 从报告或回答中的引用跳转到对应 paper 和证据。
- 在 collection 成员或 paper artifact 更新后识别旧报告和旧回答已经过期。

### 1.3 不做什么

本功能不是一个可以自由访问互联网、修改 library 或自主执行开放式工具的 agent。模型只能
使用本次 operation 明确提供的本地来源，不能将论文正文中的内容当作系统指令，也不能在
缺少证据时生成无引用结论。

## 2. 已有基础与架构约束

当前系统已经具备：

- `extracted_json`：完整解析文本，`ParsedPaper.sections` 包含标题、正文和页码。
- `summary_json`：经过 Pydantic 校验的 `StructuredSummary`，包含结构化字段和 evidence pages。
- `outline_md`：从受支持 Summary 生成的层次化 Outline。
- SQLite、Alembic、paper artifact hash、统一 token budget 和本地文件 artifact store。
- 有序 collection 及 CLI、Web 共用的 `CatalogService`。
- Web 中持久化 processing run、单 worker runner 和轮询式进度展示。

实现需要遵守以下现有约束：

- `artifacts` 和 `processing_runs` 当前都强制绑定 `paper_id`；collection 和 conversation 不能
  伪装成 paper。
- 当前 LLM provider 只接受一个字符串 prompt，并固定请求 JSON object；首版可以继续输出
  结构化 JSON，但 provider contract 最终需要支持明确的 chat messages 和 response format。
- Paper 的 pipeline status 不应被 collection summary 或 conversation 改写。
- 大型正文、完整输入、raw response 和诊断内容继续保存在 data directory，数据库保存索引、
  状态、结构化产品数据和相对路径。

## 3. 核心设计原则

### 3.1 Transcript 与可复用知识分离

原始消息是用户产品数据，必须能够完整恢复对话。可搜索、可调用的问答记录则需要稳定、
独立的结构，不能依赖重新解析聊天文本。

因此分别持久化：

```text
Conversation -> Message                    原始对话
             -> QaRecord -> Citation       结构化问答与来源
```

每轮成功回答自动创建 `QaRecord`，但只有用户明确执行 archive 后才进入精选归档。未归档记录
仍可用于当前会话和重复问题检测。

### 3.2 Context routing 不是三选一

Planner 可以组合以下来源：

```text
conversation history
previous QaRecord
summary_json
outline_md
selected extracted_json sections
collection synthesis
per-paper summaries
```

例如实验结果对比通常需要 collection synthesis、每篇 summary 和少量 raw evidence；追问
“第二种方法的实现细节”则需要历史消息、目标 paper 的 summary 和 raw sections。

### 3.3 所有生成结果绑定来源快照

Conversation 本身绑定可变的 paper 或 collection，但每一轮回答和每一份 report 都绑定创建时
的不可变 source snapshot。复用答案之前必须比较 source fingerprint，不能仅比较问题文本。

### 3.4 Summary-first，按需升级到 raw

高层问题优先使用已经校验的 Summary；结构定位优先使用 Outline；只有问题需要精确数据、
实现细节、原文或 Summary 无法覆盖时才检索 raw sections。禁止默认拼接整篇或整个 collection
的全文。

### 3.5 引用是 contract，不是展示装饰

回答和报告中的事实性 claim 必须引用本次 snapshot 中存在的 artifact。Core 在保存结果前验证
citation；无法取得足够依据时允许回答“不确定”。

## 4. 领域模型与持久化

### 4.1 Conversation 和 Message

建议新增：

```text
conversations
  id
  paper_id nullable FK
  collection_id nullable FK
  title
  created_at
  updated_at
  CHECK exactly one of paper_id and collection_id is set

conversation_messages
  id
  conversation_id FK
  role: user | assistant
  content
  status: pending | completed | failed
  run_id nullable
  created_at
```

Conversation 删除时级联删除消息和结构化问答。诊断 artifact 是否同时删除由 Core 的统一保留
策略决定，CLI 和 Web 不能采用不同规则。

### 4.2 QaRecord

`QaRecord` 表示一轮完整、可检索和可复用的问答：

```text
qa_records
  id
  conversation_id FK
  question_message_id FK UNIQUE
  answer_message_id FK UNIQUE
  standalone_question
  normalized_question
  normalized_question_hash
  intent
  context_plan_json
  answer_json
  source_snapshot_json
  source_fingerprint
  prompt_version
  answer_schema_version
  archived_at nullable
  archive_title nullable
  archive_tags_json
  created_at
```

`standalone_question` 将“它的第二个实验呢？”改写为不依赖临时上下文的完整问题。
`normalized_question` 用于精确去重；`intent` 和 `context_plan_json` 用于审计、筛选和评估
planner，而不是只记录最终回答。

结构化 `answer_json` 至少包含：

```json
{
  "standalone_question": "What workload was used for the latency result?",
  "intent": "fact_lookup",
  "answer_markdown": "...",
  "claims": [
    {
      "text": "...",
      "citation_ids": ["citation-1"]
    }
  ],
  "limitations": [],
  "follow_up_questions": []
}
```

Markdown 只是展示字段，claims 和 citations 才是后续结构化调用的接口。

### 4.3 Citation

```text
qa_citations
  id
  qa_record_id FK
  paper_id FK
  artifact_kind
  artifact_id nullable
  artifact_sha256
  summary_path nullable
  section nullable
  page_start nullable
  page_end nullable
  excerpt nullable
```

Core 保存前验证：

- Paper 属于本轮 source snapshot。
- Artifact kind、ID 和 SHA-256 与 snapshot 一致。
- Summary path、section 或 page locator 指向真实内容。
- Excerpt 能在对应 evidence 中找到或按版本化规则匹配。
- Answer claims 使用的每个 citation ID 都存在。
- Citation 没有被模型伪造成当前 context 之外的来源。

同样的 citation contract 应由 report 复用，不另建一套字符串脚注格式。

### 4.4 Collection artifact 和 report

现有 paper `artifacts` 具有非空 `paper_id`，因此新增独立的 collection 表，避免引入脆弱的
多态外键：

```text
collection_artifacts
  id
  collection_id FK
  kind
  path
  version
  source_fingerprint
  sha256
  size_bytes
  created_at

collection_reports
  id
  collection_id FK
  kind: review | comparison | gaps | custom
  status
  title
  user_prompt nullable
  source_snapshot_json
  source_fingerprint
  run_id
  report_artifact_id nullable
  error nullable
  created_at
  completed_at nullable
```

Artifact 建议保存在：

```text
data/collections/<collection-id>/synthesis/
  summary.json
  summary.md
  source.json

data/collections/<collection-id>/reports/<report-id>/
  input.json
  evidence.json
  report.json
  report.md
```

### 4.5 通用 GenerationRun

现有 `processing_runs` 和 `llm_calls` 属于 paper pipeline，保留其兼容性。新增功能使用通用的：

```text
generation_runs
  id
  kind: collection_synthesis | report | answer
  paper_id nullable
  collection_id nullable
  conversation_id nullable
  qa_record_id nullable
  status: queued | running | completed | failed | interrupted
  source_snapshot_json
  error_code nullable
  error_message nullable
  created_at
  started_at nullable
  completed_at nullable

generation_llm_calls
  id
  generation_run_id FK
  stage: rewrite | route | retrieve | map | reduce | answer | repair
  provider
  model
  prompt_version
  schema_version
  input_tokens nullable
  output_tokens nullable
  finish_reason nullable
  error_message nullable
  created_at
```

不要为了减少一个表而将虚假的 paper ID 写入旧 run，也不要在 Message 中塞入多次 LLM call
的统计。

## 5. Source snapshot、fingerprint 与 stale

### 5.1 Paper snapshot

至少包含：

- Paper ID、title 和 processing status。
- `extracted_json`、`summary_json`、`outline_md` 的 artifact ID、Schema version 和 SHA-256。
- Context builder、retrieval、prompt 和 answer schema version。

### 5.2 Collection snapshot

至少包含：

- Collection ID、名称、描述和有序成员 ID。
- 每个 paper 的 title、status 和实际使用的 artifact 信息。
- Collection synthesis artifact 的 ID、version 和 SHA-256（如果使用）。
- Context builder、retrieval、prompt 和 report/answer schema version。

Fingerprint 由 canonical JSON 计算 SHA-256。collection 成员增删、排序、paper summary 重建或
raw artifact 变化后，新 fingerprint 必须不同。

### 5.3 Stale 语义

- 历史消息永不因 stale 被改写或删除。
- 旧 `QaRecord` 和 report 保留，但 API 明确返回 `stale: true` 及原因。
- 精确或语义重复问题命中 stale 记录时，默认重新回答；UI 可以同时展示旧答案。
- Collection 在 operation 执行中被修改时，本次运行继续使用创建时 snapshot。
- Conversation 可以继续使用当前 scope，也可以由高级入口固定在旧 snapshot；每轮实际选择必须
  被持久化。

## 6. Context planner 与问答流程

### 6.1 完整流程

```text
原始问题
  -> 读取有限的最近对话
  -> 改写 standalone question
  -> 精确/候选 QaRecord 检索
  -> 结构化 context planning
  -> paper/section retrieval
  -> 按 token budget 组装 context
  -> 生成结构化回答
  -> citation 和 schema validation
  -> 必要时有限 repair 或 raw-context escalation
  -> 原子保存 Message、QaRecord、Citation 和 run 状态
```

用户消息先持久化为 pending，失败后保留问题和稳定错误状态。不能因为 provider timeout 而丢失
用户已经提交的内容，也不能保存只有一半的 assistant answer。

### 6.2 Planner contract

建议 planner 返回：

```json
{
  "standalone_question": "...",
  "intent": "comparison",
  "reuse_qa_id": null,
  "sources": ["conversation", "collection_summary", "paper_summaries", "raw"],
  "paper_ids": ["paper-a", "paper-b"],
  "retrieval_queries": ["evaluation latency workload"],
  "requires_exact_quote": false,
  "answer_kind": "comparative"
}
```

Planner 输出必须经过 Core 校验：只能选择当前 scope 内 paper，只能使用存在且版本受支持的
artifact，并且所请求上下文必须适配 token budget。

### 6.3 默认路由规则

| 问题类型 | 首选 context |
|---|---|
| 问题、贡献、目标、局限和总体结论 | `summary` |
| 文章组织、章节位置和论证顺序 | `outline` |
| 精确数值、实验条件、实现细节和原文 | `raw` |
| 单篇综合解释 | `summary + selected raw` |
| Collection 共同主题和高层差异 | `collection summary` |
| 多篇详细比较 | `collection summary + per-paper summaries + selected raw` |
| 依赖“刚才”“第二种方法”等指代的追问 | `recent history + resolved sources` |
| 来源未变化的等价旧问题 | `previous QaRecord` |

规则用于提供确定性约束和默认值，LLM planner 负责问题改写和复杂组合。不能完全依靠关键词，
也不能允许模型绕过 scope 和 budget 校验。

### 6.4 Context 组装与升级

优先级通常为：

```text
question and instructions
recent relevant history
reused QaRecord candidates
collection synthesis or paper summary
outline fragments
ranked raw sections
```

Builder 为每个片段分配稳定 source key 和 token 配额，保留引用 locator。回答 validator 判定
summary/outline 不足且 planner 尚未使用 raw 时，可以在预算允许范围内执行一次 raw escalation；
禁止无界自动重试。

论文文本是非可信数据。Provider 支持 chat roles 后，系统约束、用户问题和 source context 必须
分别传递；在此之前，prompt 也必须使用明确 delimiter，并声明 source 中的指令不可执行。

## 7. 历史问题检测与复用

重复问题检测分三层：

1. 使用 `normalized_question_hash + scope` 精确命中。
2. 从同 scope、近期会话和 archive 中检索候选问题。
3. 必要时让 LLM 对少量候选执行语义等价判断。

只有以下条件全部满足才直接复用完整答案：

```text
scope compatible
source_fingerprint equal
answer_schema_version supported
prompt/retrieval semantics compatible
citation validation still passes
```

如果答案只部分覆盖当前问题，可以把旧 `QaRecord` 作为 context，而不是伪装成 cache hit。
如果来源已变化，则返回 stale candidate 并重新生成。

首版不默认引入 embedding 或 vector database。可以从 exact hash、archive metadata、近期记录和
SQLite FTS5 候选开始。中英文问题与英文论文之间的 raw retrieval 由 planner 生成英文检索词；
真实数据证明 lexical retrieval 不足后，再为 retrieval protocol 增加 embedding backend。

## 8. Raw section 检索

从 `extracted_json` 物化可定位的 section/chunk 索引：

```text
paper_sections
  id
  paper_id FK
  ordinal
  title nullable
  text
  pages_json
  extracted_artifact_id
  extracted_artifact_sha256

paper_sections_fts
  title
  text
```

索引必须能够在 extracted artifact 更新后重建，并删除旧版本 section。检索结果携带 paper、
section、pages、source hash 和 rank，不能只返回无来源字符串。

Collection 检索采用两级过程：

1. 使用 collection synthesis、paper title 和 per-paper summary 选择相关 papers。
2. 只在候选 papers 中检索 raw sections，并按 paper 多样性、相关度和 token budget 裁剪。

禁止把 collection 内所有 `extracted_json` 简单拼接后发送给模型。

## 9. Collection synthesis 与 report

### 9.1 Collection synthesis schema

Collection synthesis 是可复用的基础 artifact，不等同于某次聊天回答。建议包含：

```text
identity and source_manifest
overview
common_themes
comparison_dimensions
paper_roles
agreements
disagreements
complementary_contributions
methodology_differences
evaluation_comparison
gaps_and_open_questions
claims and citations
```

Comparison matrix 中每个 cell 都应保存结构化值和 citation IDs，不能只渲染一张无法追踪来源
的 Markdown 表格。

### 9.2 生成策略

1. 验证 collection 非空并创建 snapshot。
2. 默认要求所有 paper 具有受支持且有效的 `summary_json`。
3. 将每篇 `StructuredSummary` 转换为紧凑、带来源的 paper representation。
4. 小 collection 在预算内直接 synthesis；大 collection 分批 map/reduce。
5. 对模型提出的跨论文 claims 执行 citation validation。
6. 原子保存 JSON、Markdown、source manifest 和数据库索引。

默认不静默跳过缺少 Summary 的 paper。高级调用可以显式设置 partial mode，但结果必须列出未覆盖
paper，并且 UI 和 CLI 显著标记覆盖范围。

### 9.3 Report kinds

| Kind | 用途 |
|---|---|
| `review` | 跨论文文献综述 |
| `comparison` | 方法、数据、实验设计和结果对比 |
| `gaps` | 局限、矛盾和研究空白 |
| `custom` | 用户提供附加 prompt 的来源约束研究任务 |

Report 可以复用版本匹配的 collection synthesis 和 paper evidence，但仍保存自己的 snapshot、
prompt version、run、结构化输出和 citations。

## 10. Core 服务与 Provider contract

建议增加三个清晰的应用服务：

```python
ConversationService
CollectionSynthesisService
CollectionResearchService
```

示例：

```python
conversation = conversations.create(paper_id=paper_id)
turn = conversations.ask(conversation.id, question)

synthesis = collections.synthesize(collection_id)
report = research.create_report(
    collection_id=collection_id,
    kind=ReportKind.COMPARISON,
)
```

共同依赖：

```text
SourceSnapshotBuilder
ContextPlanner
RetrievalProtocol
ContextBuilder
CitationValidator
GenerationRunRepository
DiagnosticArtifactStore
LlmProvider
```

建议的 Core 包边界：

```text
passagen/assistant/
  models.py
  schemas.py
  service.py
  planner.py
  retrieval.py
  context.py
  citations.py
  repository.py

passagen/research/
  schemas.py
  synthesis.py
  reports.py
```

Provider contract 最终扩展为：

```python
generate(
    messages: Sequence[ChatMessage],
    *,
    max_tokens: int,
    response_format: ResponseFormat,
) -> LlmResponse
```

首个垂直切片可以通过兼容 adapter 将 messages 渲染为单 prompt，以控制改动范围；但业务 service
不能依赖 OpenAI-compatible HTTP payload，也不能永久假设所有调用都返回同一种 JSON 格式。

## 11. CLI 与 Web 入口

### 11.1 CLI

CLI 面向自动化和一次性调用：

```bash
passagen paper ask <paper-id> <question>
passagen collection synthesize <collection-id>
passagen collection review <collection-id>
passagen collection compare <collection-id>
passagen collection research <collection-id> --prompt-file <path>
passagen collection ask <collection-id> <question>
passagen conversation show <conversation-id>
passagen question archive <qa-record-id>
passagen question search <query>
```

CLI 同步等待 Core operation，显示结构化进度和 token 汇总，并支持将 Markdown/JSON 重定向或
导出。首版不要求持续的交互式终端聊天界面。

### 11.2 Web

Paper 页面增加 Ask 入口：

- 桌面端优先使用可与 Summary、Outline 或 PDF 并排查看的对话面板。
- 移动端使用独立 tab/route。
- 支持 conversation 切换、创建、重命名和删除。
- Citation 点击后跳转现有 Summary evidence 或 PDF page route。
- 每条回答展示本轮实际使用的 Summary、Outline、Raw sections 或 Previous answer。

Collection workspace 增加：

```text
Papers | Synthesis | Reports | Ask
```

- `Synthesis` 展示总体总结、comparison matrix、分歧和研究空白。
- `Reports` 创建和查看 review、comparison、gaps 与 custom report。
- `Ask` 提供持久化 collection conversation 和 citation navigation。
- Stale、partial coverage、queued/running/failed 状态必须显式展示。

## 12. API 与异步执行

建议 API：

```http
POST   /api/conversations
GET    /api/conversations?paper_id=...
GET    /api/conversations?collection_id=...
GET    /api/conversations/{conversation-id}
PATCH  /api/conversations/{conversation-id}
DELETE /api/conversations/{conversation-id}

POST   /api/conversations/{conversation-id}/turns
GET    /api/conversations/{conversation-id}/turns/{turn-id}

GET    /api/qa-records?q=...&archived=true
PATCH  /api/qa-records/{qa-record-id}

GET    /api/collections/{collection-id}/synthesis
POST   /api/collections/{collection-id}/synthesis-runs
GET    /api/collections/{collection-id}/reports
POST   /api/collections/{collection-id}/report-runs
GET    /api/generation-runs/{run-id}
```

问答可能包含 rewrite、route、retrieval、answer 和 repair，不能假定一次调用会在普通 HTTP 超时
内完成。`POST turns` 和 collection 长任务返回 `202 Accepted`，Web 使用乐观显示用户消息并
轮询 run/turn 状态。Provider 和产品需求稳定后再增加 SSE token streaming。

首版后台执行沿用单进程、单 worker 思路，不需要分布式队列。服务启动时将遗留 running run
标记为 interrupted，queued run 是否恢复必须采用明确规则，不能永久显示为运行中。

## 13. 日志、隐私与诊断

- Core 为每次 answer、synthesis 或 report 创建 generation run，并记录每次模型调用。
- Prompt、request、raw response、parsed response 和 validation error 保存到
  `data_dir/runs/<run-id>/llm/<call-id>/`。
- 数据库保存 provider、model、token、耗时、finish reason、状态和 artifact 相对路径。
- HTTP access log、终端日志和 system journal 不保存完整问题、回答、prompt 或论文正文。
- API key、Authorization header 和环境变量值在所有记录和测试 artifact 中禁止出现。
- 对话、归档、报告和诊断数据的导出、保留和删除规则由 Core 统一实现。
- Source content 必须作为非可信数据隔离，防止论文中的 prompt injection 改变系统行为。

## 14. 失败与恢复

- User message 在调用 provider 前落库；失败时保存稳定错误和可重试状态。
- Answer、QaRecord 和 Citation 在同一事务中完成，禁止只保存部分结构化答案。
- Provider timeout、截断、Schema 错误、retrieval 错误和 citation 错误使用稳定错误类型。
- 有限 repair 和 raw escalation 必须记录为独立 LLM call，不覆盖第一次失败诊断。
- 单篇 evidence 失败时记录具体 paper 和原因，不静默跳过。
- 是否允许 partial collection 必须由参数明确指定，默认要求全部输入有效。
- 成功且 fingerprint 匹配的中间 evidence 可以在重试时复用。
- Collection 在运行中被修改时继续使用创建时 snapshot，新结果立即按当前 collection 判定 stale。
- 删除 paper 或 collection 时，conversation/report 的级联和 artifact 清理必须有数据库测试。

## 15. 开发方案

开发采用可独立验收的纵向阶段。每个阶段先完成 Core contract 和 migration，再增加 adapter；
不得先在 React 或 FastAPI 中实现临时业务逻辑。

### 15.1 仓库职责与依赖关系

| 仓库 | 负责内容 | 不负责内容 |
|---|---|---|
| `passagen-core` | Schema、migration、repository、snapshot、planner、retrieval、citation、service、run 和 diagnostics | HTTP、Typer、React 展示 |
| `passagen-web` | FastAPI adapter、后台 runner 接入、React Query 状态和浏览器 workflow | Prompt、检索排序、cache/stale 判定 |
| `passagen-cli` | Typer adapter、同步进度、退出码、Markdown/JSON 输出 | 独立对话引擎、独立 report Schema |

关键依赖链为：

```text
Phase 0 contracts/migration
  -> Phase 1 single-paper Core
  -> Phase 2 Web vertical slice
  -> Phase 3 retrieval/reuse/stale
  -> Phase 4 collection synthesis + CLI
  -> Phase 5 collection conversation/report + Web
  -> Phase 6 evidence-driven enhancements
```

每个 Phase 应拆成可审查的小批次：先提交 Schema/migration 和 repository 测试，再提交纯 Core
service，最后提交 CLI/Web adapter。跨仓库工作不能依赖尚未发布的隐式实现；Core contract 合并
并发布兼容版本后，CLI/Web 再更新相邻依赖和 lockfile。

### 15.2 Phase 0：Contract、评估样本与 migration 设计

交付：

- 固化 Conversation、Message、QaRecord、Citation、SourceSnapshot、ContextPlan 和 Answer Schema。
- 准备覆盖 summary、outline、raw、组合 context、追问和重复问题的本地评估问题集。
- 设计 Alembic migration、级联删除和 schema version 升级路径。
- 定义稳定错误、run 状态和 prompt/schema/retrieval version 常量。

验收门槛：

- Schema 可以 round-trip，非法 scope、citation 和 context plan 会被拒绝。
- Migration upgrade/downgrade、旧 library upgrade 和 foreign-key integrity 有测试。
- 评估集不访问真实外部 LLM，并明确每个问题期望使用的 context 类型。

### 15.3 Phase 1：单篇问答 Core 垂直切片

交付：

- Conversation repository、message lifecycle 和 generation run。
- 单篇 source snapshot 和 fingerprint。
- 确定性初版 planner：summary、outline、raw 和 history 组合。
- 从 `extracted_json` 读取并排序相关 sections 的首版 retrieval。
- 结构化回答、citation validation、有限 repair 和持久化 `QaRecord`。
- Provider adapter 和问答相关 token/call 统计。

首版可以先在内存中对单篇 sections 排序，不必阻塞于完整 FTS migration；retrieval protocol
必须从第一天存在，以便 Phase 3 替换实现。

验收门槛：

- 高层、结构和细节问题分别选择预期 context。
- Follow-up 可以被改写为独立问题。
- 回答中的所有 citation 能解析到当前 paper artifact 和页码/章节。
- Provider 失败后问题仍可见且可重试，不产生半条 assistant answer。
- 不把整篇 raw text 默认发送给模型。

### 15.4 Phase 2：单篇 Web 体验与结构化归档

交付：

- Conversation 和 turn API，`202` run contract 及轮询。
- Paper Ask 面板、conversation 管理、pending/failed/retry 状态。
- Citation 到 Summary、Outline 和 PDF 的导航。
- QaRecord archive/unarchive、标题、标签、搜索和结构化 JSON 导出。
- 回答来源标识和 token/诊断摘要。

验收门槛：

- 刷新页面或重启服务后会话完整保留。
- 桌面和移动端均能提问、查看失败并重试。
- 用户可以归档一轮问答，并在独立搜索中再次找到和调用。
- UI 不将 archived、cached 和 newly generated answer 混为同一状态。

### 15.5 Phase 3：重复问题检测、FTS 与 stale

交付：

- Question normalization 和 exact hash cache。
- QaRecord 候选检索及有限的 LLM 语义等价判断。
- `paper_sections` 物化、SQLite FTS5 和 extracted artifact 更新后的重建。
- Source fingerprint 比较、stale reason 和 API/UI 展示。
- 复用、部分覆盖和强制重新生成策略。

验收门槛：

- 相同问题和来源直接复用，不调用回答模型。
- 同义问题只在高置信且版本匹配时复用。
- Summary 或 extracted artifact 重建后旧答案不会被静默当作新答案。
- 中英文问题可以通过 planner 生成的检索词定位英文 raw section。

### 15.6 Phase 4：Collection synthesis 与 comparison

状态：已完成。Core 的 snapshot/fingerprint、schema version 9、结构化 synthesis、
summary-only direct 与 bounded map/reduce、citation validation、partial coverage、stale/reuse/force、
generation accounting 和确定性 artifact renderer，以及 CLI `collection synthesize/compare` 已交付。

交付：

- Collection snapshot、fingerprint、artifact 和 generation run。
- Collection synthesis Schema、small-context 和 map/reduce 策略。
- Comparison matrix、claims、citations、partial coverage 和 stale 检测。
- Core service、CLI `collection synthesize/compare` 及 JSON/Markdown 输出。

验收门槛：

- Collection 成员、顺序或输入 summary hash 变化会使旧 synthesis stale。
- 默认不会忽略缺少 Summary 的 paper。
- 大 collection 不全文拼接，并遵守明确 token budget。
- Comparison 中每项事实都能定位到 paper 和来源 artifact。

### 15.7 Phase 5：Collection conversation、report 与 Web workspace

状态：Core 已完成。Collection conversation（exactly-one scope、snapshot/fingerprint、scope 内
QA 复用与 stale）、两级 paper/section 检索（记录实际选择的 papers）、多 paper citation
校验、review/comparison/gaps/custom report（版本化 schema、持久化、synthesis 复用、
partial/stale、bounded repair、JSON/Markdown 与 citation 导航元数据），以及通用 queued
generation run dispatch（answer/synthesis/report 一致 claim/execute，启动中断不留永久
running 产品）已在 Core 交付并通过 fake provider 测试。CLI `collection ask/review/research`
与 Web `Synthesis | Reports | Ask` workspace 也已交付，均复用上述 Core service，不另建
prompt、检索或 stale 逻辑。

交付：

- Collection 的两级 paper/section retrieval 和 context planning。
- Review、comparison、gaps 和 custom report。
- CLI 一次性 `collection ask/review/research`。
- Web `Synthesis | Reports | Ask`，历史 run、report 和 conversation 浏览。
- Collection citation 到具体 paper/PDF 的导航。

验收门槛：

- Collection 问答可以只选择相关 papers，并记录实际选择。
- 跨 paper 回答不产生 scope 外 citation。
- Web 长任务返回 `202`，重启后不会遗留永久 running 状态。
- CLI 和 Web 调用相同 Core service，输出相同 Schema 和 stale 语义。

### 15.8 Phase 6：质量与规模增强

根据真实使用数据决定是否交付：

- Provider 原生多 message 与 token streaming、Web SSE。
- 可选 embedding retrieval backend。
- 更细粒度 reranking、answer groundedness 和 citation coverage 评估。
- 大 collection evidence cache 和受控并发 worker。
- Conversation/report archive bundle 的导入导出。

进入条件是已有 lexical retrieval、质量评估或延迟数据证明增强确有必要，而不是预先增加运行
依赖和部署复杂度。

### 15.9 关键风险与控制措施

| 风险 | 控制措施 |
|---|---|
| Planner 选择错误 context | 确定性规则约束、结构化输出、离线评估集和一次受限 raw escalation |
| 回答有 citation 但不支持 claim | Claim-to-citation 校验、excerpt/locator 验证和允许“不确定” |
| Collection prompt 超预算 | Summary-first、两级检索、map/reduce 和每阶段 token 上限 |
| 旧答案被错误复用 | Scope、fingerprint、Schema 和 prompt/retrieval version 联合校验 |
| 中英文检索召回不足 | Planner 生成英文查询；以评估数据决定是否增加 embedding |
| 长任务中断或部分落库 | 持久化 run、事务性产品数据、interrupted recovery 和幂等重试 |
| Paper 内容触发 prompt injection | Source 与指令分层、delimiter、scope allowlist 和禁止 source tool execution |
| 新表破坏已有 library | Alembic upgrade 测试、foreign-key check、备份流程和不改旧 pipeline 语义 |
| Core/CLI/Web 行为漂移 | 单一 Core service、版本化 Schema、contract tests 和固定 fake provider fixtures |

## 16. 测试策略

### Core

- Repository CRUD、事务、级联、并发冲突和 migration。
- Snapshot canonicalization、fingerprint 和 stale 判定。
- Planner Schema、scope/budget validation 和 context 组合。
- Raw retrieval ranking、page locator 和 artifact 重建。
- Answer/report Schema、citation validation、repair 和失败原子性。
- Fake provider 驱动的完整单篇及 collection service 测试。

### CLI

- Fake Core/provider 下的命令参数、进度、退出码和 Markdown/JSON 输出。
- Partial、stale、provider unavailable 和 citation failure 的稳定错误展示。
- 不访问真实外部 API 的端到端 library workflow。

### Web backend

- Conversation、turn、archive、synthesis、report 和 run API contract。
- `202`、polling、interrupted recovery、origin protection 和错误映射。
- 删除 scope 后的级联行为及无 API key/正文日志验证。

### Web frontend

- Conversation 切换、乐观 user message、pending/failed/retry。
- Citation navigation、archive 搜索、source badges、stale 和 partial 状态。
- Paper 与 collection workflow 的 desktop/mobile integration test。
- Packaged frontend build 后的关键 E2E，而不只测试 Vite source。

## 17. 总体验收条件

- Paper 和 collection 均支持多个持久化 conversation。
- 每轮成功回答都有结构化 QaRecord、source snapshot、context plan 和可验证 citations。
- 用户可以归档、搜索、导出并再次调用结构化问答。
- Planner 能组合 Summary、Outline、Raw、History 和 Previous QaRecord。
- 相同问题只在 scope、fingerprint 和 Schema 兼容时直接复用。
- Collection summary/comparison 覆盖范围明确，不静默遗漏 paper。
- 大型输入不通过简单全文拼接构建 prompt，并始终遵守 token budget。
- Paper/collection 内容变化后旧 answer、synthesis 和 report 可以可靠判定 stale。
- CLI 和 Web 共用 Core service、Schema、错误和恢复语义。
- 所有长任务均有持久化状态，服务中断不会产生永久 running run。
- 普通日志不记录论文正文、完整问题/回答或 API key，诊断可通过 run ID 定位。
- Core、CLI 和 Web 测试不需要访问真实外部 LLM。

## 18. 暂缓事项

在真实使用证明需要前，不实现：

- 分布式任务队列和多节点 worker。
- 默认依赖 embedding 或 vector database。
- 自主 agent、开放式工具调用或自动访问互联网。
- 自动根据模型输出修改 collection、paper metadata、note 或 tag。
- 无 citation 的自由生成模式。
- 多用户协作、远端同步、配额和计费系统。
