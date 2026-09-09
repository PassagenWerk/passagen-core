# 共享配置

Passagen CLI 和 Passagen Web 使用同一份 YAML 配置。默认位置是
`<data-dir>/passagen.yaml`；CLI 的 `--config` 和 Web 的 `--config` 可以显式指定其他文件。
Core 负责字段校验和默认值，两个入口不得维护不同的配置格式。

## 完整示例

```yaml
passagen:
  database_path: null
  debug: false

providers:
  healthcheck_timeout_seconds: 3
  crossref:
    enabled: true
    base_url: https://api.crossref.org
    mailto: null
    timeout_seconds: 10
  arxiv:
    enabled: true
    base_url: https://export.arxiv.org
    timeout_seconds: 10
  citation_page:
    enabled: true
    allowed_hosts:
      - www.usenix.org
    timeout_seconds: 10
  openalex:
    enabled: true
    base_url: https://api.openalex.org
    mailto: null
    timeout_seconds: 10
  grobid:
    base_url: http://localhost:8070
    timeout_seconds: 60
  llm:
    default:
      base_url: https://api.deepseek.com/v1
      model: deepseek-flash-v4
      api_key_env: PASSAGEN_API_KEY
      timeout_seconds: 120
      max_context_window: 1000000
      flavor: deepseek
      reasoning: disable

pipeline:
  metadata:
    first_pages: 2
  parsing:
    parser: auto
    min_text_characters: 10
  abstract_fixing:
    enabled: true
    max_output_tokens: 2000
    prompt_path: null
  summarization:
    strategy: auto
    chunk_max_input_tokens: 128000
    chunk_overlap_paragraphs: 1
    fact_max_output_tokens: 6000
    summary_max_output_tokens: 20000
    facts_prompt_path: null
    summary_prompt_path: null
    full_prompt_path: null
    reduce_prompt_path: null
    repair_prompt_path: null
  outlining:
    max_output_tokens: 40000
    prompt_path: null

assistant:
  rewrite_max_output_tokens: 1024
  equivalence_max_output_tokens: 1024
  max_qa_candidates: 3
  qa_candidate_pool_size: 20
  answer_max_output_tokens: 8192
  truncated_response_max_attempts: 3
  max_history_messages: 10
  max_raw_sections: 3
  collection_max_input_tokens: 64000
  collection_map_max_output_tokens: 8000
  collection_synthesis_max_output_tokens: 12000
  collection_max_selected_papers: 4
  report_max_output_tokens: 12000
  report_validation_max_attempts: 2
  rewrite_prompt_path: null
  equivalence_prompt_path: null
  answer_prompt_path: null
  repair_prompt_path: null
  report_prompt_path: null
```

`database_path: null` 使用 `<data-dir>/passagen.db`。`data_dir` 只由 CLI/Web 参数提供，不能
写入 YAML。相对 prompt path 以配置文件所在目录为基准解析。

## DeepSeek

默认 LLM 是 DeepSeek `deepseek-flash-v4`，通过 OpenAI-compatible Chat Completions API
调用：

```text
POST https://api.deepseek.com/v1/chat/completions
```

API key 只从 `providers.llm.default.api_key_env` 指定的环境变量读取：

```bash
export PASSAGEN_API_KEY=your-deepseek-api-key
```

不要把 key 写入 YAML。Passagen 不会在配置输出、普通日志、数据库或诊断 artifact 中保存
key。环境变量必须在启动 CLI 命令或 Web 服务的同一个 shell 中设置。

`max_context_window` 必须与实际模型能力一致。`flavor: deepseek` 使用 Chat Completions API 和
`thinking` 参数；`flavor: openai` 使用 Responses API 和 `reasoning.effort`。Passagen 不会根据
URL 猜测 flavor。`base_url` 应包含供应商要求的 API 前缀，例如配置
`https://api.openmodel.ai/v1` 后，请求地址为 `https://api.openmodel.ai/v1/responses`。
`reasoning` 必须显式设置为 `enable` 或 `disable`。如果替换模型，应同时检查 `base_url`、
`model`、`flavor` 和上下文窗口。

默认 pipeline 预算按 `deepseek-flash-v4` 的 1M 上下文设置：完整论文优先单次总结，分层总结的
单块输入上限为 128K，并为 summary 和 outline 保留较大的结构化输出空间。Abstract cleanup 和
问题改写本身是短任务，因此其输出预算不会随上下文窗口等比例放大。

## 多模型路由

默认情况下，所有任务都使用 `llm.default`。高级配置可以定义完整的额外 profile，并只路由需要
不同模型的任务；没有出现在 `tasks` 中的任务仍使用默认配置：

```yaml
providers:
  llm:
    default:
      base_url: https://api.deepseek.com/v1
      model: fast-model
      api_key_env: PASSAGEN_API_KEY
      timeout_seconds: 120
      max_context_window: 128000
      flavor: deepseek
      reasoning: disable
    profiles:
      reasoning:
        base_url: https://api.deepseek.com/v1
        model: reasoning-model
        api_key_env: PASSAGEN_API_KEY
        timeout_seconds: 180
        max_context_window: 128000
        flavor: deepseek
        reasoning: enable
    tasks:
      summary_synthesis: reasoning
      qa_answer: reasoning
```

每个 task key 路由一次特定用途的 LLM 调用：

| Task | 调用时机与职责 |
|---|---|
| `abstract_cleanup` | 清理或修复论文 abstract，输出结构化的 abstract 候选。只影响启用了 abstract fixing 的处理流程。 |
| `summary_evidence` | 分层总结时逐块读取论文正文并提取带页码的 evidence。长论文可能调用多次，适合吞吐量高、成本较低的模型。完整论文能直接总结时不会调用。 |
| `summary_reduce` | 合并后的 chunk evidence 超出最终总结模型上下文时，压缩 evidence。可能调用多次；上下文窗口会影响分组大小。无需压缩时不会调用。 |
| `summary_synthesis` | 将完整论文或已合并的 evidence 生成最终 Structured Summary。这是论文总结质量的主要模型。 |
| `summary_repair` | Structured Summary 不符合 schema 或 evidence 约束时修复候选结果。应支持与 synthesis 相同的结构化输出，并具有足够的上下文窗口。 |
| `outline_synthesis` | 从论文内容生成最终 Markdown outline。 |
| `qa_rewrite` | 结合最近对话，把追问改写为独立问题和检索 query。Paper Ask 与 Collection Ask 都会使用。 |
| `qa_equivalence` | 判断当前问题能否完整或部分复用已有 QA artifact。该判断不可用时会跳过复用并继续正常回答。 |
| `qa_answer` | 根据检索到的 Summary、outline 或原文 evidence 生成带 citation 的 Paper/Collection Ask 答案。 |
| `qa_repair` | QA 答案 schema、citation 或引用文本校验失败时修复答案。 |
| `collection_synthesis` | Collection 的全部 Summary 能在单个 prompt 中放入时，直接生成最终 Collection Intelligence。它不会用于超预算 Collection 的分批步骤。 |
| `collection_map` | direct prompt 超预算时，把 Collection 分批，并为每批 Summary 生成一个可校验的中间 synthesis。批次数量由该 profile 的上下文窗口和 `collection_max_input_tokens` 共同决定。 |
| `collection_reduce` | 合并一个或多个 map 中间结果，生成最终 Collection Intelligence；中间结果仍过大时会执行多轮 reduce。 |
| `collection_repair` | 任意 direct、map 或 reduce synthesis 未通过 schema、coverage 或 citation 校验时，修复该候选结果。未显式路由时使用 `default`，并不自动沿用产生候选结果的 profile。 |
| `report_answer` | 使用 Collection Summary 和可用的 synthesis 生成 Review、Comparison、Gaps 或 Custom Research Document。 |
| `report_repair` | Research Document 未通过 schema 或 citation 校验时执行修复，最多调用 `report_validation_max_attempts` 次。 |

Profile 必须完整配置；`default` 是保留名称，不能出现在 `profiles` 中。不同 task 可以指向同一个
profile。未出现在 `tasks` 中的 task 使用 `default`，不会自动继承相邻阶段的路由。例如只配置
`collection_synthesis: gpt` 时，direct synthesis 使用 `gpt`，但失败后的
`collection_repair` 仍使用 `default`。

Collection 是否选择 direct 或 map/reduce 由输入预算自动决定。direct 调用在 generation log 中
可能显示 `stage: reduce`，但其路由 task 仍是 `collection_synthesis`；`stage` 表示生成流水线阶段，
task key 表示选择哪个 LLM profile。

## Assistant

`assistant` 控制 Ask、Collection Synthesis 和 Research Document 的生成预算与上下文规模。
`rewrite_max_output_tokens` 用于把追问改写为独立问题和检索词；
`equivalence_max_output_tokens`、`max_qa_candidates` 和 `qa_candidate_pool_size` 控制历史答案语义
复用；`answer_max_output_tokens` 同时用于答案生成与引用修复。启用 reasoning 时，reasoning
token 也会消耗相应的输出预算。

答案因长度截断且不符合 schema 时，Passagen 最多按
`truncated_response_max_attempts` 重试，并逐次扩大输出预算。`max_history_messages` 限制送入
rewrite 和 answer 上下文的最近消息数；设为 `0` 可禁用历史上下文。`max_raw_sections` 限制事实
查询最多加载的原文章节数。

`collection_max_input_tokens` 是应用层的 Collection 输入预算，不等于模型的
`max_context_window`。`collection_map_max_output_tokens` 用于大型 Collection 的分批 map 输出，
`collection_synthesis_max_output_tokens` 用于 direct/reduce 的最终结构化结果。
`collection_max_selected_papers` 只限制 Collection Ask 每轮选择的相关 papers，不限制 Synthesis
覆盖。`report_max_output_tokens` 包含结构化 JSON 的开销；`report_validation_max_attempts` 是报告
Schema 或 citation 校验失败后的有界修复次数。

QA 和 report prompt path 为 `null` 时使用 Core 内置模板。相对路径与 pipeline prompt 一样，
以配置文件所在目录为基准解析。

## GROBID 和 PDF Parser

GROBID 提供学术 PDF header 和全文 TEI 解析。使用 Docker 启动：

```bash
docker run --rm --init -p 8070:8070 lfoppiano/grobid:0.9.1-crf
curl http://localhost:8070/api/isalive
```

Parser 模式：

| 值 | 行为 |
|---|---|
| `auto` | 优先使用配置的 GROBID 路径，并按可用性执行 parser 策略。 |
| `grobid` | 要求 GROBID 可用；不可用时该论文处理失败。 |
| `pymupdf` | 仅使用本地 PyMuPDF，不需要 GROBID。 |

没有运行 GROBID 时建议显式设置：

```yaml
pipeline:
  parsing:
    parser: pymupdf
```

PyMuPDF 不能替代 OCR。输入 PDF 必须已经包含文本层。

## Metadata Provider

Passagen 按以下优先级合并 metadata：用户编辑、Crossref/arXiv 精确标识符结果、来源页
`citation_*` metadata、OpenAlex 严格搜索结果、GROBID、本地 PDF。后面的低优先级来源只用于
补齐高优先级来源缺少的字段。

来源页查询仅接受 `citation_page.allowed_hosts` 中的精确 HTTPS 主机名。Passagen 不跟随
重定向，不接受 URL 凭据、非 443 端口或子域通配；需要支持出版社或会议站点时应逐个加入：

```yaml
providers:
  citation_page:
    enabled: true
    allowed_hosts:
      - www.usenix.org
      - proceedings.example.org
```

OpenAlex 使用标题搜索，但不会直接采用模糊结果。候选标题规范化后必须完全一致；PDF 已有作者
时还必须至少匹配一名作者；无作者时必须只有一个同标题、同年份候选。OpenAlex 建议设置联系
邮箱：

```yaml
providers:
  openalex:
    enabled: true
    mailto: you@example.com
```

Crossref 建议设置联系邮箱：

```yaml
providers:
  crossref:
    enabled: true
    mailto: you@example.com
```

不需要外部 metadata enrichment 时，可以分别设置：

```yaml
providers:
  crossref:
    enabled: false
  arxiv:
    enabled: false
  citation_page:
    enabled: false
  openalex:
    enabled: false
```

Provider 请求失败时，本地 PDF metadata 和已经确认的用户编辑值仍会保留。

## Pipeline

| 字段 | 说明 |
|---|---|
| `metadata.first_pages` | 本地标识和 metadata 提取读取的前几页。 |
| `parsing.parser` | `auto`、`grobid` 或 `pymupdf`。 |
| `parsing.min_text_characters` | 判断 PDF 文本层有效的最低字符数。 |
| `abstract_fixing.enabled` | 是否生成 cleaned Author Abstract；该阶段非阻塞。 |
| `abstract_fixing.max_output_tokens` | Abstract clean 最大输出 token。 |
| `summarization.strategy` | `auto`、`full` 或 `hierarchical`。 |
| `summarization.chunk_max_input_tokens` | 分层模式单个 evidence 输入预算。 |
| `summarization.chunk_overlap_paragraphs` | 相邻语义块重叠段落数。 |
| `summarization.fact_max_output_tokens` | 单个 evidence 请求输出上限。 |
| `summarization.summary_max_output_tokens` | Summary 和 repair 输出上限。 |
| `outlining.max_output_tokens` | Outline 输出上限。 |

`auto` summarization 在全文请求超过上下文预算时切换到 hierarchical。`full` 超出预算会报错，
`hierarchical` 始终执行语义切块和 evidence reduce。

处理顺序：

```text
Metadata -> Full text -> Abstract clean -> Summary -> Outline
```

Abstract clean 保留原始 Author Abstract，失败只产生 warning。单独重建 Abstract clean 不会
重建 Summary 或 Outline；从 Full text 或更早阶段重建时会按完整顺序更新下游 artifact。

## 自定义 Prompt

所有 prompt path 为 `null` 时使用 Core 内置模板。自定义模板必须保留对应占位符，否则
`config check` 会拒绝配置。修改 prompt 后，已有 artifact 不会被静默覆盖，应显式 reprocess
相关阶段。

## 环境变量覆盖

配置支持以 `PASSAGEN_` 开头、双下划线分隔层级的环境变量。例如：

```bash
export PASSAGEN_PROVIDERS__LLM__DEFAULT__MODEL=deepseek-flash-v4
export PASSAGEN_PIPELINE__PARSING__PARSER=pymupdf
```

API key 是例外：它的环境变量名称由 `api_key_env` 的值决定。

## 验证配置

使用 CLI 检查最终配置和 prompt：

```bash
passagen --data-dir /path/to/library config check
passagen --data-dir /path/to/library check
```

Web 使用同一配置，但不会代替 CLI 执行配置诊断。CLI 使用见
Passagen CLI 仓库，Web 启动参数见 Passagen Web 仓库的 README。
