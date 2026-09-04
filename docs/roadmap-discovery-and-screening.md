# Passagen Conference Discovery & Screening Roadmap

## 1. 目标

为 Passagen 增加一个位于现有 PDF 解析流程之前的论文发现与筛选阶段，使其能够从指定会议和年份出发，自动完成：

1. 获取会议论文列表；
2. 获取论文标题、作者、DOI、abstract 等元数据；
3. 根据用户定义的研究主题进行规则预筛选；
4. 使用 LLM 对论文相关性进行结构化判断；
5. 生成人工可复核的候选论文列表；
6. 输出最终选中论文的下载清单（标题、作者、DOI、landing URL），供人工下载 PDF；
7. 人工下载的 PDF 通过现有 `scan` / `run` 入口无缝进入 Passagen 解析、事实提取和总结流程。

整体架构：

```text
Conference / Proceedings
        ↓
Paper Discovery
        ↓
Metadata Enrichment
        ↓
Lexical Prefilter
        ↓
LLM Screening
        ↓
Human Review
        ↓
Download List Export
        ↓
人工下载 PDF 到本地目录
        ↓
Existing Passagen Pipeline (passagen scan / run)
        ↓
Parse → Facts → Summary → Survey
```

核心原则是：**论文发现与论文阅读解耦**。现有 PDF pipeline 不需要理解 conference、DBLP 或 screening 等概念，它只接收本地 PDF。Passagen 不自动下载 PDF，候选清单交由人工完成获取。

---

# 2. 总体设计原则

## 2.1 Discovery 与 Ingestion 分离

新增：

```text
conference / metadata
    ↓
candidate
    ↓
download list
    ↓
人工下载 PDF
```

现有：

```text
PDF
 ↓
parse
 ↓
extract
 ↓
summarize
```

两者通过本地 PDF 文件连接，而不是让 ACM/DBLP crawler 直接调用 GROBID 或全文分析代码。人工下载的 PDF 放入目录后，直接复用现有 `passagen scan` / `passagen run` 入口，无需为 conference 来源添加任何特殊分支。

## 2.2 原始数据与模型判断分离

以下数据必须独立保存：

```text
PaperMetadata
LLM Screening Result
Human Decision
```

LLM 输出不能直接覆盖 metadata。

这样更换：

* 模型；
* prompt；
* 研究主题；
* threshold；

时，不需要重新抓会议元数据。

## 2.3 Recall First

Abstract 筛选阶段的目标不是精确决定综述最终收录论文，而是：

> 尽可能避免漏掉潜在相关论文，同时显著减少进入全文处理阶段的论文数量。

因此：

* keyword miss 不能直接 reject；
* LLM prompt 使用 broad inclusion criteria；
* borderline paper 进入 review 队列。

---

# 3. Phase 0 — 数据模型与基础设施

目标：先建立稳定的领域模型，不接任何会议 API。

## 3.1 PaperMetadata

新增：

```python
class PaperMetadata(BaseModel):
    paper_id: str

    title: str
    authors: list[str]

    abstract: str | None = None

    venue: str
    year: int

    doi: str | None = None

    landing_url: str | None = None
    pdf_url: str | None = None

    source: str
    source_id: str | None = None

    metadata_sources: dict[str, str] = {}
```

要求：

* `paper_id` 在 Passagen 内稳定；
* DOI 优先用于 dedup；
* DOI 不存在时使用 canonicalized title；
* metadata 中不包含任何 LLM 判断；
* `metadata_sources` 记录每个字段的来源（例如 `{"title": "dblp", "doi": "dblp", "abstract": "openalex"}`），用于后续排查错误。

## 3.2 Collection

新增会议集合概念：

```python
class Collection(BaseModel):
    id: str
    venue: str
    year: int

    sources: list[str]
    paper_ids: list[str]
```

示例：

```text
sigcomm-2025
nsdi-2025
sosp-2025
```

## 3.3 ScreeningQuery

新增可复用的筛选配置：

```python
class ScreeningQuery(BaseModel):
    name: str

    description: str

    include_topics: list[str]
    exclude_topics: list[str]

    examples_positive: list[str] = []
    examples_negative: list[str] = []

    include_threshold: float = 0.70
    review_threshold: float = 0.40
```

示例配置：

```yaml
name: programmable-networks

description: >
  Research related to programmable packet processing,
  network compilers, programmable hardware, in-network
  computing, and formal verification of network programs.

include_topics:
  - programmable switches
  - P4
  - RMT
  - SmartNIC
  - DPU
  - FPGA dataplane
  - network compiler
  - DSL
  - intermediate representation
  - synthesis
  - formal verification
  - symbolic execution
  - model checking
  - in-network computing

exclude_topics:
  - purely application-layer distributed systems
  - networking papers without programmable or systems novelty
```

## Deliverables

* `PaperMetadata`
* `Collection`
* `ScreeningQuery`
* JSON serialization
* basic validation
* unit tests

---

# 4. Phase 1 — Conference Paper Discovery

目标：能够从指定会议和年份获取完整论文列表。

第一版只实现一个稳定来源：

> DBLP

不要第一阶段直接依赖 ACM DL HTML scraping。

## 4.1 Provider Interface

定义（与现有 core 保持一致，使用同步接口）：

```python
class PaperDiscoveryProvider(Protocol):
    def list_papers(
        self,
        venue: str,
        year: int,
    ) -> list[PaperMetadata]:
        ...
```

按 `architecture.md` 的依赖方向落位：

```text
passagen-core/src/passagen/
├── external/discovery.py     # DBLP HTTP client 与响应解码
├── providers/discovery.py    # 重试、限流、错误转换
└── stages/discovery/         # collection 持久化与编排
```

## 4.2 DBLP Provider

支持：

```bash
passagen discover sigcomm 2025
```

输出类似：

```text
Discovered SIGCOMM 2025

Papers: 110
With DOI: 108
Without DOI: 2
```

collection 保存在现有 `data_dir`（默认 `./data/`）之下，不引入新的数据根目录：

```text
data/
└── collections/
    └── sigcomm-2025/
        ├── collection.json
        └── papers.jsonl
```

## 4.3 Deduplication

优先级：

```text
DOI
 ↓
normalized title
 ↓
title + first author + year
```

避免 enrichment 时产生重复论文。

## Acceptance Criteria

```bash
passagen discover sigcomm 2025
```

能够：

* 获取主会议论文列表；
* 保存 metadata；
* 第二次运行不会产生 duplicate；
* 网络请求失败不会损坏已有 collection。

---

# 5. Phase 2 — Metadata Enrichment

目标：补充 DBLP 通常缺少的 abstract 和其他字段。

建议第一版支持：

```text
OpenAlex
或
Semantic Scholar
```

不需要同时做两个。

## 5.1 Enricher Interface

```python
class MetadataEnricher(Protocol):
    def enrich(
        self,
        paper: PaperMetadata,
    ) -> PaperMetadata:
        ...
```

匹配优先：

```text
DOI
 ↓
exact title
 ↓
normalized title
```

## 5.2 Pipeline

```text
DBLP
 ↓
title / authors / DOI
 ↓
OpenAlex or Semantic Scholar
 ↓
abstract / landing URL / metadata
```

CLI：

```bash
passagen enrich sigcomm-2025
```

输出：

```text
110 papers

abstract:
  found:   103
  missing:   7

doi:
  found:   108
  missing:   2
```

注意：OpenAlex 返回的 abstract 是 inverted index 格式，enricher 负责重建为纯文本后再写入 `PaperMetadata`。

## 5.3 Provenance

每个字段的来源记录在 `PaperMetadata.metadata_sources` 中（见 §3.1）。

例如：

```python
paper.metadata_sources == {
    "title": "dblp",
    "doi": "dblp",
    "abstract": "openalex",
}
```

后续排查错误时很重要。

---

# 6. Phase 3 — Lexical Prefilter

目标：建立低成本、可解释的关键词提示层。

注意：

> lexical filter 不负责最终 reject。

它主要承担：

* priority ranking；
* keyword evidence；
* LLM hint；
* 未来大规模 collection 的成本优化。

## 6.1 Topic Rules

例如：

```yaml
programmable_switch:
  - programmable switch
  - programmable data plane
  - p4
  - tofino
  - rmt
  - pisa

compiler:
  - compiler
  - compilation
  - programming language
  - domain-specific language
  - intermediate representation
  - synthesis

verification:
  - formal verification
  - model checking
  - symbolic execution
  - specification
  - correctness

accelerator:
  - smartnic
  - dpu
  - fpga
  - network processor
```

## 6.2 输出

```python
class LexicalEvidence(BaseModel):
    score: float
    matched_terms: list[str]
    matched_topics: list[str]
```

不要产生：

```python
rejected = True
```

---

# 7. Phase 4 — LLM Screening

这是 MVP 最重要的一阶段。

目标：

```text
title + abstract + research query
 ↓
structured relevance judgment
```

## 7.1 Result Model

```python
class RelevanceDecision(BaseModel):
    relevant: bool

    score: float
    confidence: float

    categories: list[str]
    matched_topics: list[str]

    reasons: list[str]
```

建议最终状态由系统计算：

```text
score >= 0.70
    → INCLUDE

0.40 <= score < 0.70
    → REVIEW

score < 0.40
    → REJECT
```

而不是直接相信模型 boolean。

## 7.2 Prompt

LLM 必须明确知道：

* 这是 literature screening；
* 使用 broad inclusion criteria；
* 不要求 abstract 出现显式 keyword；
* paper 只要方法或系统架构与主题相关即可纳入；
* 不应因为论文目标应用不同而拒绝底层技术相关论文。

输入：

```text
Research scope
+
Title
+
Abstract
+
Lexical hints
```

输出严格 JSON / Pydantic。

## 7.3 Concurrency

conference 通常有约 50–300 篇论文。

与现有 core 的同步执行模型保持一致，不引入 asyncio。LLM 调用是 I/O 密集操作，使用有界线程池即可：

```text
ThreadPoolExecutor(max_workers=8)
```

对应 CLI 参数：

```text
--jobs 8
```

不要一次创建无限并发请求。

## 7.4 Failure Handling

单篇失败：

```text
LLM_ERROR
```

不能导致整个 screening 失败。

最后报告：

```text
evaluated: 108
failed:      2
```

---

# 8. Phase 5 — Screening Cache

目标：避免修改其他 pipeline 时重复花费 LLM token。

Cache key：

```text
hash(
    title,
    abstract,
    query,
    prompt_version,
    model,
)
```

变化以下内容时重新筛选：

* abstract；
* query；
* prompt；
* model。

不相关变化不得触发重跑，例如：

* PDF parser；
* GROBID config；
* summary prompt；
* fact extraction。

建议记录：

```python
class ScreeningRun(BaseModel):
    model: str
    prompt_version: str
    query_name: str

    created_at: datetime

    include_threshold: float
    review_threshold: float
```

---

# 9. Phase 6 — Human Review

目标：模型负责缩小范围，人负责处理不确定项。

第一版不要做复杂 TUI。

可以先实现：

```bash
passagen screen export-review sigcomm-2025 \
    --query programmable-networks \
    --output review.csv
```

CSV：

```text
title
authors
score
categories
llm_decision
human_decision
reason
doi
```

允许人工填写：

```text
human_decision =
    include
    reject
    blank
```

再导回：

```bash
passagen screen import-review review.csv
```

最终 decision：

```python
effective_decision = (
    human_decision
    if human_decision is not None
    else llm_decision
)
```

## 后续增强

未来可以添加：

```bash
passagen screen review ...
```

实现终端交互式 review。

---

# 10. Phase 7 — Download List Export

这一阶段晚于 screening 开发。

Passagen **不自动下载 PDF**。本阶段的目标是输出一份结构化的下载清单，供人工从出版社页面、作者主页或机构仓库获取 PDF。

目标：

```text
accepted PaperMetadata
 ↓
结构化下载清单（CSV / JSONL / Markdown）
 ↓
人工下载 PDF 到本地目录
```

命令：

```bash
passagen export sigcomm-2025 \
    --query programmable-networks \
    --output download-list.csv
```

清单每行包含人工定位和下载所需的全部信息：

```text
paper_id
title
authors
venue / year
doi
landing_url
pdf_url（仅当 metadata 中已知开放获取地址，否则留空）
final_decision
score
```

要求：

* 只包含 `effective_decision == include` 的论文；
* DOI 和 landing URL 必须可直接点击访问；
* 清单是纯导出物，重复导出不会修改 collection 或 screening 结果；
* 不提供任何绕过访问限制的下载逻辑。

人工下载完成后，将 PDF 放入一个目录（例如 `pdfs/sigcomm-2025/`），交给现有 pipeline 处理。

---

# 11. Phase 8 — 接入现有 Passagen

现有 pipeline 保持入口不变：

```text
local PDF directory
 ↓
passagen scan / passagen run
```

不要让现有 pipeline 添加：

```python
if source == "acm":
...
```

conference pipeline 只负责到下载清单为止；人工按清单下载 PDF 后，直接复用现有入口：

```bash
passagen run pdfs/sigcomm-2025/
```

现有 `scan` 的内容寻址去重（SHA-256）天然保证重复导入不会产生重复记录，因此无需为 conference 来源新增任何 ingestion API。

最终 composite command：

```bash
passagen collect sigcomm 2025 \
    --query programmable-networks
```

内部执行：

```text
discover
 ↓
enrich
 ↓
screen
 ↓
human override
 ↓
export download list
```

`collect` 到导出清单为止；下载与 `passagen run` 由人工衔接。每一步仍应可以独立调用。

---

# 12. CLI 设计

CLI 命令实现在 `passagen-cli/src/passagen_cli/commands/`，只负责参数解析和结果呈现；业务逻辑在 `passagen-core` 的 `stages/` 中，必须可以不经过 Typer 直接调用。

推荐最终 CLI：

```bash
passagen discover sigcomm 2025
```

```bash
passagen enrich sigcomm-2025
```

```bash
passagen screen sigcomm-2025 \
    --query queries/programmable-networks.yaml
```

```bash
passagen screen export-review sigcomm-2025 \
    --query programmable-networks
```

```bash
passagen export sigcomm-2025 \
    --query programmable-networks \
    --output download-list.csv
```

以及高层命令：

```bash
passagen collect sigcomm 2025 \
    --query programmable-networks
```

原则：

> 高层 command 提供 convenience，底层 command 提供 reproducibility 和 debugging。

---

# 13. 建议目录结构

遵循 monorepo 现有边界：共享业务逻辑在 `passagen-core`，CLI 在 `passagen-cli`。按 `architecture.md` 的依赖规则，HTTP 细节只在 `external/`，重试与限流在 `providers/`，编排在 `stages/`。

```text
passagen-core/src/passagen/
├── domain/
│   └── discovery.py          # PaperMetadata, Collection, ScreeningQuery
│
├── external/
│   ├── discovery.py          # DBLP HTTP client 与响应解码
│   └── enrichment.py         # OpenAlex HTTP client 与响应解码
│
├── providers/
│   ├── discovery.py          # discovery 重试、限流、错误转换
│   └── enrichment.py         # enrichment 重试、限流、缓存
│
├── screening/                # 筛选业务算法（不依赖 HTTP）
│   ├── query.py
│   ├── lexical.py
│   ├── llm.py                # 复用现有 LlmProvider 与结构化校验/修复
│   └── cache.py
│
└── stages/
    ├── discovery/            # collection 持久化与编排
    ├── enrichment/
    ├── screening/
    └── export/               # review CSV 与 download list 导出

passagen-cli/src/passagen_cli/commands/
├── discover.py
├── enrich.py
├── screen.py
├── export.py
└── collect.py
```

说明：

* `screening/` 是纯业务包，依赖 `providers.llm` 的 Protocol，不直接持有 HTTP client。
* collection 持久化（JSONL + filesystem，位于 `data_dir/collections/`）由 `stages/discovery/` 内的 storage 模块负责，不改动现有 SQLite schema。
* 不为远期目标预先创建空包；目录随对应 Milestone 实现时创建。

依赖关系：

```text
discovery
    ↓
PaperMetadata
    ↓
screening
    ↓
candidate
    ↓
download list
    ↓
人工下载 PDF
    ↓
existing pipeline (scan / run)
```

---

# 14. MVP 范围

建议将第一阶段正式定义为：

## Passagen v0.3 — Conference Discovery MVP

（主 roadmap 中 v0.1 / v0.2 / v1.0 已占用，本功能从 v0.3 起编号。）

只实现：

```text
DBLP
 ↓
OpenAlex / Semantic Scholar
 ↓
PaperMetadata
 ↓
LLM screening
 ↓
JSONL / CSV candidates
```

明确暂时不做：

* ACM HTML crawler；
* IEEE crawler；
* 自动下载 PDF（只输出人工下载清单）；
* citation graph；
* Web UI；
* vector database；
* complex ranking model。

### MVP 成功标准

以下命令能够成功运行：

```bash
passagen discover sigcomm 2025

passagen enrich sigcomm-2025

passagen screen sigcomm-2025 \
    --query programmable-networks
```

最终得到：

```text
SIGCOMM 2025
110 papers discovered
104 abstracts available

21 include
8 review
81 reject
```

并能够输出结构化 JSONL / CSV。

做到这里，这个功能已经能够实际替代目前人工浏览会议目录的工作。

---

# 15. Milestone 划分

## M1 — Domain Model

实现：

* `PaperMetadata`
* `Collection`
* `ScreeningQuery`
* persistence
* dedup

测试：

* serialization roundtrip
* DOI normalization
* title dedup

---

## M2 — DBLP Discovery

实现：

* venue/year lookup
* conference paper enumeration
* metadata persistence
* retry/error handling

验收：

```bash
passagen discover sigcomm 2025
```

得到完整 collection。

---

## M3 — Abstract Enrichment

实现一个 provider：

* OpenAlex 或 Semantic Scholar

包括：

* DOI lookup
* title fallback
* rate limiting
* cache

验收：

大部分会议论文拥有 abstract。

---

## M4 — Screening Engine

实现：

* query YAML
* lexical evidence
* structured LLM classification
* score/category/reason
* concurrency
* partial failure

验收：

可以对完整 conference 自动筛选。

---

## M5 — Reproducibility & Cache

实现：

* prompt version
* model tracking
* content hashing
* screening cache
* run metadata

验收：

重复运行不会重复调用模型。

---

## M6 — Human Review

实现：

* CSV export
* human override
* decision merge

验收：

人工可以修改 borderline paper 的最终状态。

---

## M7 — Download List Export

实现：

* 按 `effective_decision` 过滤 included papers
* CSV / JSONL / Markdown 清单导出
* DOI、landing URL、已知开放 pdf_url 的完整输出

验收：

```bash
passagen export sigcomm-2025 \
    --query programmable-networks \
    --output download-list.csv
```

产出的清单可以直接用于人工逐篇下载 PDF。

---

## M8 — Existing Pipeline Integration

实现：

```text
download list
→ 人工下载 PDF 到目录
→ passagen run <directory>（现有入口，无改动）
```

增加：

```bash
passagen collect
```

验收：

从：

```text
SIGCOMM 2025
```

一路运行（中间经人工下载）至：

```text
Passagen summarized papers
```

---

# 16. 第二阶段扩展

v0.3 MVP 完成后再做以下功能。

## 16.1 Additional Sources

新增：

```text
USENIX
OpenReview
ACM
IEEE
conference-specific source
```

这些都实现同一个：

```python
PaperDiscoveryProvider
```

而不是修改核心 pipeline。

## 16.2 Citation Expansion

引入：

```text
Seed Paper
 ↓
references
citations
similar papers
 ↓
screening
```

命令示例：

```bash
passagen expand \
    --seed doi:xxx \
    --citations \
    --references
```

## 16.3 Cross-conference Collection

例如：

```yaml
venues:
  - SIGCOMM
  - NSDI
  - SOSP
  - OSDI

years:
  from: 2020
  to: 2026
```

形成：

```text
programmable-networks-2020-2026
```

## 16.4 Human Feedback as Few-shot Examples

人工判断：

```text
include/reject
```

可以自动转化成下一次 screening 的 few-shot examples。

例如：

```text
positive:
- VeriLucid
- Presto
- EPIC

negative:
- unrelated measurement paper
```

逐渐形成用户自己的 literature-screening preference。

---

# 17. 第三阶段：从 Conference Reader 到 Literature Discovery System

长期结构：

```text
                    Conference
                        │
Keyword Search ─────────┤
                        │
Citation Graph ─────────┼──→ Research Collection
                        │
Seed Papers ────────────┤
                        │
Manual DOI/PDF ─────────┘
                              ↓
                         LLM Screening
                              ↓
                         Human Review
                              ↓
                        Full-text Reading
                              ↓
                         Fact Extraction
                              ↓
                       Literature Synthesis
```

此时 Passagen 不再只是：

> PDF reader + summarizer

而成为：

> literature discovery, screening, reading and synthesis system

---

# 18. 当前建议开发顺序

按优先级：

```text
1. PaperMetadata / Collection
2. DBLP discovery
3. abstract enrichment
4. JSONL persistence + dedup
5. ScreeningQuery
6. LLM structured screening
7. screening cache
8. CLI discover / enrich / screen
------------------------------
MVP boundary
------------------------------
9. human review
10. download list export
11. existing pipeline integration (passagen run，无改动)
12. additional conference providers
13. citation expansion
14. cross-conference collections
```

其中第 1–8 项应作为第一个可交付版本。

---

# 19. 非目标

第一阶段明确避免：

* 为每个 publisher 编写复杂 scraper；
* 自动下载 PDF（只输出人工下载清单，不实现任何绕过访问限制的逻辑）；
* 引入大型数据库；
* 构建 Web frontend；
* 使用 embedding/vector DB 替代简单筛选；
* 自动生成最终 survey；
* 将 conference-specific 逻辑加入现有 parser；
* 引入 asyncio（保持现有同步执行模型，并发用有界线程池）。

第一阶段建议继续使用：

```text
Pydantic
+
JSONL
+
filesystem
+
content-addressed cache
```

只有当 collection 扩展到数万篇论文，并需要频繁做跨集合查询和 citation graph 操作时，再评估 SQLite 或 DuckDB。

---

# 20. 最终架构约束

开发过程中应始终保持：

```text
Discovery source
      ↓
PaperMetadata
      ↓
Screening
      ↓
PaperCandidate
      ↓
Download list
      ↓
人工下载 Local PDF
      ↓
Existing Passagen
```

不要形成：

```text
DBLP/ACM
 ↓
LLM
 ↓
GROBID
 ↓
special-case pipeline
```

新的 conference discovery 功能应当只是 Passagen 的一个独立前置阶段。

**最核心的架构目标是让“如何找到论文”和“如何阅读论文”完全解耦。**
