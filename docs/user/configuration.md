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
  grobid:
    base_url: http://localhost:8070
    timeout_seconds: 60
  llm:
    base_url: https://api.deepseek.com/v1
    model: deepseek-flash-v4
    api_key_env: PASSAGEN_API_KEY
    timeout_seconds: 120
    disable_thinking: false
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
  abstract_fixing:
    enabled: true
    max_output_tokens: 2000
    prompt_path: null
  summarization:
    strategy: auto
    chunk_max_input_tokens: 24000
    chunk_overlap_paragraphs: 1
    fact_max_output_tokens: 1500
    summary_max_output_tokens: 3000
    facts_prompt_path: null
    summary_prompt_path: null
    full_prompt_path: null
    reduce_prompt_path: null
    repair_prompt_path: null
  outlining:
    max_output_tokens: 4000
    prompt_path: null
```

`database_path: null` 使用 `<data-dir>/passagen.db`。`data_dir` 只由 CLI/Web 参数提供，不能
写入 YAML。相对 prompt path 以配置文件所在目录为基准解析。

## DeepSeek

默认 LLM 是 DeepSeek `deepseek-flash-v4`，通过 OpenAI-compatible Chat Completions API
调用：

```text
POST https://api.deepseek.com/v1/chat/completions
```

API key 只从 `providers.llm.api_key_env` 指定的环境变量读取：

```bash
export PASSAGEN_API_KEY=your-deepseek-api-key
```

不要把 key 写入 YAML。Passagen 不会在配置输出、普通日志、数据库或诊断 artifact 中保存
key。环境变量必须在启动 CLI 命令或 Web 服务的同一个 shell 中设置。

`context_window_tokens` 必须与实际模型能力一致。Passagen 使用
`max_context_utilization`、`safety_margin_tokens` 和 `chars_per_token` 估算可用输入预算。
如果替换为另一个 OpenAI-compatible 服务，应同时修改 `base_url`、`model` 和上下文参数。
直连 `deepseek.com` 时，Passagen 会禁用 thinking，以避免 reasoning token 占用结构化输出预算。

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

Passagen 只在识别到精确 DOI 或 arXiv ID 后查询对应服务，不根据模糊标题自动合并论文。

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
export PASSAGEN_PROVIDERS__LLM__MODEL=deepseek-flash-v4
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
