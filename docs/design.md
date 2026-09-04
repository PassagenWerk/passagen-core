# Passagen 设计方案

Passagen Core 是本地 paper library 的共享业务实现，负责管理 PDF、元数据、处理状态和
artifact，并调用外部服务生成结构化英文摘要与 outline。CLI 和 Web 是 Core 的独立入口。

## 设计目标

- 增量扫描新加入的 PDF，已经处理过的论文不重复处理。
- 导入 PDF 后立即建立受 Passagen 管理的副本，后续处理不依赖源文件。
- 提取并补全论文元数据，统一管理 PDF 和生成产物。
- 生成经过 Schema 校验的英文结构化摘要。
- 仅以结构化摘要为输入生成英文 outline，避免两份结果相互矛盾。
- 记录各处理阶段的状态，支持失败重试和断点续跑。

初始版本只提供 CLI；当前已经提供独立 Web 适配器。Core 仍不负责多用户管理、向量检索
或自动下载论文。

## 处理流程

```text
扫描 PDF
  -> 计算 SHA-256
  -> 去重并将 PDF 导入受管理存储
  -> 轻量读取 PDF metadata 和前几页，提取候选标识
  -> 本地身份信息不足时可选调用 GROBID header extraction
  -> 使用 DOI 查询 Crossref，使用 arXiv ID 查询 arXiv API
  -> 补全论文记录
  -> 全文结构解析
  -> 按章节切分正文
  -> LLM 生成结构化摘要
  -> Schema 校验和有限修复
  -> 基于结构化摘要生成英文 outline
  -> 归档生成结果
```

处理状态至少包括：

```text
discovered -> metadata_resolved -> parsed -> summarized -> outlined
```

每一步成功后持久化状态。重新执行时从最后一个成功阶段继续，而不是重新调用全部外部服务。

## Application Entry Points

Core 提供扫描、单阶段处理、幂等 update、Catalog 查询与 library 管理能力。CLI 将这些能力
映射为命令，Web 将其映射为 HTTP 和浏览器操作；Core 不包含 Typer 或 FastAPI contract。

`update` 指定 `paper_id` 时只推进该 Paper，省略时推进所有落后于当前开发前沿的 Paper，
已达到目标状态的记录直接跳过；强制模式从 metadata 开始重建。批量中单篇失败不阻断
其他记录。当前前沿是 `outlined`。

扫描和处理保持为独立 Core operation，适配器可以组合它们形成完整工作流。

阶段失败时 Paper 保持在最后一个成功状态，不进入单独的失败状态。调用者修复问题后手动再次执行 `update`，程序从该状态的下一阶段继续；`--force` 会先使下游结果失效，并从 metadata 开始完整重建。程序不会在一次执行内自动重试外部请求。

## Logging And Diagnostics

Core 使用标准 `logging` 发送结构化事件，但不安装 handler 或决定终端、文件和 HTTP 展示。
CLI 与 Web 分别负责宿主运行日志和进度呈现。

LLM call、prompt、raw response、parsed response 和 validation error 是可共享的业务诊断，
由 Core run/diagnostic contract 管理，不属于普通终端或 access log。过渡期 paper pipeline
仍可接收调用方提供的诊断目录；目标布局和保留规则见 [`operations.md`](operations.md)。

任何日志和诊断 artifact 都不得保存 API key、Authorization header 或完整环境变量。

## 论文标识和去重

每篇论文同时保存以下可用标识：

- DOI：标准化为小写，并移除 URL 前缀和 `doi:` 前缀。
- arXiv ID：移除 `arXiv:` 前缀，保留版本信息之外的规范 ID。
- PDF SHA-256：根据原始文件内容计算。

内部 `paper_id` 由数据库生成且创建后不可变。DOI、arXiv ID 和 SHA-256 分别建立唯一索引，满足以下任一条件即视为已有论文：

- DOI 相同；
- arXiv ID 相同；
- SHA-256 相同。

标题只用于辅助查找和人工检查，不单独作为自动去重依据。首版按一篇 Paper 对应一个受管理 PDF 处理，不实现同一论文多版本合并；后续补全 DOI 或 arXiv ID 不改变 `paper_id` 和 PDF 的内容寻址路径。

## 元数据

### 字段

至少保存：

- `title`
- `authors`
- `year`
- `venue`
- `doi`
- `arxiv_id`
- `source_url`
- `original_filename`
- `pdf_sha256`

`original_filename` 只用于展示和审计，不参与后续文件读取。数据库不保存扫描目录中的源路径。

### GROBID、Crossref 与 arXiv

优先从 PDF 中识别 DOI 或 arXiv ID，再按标识类型精确查询元数据：

- DOI 使用 Crossref REST API。
- arXiv ID 使用 arXiv API。
- 同时具有 DOI 和 arXiv ID 时可以查询两者，Crossref 用于已发表版本的 venue、year 和 DOI 元数据，arXiv 用于预印本标识和版本信息。
- 可选 GROBID fallback 默认关闭；启用后，本地缺少 title、authors 或 DOI/arXiv ID 时调用 `processHeaderDocument`，并使用其 TEI header 结果补充身份信息。
- Crossref 标题与当前 PDF/GROBID 标题冲突时，GROBID 可作为第二次校验；若 GROBID 给出不同 DOI，则使用新 DOI 重新执行 Crossref 精确查询。
- 低置信度 fallback 只填补本地缺失字段，不覆盖已经存在的本地字段；当 GROBID 标题与本地标题明显不一致时拒绝整份 GROBID 结果。Crossref 冲突仲裁时，只有 GROBID 标题与 Crossref 标题一致，才允许使用 GROBID 修正论文身份。
- 没有可靠标识且 GROBID 未启用或未提取到标识时，只使用已有本地结果，不根据模糊标题自动查询论文。

本地书目信息采用分层提取：先使用可信的 PDF metadata/XMP；title 缺失或明显为生成器占位值时，从前几页的字体大小和坐标选择标题块，并用原文件名候选校验；author metadata 缺失时，从标题下方的姓名块提取并过滤机构、URL 和脚注标记。venue 与非 DOI/arXiv source URL 可以从出版方封面文字补充。全文结构和章节边界仍由 M4 parser 负责。

字段合并优先级为 `user > crossref > arxiv > grobid > pdf`。每个字段记录实际来源 `user`、`crossref`、`arxiv`、`grobid` 或 `pdf`，不能只记录整条论文的单一来源。

GROBID、Crossref 或 arXiv 请求失败、限流、未命中或返回无效内容时，保留已提取的元数据并继续处理。外部补全是 best-effort 能力，不是摘要流水线成功的前置条件。

`metadata_resolved` 表示本地元数据已经标准化并完成可用的外部补全尝试，不表示 GROBID、Crossref 或 arXiv 请求必须成功。

## PDF 导入与托管

`scan` 接受用户目录中的 PDF 作为一次性导入源。计算 SHA-256 并完成去重后，程序将文件复制到相对 `data_dir` 的内容寻址路径：

```text
pdfs/<sha256 前两位>/<sha256>.pdf
```

导入完成后的规则：

- 数据库通过 `artifacts(kind="original_pdf")` 保存相对 `data_dir` 的路径，不保存源文件绝对路径。
- 解析、重试、重新生成和状态查询只使用受管理副本。
- 用户可以移动或删除扫描目录中的源文件，不影响已导入论文。
- 相同 SHA-256 复用同一个受管理文件，不重复复制。
- `original_filename` 可以保留为审计元数据，但不是文件定位依据。

复制先写入 `data_dir` 内的临时文件，校验实际写入内容的 SHA-256 后再原子重命名到最终路径。数据库记录失败时清理本次创建且尚未被引用的文件；最终路径已存在时校验后复用。

扫描阶段执行轻量完整性检查：文件前 1024 bytes 必须包含 `%PDF-` header，末尾 1024 bytes 必须包含 `%%EOF` trailer。该检查用于拒绝明显伪装或截断的文件，不替代 PDF parser 在下一阶段进行的结构校验。

## PDF 解析

### 工具比较

| 维度 | PyMuPDF | GROBID |
| --- | --- | --- |
| 部署 | Python 依赖，简单 | Java 服务，通常通过 Docker 运行 |
| 文本提取 | 快速，但偏底层 | 面向学术论文的结构化解析 |
| 章节识别 | 需要自行实现 | 可输出标题、章节、引用和参考文献结构 |
| 双栏和阅读顺序 | 需要额外处理 | 通常优于通用 PDF 提取器 |
| 页码和坐标 | 支持良好 | 可配置坐标输出，但处理更复杂 |
| 适用场景 | 轻量提取和降级处理 | 论文语义分段与元数据提取 |

### 选择

默认使用 GROBID 的 `processFulltextDocument` 接口，将 TEI XML 转换为内部统一的章节结构。结构化章节更适合后续按 Introduction、Method、Evaluation 等语义进行分块总结。

PyMuPDF 作为 `--parser pymupdf` 轻量模式，并在 GROBID 不可用或解析失败时作为降级后端。降级结果必须标记 `parser=pymupdf`，因为其章节边界和阅读顺序的可靠性较低。

内部解析结果统一为：

```yaml
metadata: {}
sections:
  - title: string | null
    text: string
    pages: [integer]
references: []
parser: grobid | pymupdf
```

首版只保证处理包含文本层的 PDF。扫描版 PDF 的 OCR 不在首版范围内，检测到无有效文本时应明确失败并给出原因。

## 结构化摘要

结构化摘要使用英文。以下 YAML 用于展示字段结构；程序内部以 Pydantic 模型和 JSON Schema 为准，规范结果先保存为 JSON，再按需导出 YAML。

所有非必填字段均允许 `null`。当论文没有报告相关内容时必须输出 `null`，不得根据常识补写。列表字段可为空列表。

```yaml
schema_version: "2"

identity:
  title: string
  authors: [string]
  year: integer | null
  venue: string | null
  doi: string | null
  arxiv_id: string | null

classification:
  paper_type: string | null
  topics: [string]
  keywords: [string]

problem:
  context: string | null
  problem_statement: string | null
  motivation: string | null
  goals: [string]
  non_goals: [string]
  assumptions: [string]
  prior_work_limitations: [string]

contributions:
  - category: string | null
    statement: string
    evidence_pages: [integer]

design:
  overview: string | null
  components:
    - name: string
      role: string | null
      details: [string]
      interactions: [string]
      evidence_pages: [integer]
  processes:
    - name: string
      description: string | null
      steps: [string]
      evidence_pages: [integer]
  key_mechanisms: [string]
  design_decisions: [string]
  tradeoffs: [string]

implementation:
  prototype_scope: string | null
  implemented_components: [string]
  languages: [string]
  frameworks_and_dependencies: [string]
  hardware_platforms: [string]
  software_platforms: [string]
  code_size: string | null
  deployment_model: string | null
  engineering_details: [string]

evaluation:
  research_questions: [string]
  environment:
    hardware: [string]
    software: [string]
    topology_or_scale: string | null
    configuration: [string]
  baselines: [string]
  datasets: [string]
  workloads: [string]
  metrics: [string]
  methodology: [string]
  results:
    - research_question: string | null
      metric: string
      metric_direction: higher_is_better | lower_is_better | neutral | unknown
      subject: string
      subject_value: string | null
      baseline: string | null
      baseline_value: string | null
      improvement: string | null
      conditions: [string]
      evidence_pages: [integer]
  ablations: [] # 与 results 使用相同结构

discussion:
  limitations: [string]
  tradeoffs: [string]
  threats_to_validity: [string]
  applicability: [string]
  future_work: [string]
  conclusions: [string]
  reusable_methods: [string]

related_work:
  groups:
    - area: string
      representative_works: [string]
      relationship: string | null
      distinction: string | null
      evidence_pages: [integer]
```

`identity` 保存在 Summary artifact 中，但最终值由数据库中的 PaperRecord 覆盖，LLM 不具有修改 title、authors、year、venue、DOI 或 arXiv ID 的权限。Schema v2 是不兼容变更，v1 Summary 和基于它生成的 Outline 必须使用 `update <paper-id> --force` 重建。

Summary 生成支持 `auto`、`full` 和 `hierarchical` 三种策略。请求发送前用全局 LLM 预算
（context window × 利用率 − 安全余量 − 输出预留）评估完整全文 prompt：可容纳时直接根据
全文序列化生成结构化摘要；超出预算时按章节、段落和句子边界语义切块，为每块提取带类别、
章节、页码和定量归属的结构化 evidence，合并去重后再生成最终摘要。evidence 总量超过最终
预算时按 Summary 领域分组中间归并，不做尾部截断。中间结果应保留，失败重试时不重复处理
成功分块。

## Schema 校验和修复

LLM 必须使用支持结构化输出的调用方式；无论供应商是否声称保证 JSON 格式，返回值都要经过本地 Pydantic 校验。

处理顺序如下：

1. 解析模型返回的 JSON。
2. 使用 Schema 校验字段、类型和必填项。
3. 对可确定的格式问题执行本地修复，例如移除 Markdown code fence、将缺失的可空字段补为 `null`。
4. 仍不合法时，将校验错误和原始结果交给模型修复，最多重试两次。
5. 仍然失败则保存原始响应和错误信息，Paper 保持在 `parsed`。

本地修复不得改写摘要语义，也不得自动生成论文中不存在的内容。

Schema 具有独立版本号。Schema 或 prompt 更新后，可以显式重新生成旧论文，而不将旧产物误认为当前版本结果。

## 英文 Outline

英文 outline 只能在结构化摘要通过校验后生成。输入为规范化后的结构化摘要，不再次直接读取 PDF，也不独立总结全文。

outline 使用论文式组织方式，包含：

- Introduction
- Background
- Design
- Implementation
- Evaluation
- Related Work

Outline 使用 section thesis、named point、supporting details 和 evidence pages 的两级结构。结构化摘要中的对应内容为 `null` 或空列表时，应省略该内容，不允许模型自行补充事实。结果保存为 Markdown。

## LLM Provider

通过统一 provider 接口支持 DeepSeek、OpenAI 等服务。首版优先实现 OpenAI-compatible API，运行时配置 provider、base URL 和模型：

```yaml
providers:
  llm:
    base_url: https://api.example.com/v1
    model: model-name
    api_key_env: PASSAGEN_API_KEY
    timeout_seconds: 120
```

每次调用记录模型、prompt 版本、Schema 版本、token 用量、调用时间和错误信息。API key 只从指定环境变量读取。

## 本地运行目录

Passagen 默认把配置和所有受管理数据限制在启动命令时的当前工作目录：

```text
./data/
  passagen.yaml
  ...
```

- 配置文件位于数据目录内（`<data_dir>/passagen.yaml`，默认 `./data/passagen.yaml`），使配置与数据构成自包含的库；仓库提供不含隐私信息的 `passagen.example.yaml` 模板，本地配置不受 Git 跟踪，文件不存在或内容为空时仍可使用内置默认值和环境变量。
- `data/` 保存数据库、受管理 PDF 和生成产物。
- `data_dir` 只能由 `--data-dir` 命令行参数指定；配置文件或 `PASSAGEN_DATA_DIR` 环境变量设置 `data_dir` 会被拒绝。
- 默认运行不会读取或创建 `~/.config/passagen`、`~/.local/share/passagen` 等用户级目录。
- `--config`、`--data-dir` 等显式覆盖仍然有效。
- 相对覆盖路径以执行命令时的当前工作目录为基准。
- API key 继续只从指定环境变量读取，不写入 `passagen.yaml`。

配置分为两个模块：外部 provider service（`providers`，按 provider 分包，各自的超时时间一并归入对应 provider）和各处理阶段参数（`pipeline`，按阶段分包）：

```yaml
passagen:
  database_path: null
  debug: false
providers:
  crossref:
    enabled: true
    base_url: https://api.crossref.org
    mailto: null
    timeout_seconds: 10
  arxiv:
    enabled: true
    base_url: https://export.arxiv.org
    timeout_seconds: 10
  grobid:
    base_url: http://localhost:8070
    timeout_seconds: 60
  llm:
    base_url: https://api.openai.com/v1
    model: gpt-4o-mini
    api_key_env: PASSAGEN_API_KEY
    timeout_seconds: 120
    context_window_tokens: 128000
    max_context_utilization: 0.65
    safety_margin_tokens: 8000
    chars_per_token: 4.0
pipeline:
  metadata:
    first_pages: 2
  parsing:
    parser: auto
    min_text_characters: 10
  summarization:
    strategy: auto
    chunk_max_input_tokens: 24000
    chunk_overlap_paragraphs: 1
    fact_max_output_tokens: 1500
    summary_max_output_tokens: 3000
```

与模型能力相关的全局 LLM 调用参数（上下文窗口、利用率、安全余量、token 估算系数）
归入 `providers.llm`，供所有 LLM 调用共享；`pipeline` 各阶段只保留自身的策略参数。
token 估算按 `len(text) / chars_per_token` 计算，不引入 tokenizer 依赖，估算误差由安全
余量吸收。

执行需要 GROBID 的 metadata fallback 或 `auto`/`grobid` 解析前，Passagen 会检查服务健康状态。执行摘要前会检查 LLM API key 配置；不执行对应阶段时不检查这些依赖。缺少必需依赖时该阶段以明确错误失败。

配置文件使用 `yaml.safe_load` 解析。根节点和各配置分区必须是 mapping，不允许使用可执行 Python tag。当前接受 `passagen`、`providers` 和 `pipeline` 分区；`providers` 内部按 provider 分包，`pipeline` 内部按处理阶段分包，避免把所有字段堆入 `passagen`。

当前配置优先级为：CLI 参数 > 环境变量 > YAML > 内置默认值。嵌套环境变量使用双下划线，例如 `PASSAGEN_PROVIDERS__CROSSREF__TIMEOUT_SECONDS=5`。

## 数据存储

SQLite 保存论文索引、处理状态和外部调用记录，本地文件系统保存 PDF 及生成产物。默认根目录是当前工作目录下的 `data/`。程序通过 SQLAlchemy 2.0 typed ORM 和短 Session 事务访问 SQLite，使用内嵌 Alembic revision 管理 Schema，并同步 `PRAGMA user_version` 供 CLI 展示：

```text
data/
  pdfs/
    <sha256-prefix>/
      <sha256>.pdf
  papers/
    <paper-id>/
      extracted.json
      summary.json
      summary.yaml
      outline.md
  passagen.db
```

数据库至少包含：

- `papers`：论文标识、元数据、元数据来源和当前状态；
- `artifacts`：受管理 PDF、解析结果、摘要和 outline 相对 `data_dir` 的路径及版本；
- `processing_runs`：每个阶段的开始时间、结束时间、状态和错误；
- `llm_calls`：provider、模型、prompt/Schema 版本和 token 用量。

导入 PDF 时，文件原子落盘与数据库 artifact 登记必须作为一个可恢复操作处理，不能留下引用源目录的记录。后续 artifact 只有在文件完整落盘且数据库事务成功后才推进对应处理状态。失败产生的响应和中间结果应保留，便于诊断和重试。

## Initial Scope (Historical)

首版实现：

- 单机 CLI；
- DOI/arXiv ID 与 SHA-256 去重；
- Crossref DOI 与 arXiv API 元数据补全；
- GROBID 默认解析和 PyMuPDF 降级解析；
- 一个 OpenAI-compatible LLM provider；
- Pydantic/JSON Schema 校验及有限修复；
- 英文结构化摘要和基于该摘要生成的英文 outline；
- SQLite 状态管理、断点续跑和失败重试。

初始版本不实现 Web UI、OCR、向量数据库、多用户管理和论文自动下载。Web UI 现已作为
独立适配器交付；其余能力仍不属于当前 Core 范围。
