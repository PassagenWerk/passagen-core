# Paper Summary 上下文改进路线图

本文档定义 `passagen-core` 在论文总结阶段的上下文管理和切块改进方案。目标是在模型
上下文足够时保留全文理解能力，在论文过长时通过语义切块维持事实覆盖率，并让输入、
输出和安全余量都受到明确的 token 预算约束。

## 结论

Summary 不应对所有论文强制使用同一种策略。目标行为如下：

```text
ParsedPaper
  -> 计算完整请求的 token 预算
  -> 全文可舒适容纳
       -> 直接根据全文生成 StructuredSummary
  -> 全文无法舒适容纳
       -> 按章节和段落提取结构化 evidence
       -> 合并、去重并处理冲突
       -> 根据 evidence 生成 StructuredSummary
```

普通长度论文优先使用全文生成。全文输入低于可用上下文预算时，这种方式通常具有更好的
全局结构理解、跨章节关联和摘要连贯性。分块流程仅用于超长论文，不再作为所有论文的默认
信息瓶颈。

## 当前实现

当前 summarization 是固定的两阶段 Map-Reduce：

1. `_chunks()` 按 `max_chunk_characters` 切分 `ParsedPaper.sections`。
2. 每块生成一个 `ExtractedFacts`。
3. 最终 Summary 只读取所有 facts，不再读取论文原文。

主要实现位置：

- `src/passagen/stages/summarization/service.py`：切块、facts 提取和 Summary 生成。
- `src/passagen/stages/summarization/schema.py`：`ExtractedFacts` 和 `StructuredSummary`。
- `src/passagen/external/llm.py`：OpenAI-compatible 请求和 token usage 读取。
- `src/passagen/config/models.py`：summarization 配置。

当前方案存在以下问题：

- `max_chunk_characters` 限制字符数，不代表模型 token 数或 context window。
- `_chunks()` 达到字符上限后直接截断，可能切断句子、表格、图注或实验结果。
- 每个块缺少 abstract、章节路径和论文级上下文，难以判断局部内容的重要性。
- `ExtractedFacts` 只有 `list[str]`，会丢失类别、来源、实体归属和指标关系。
- 最终 Summary 请求包含全部 facts，但没有检查该请求的输入加输出是否超出上下文。
- API 请求中的 `max_tokens` 只约束输出，不能替代总 context window 管理。
- 实际配置和示例不一致：代码及当前 `passagen.yaml` 使用 12,000 字符，而 README 和
  `passagen.example.yaml` 使用 96,000 字符。

## 设计目标

- 全文可容纳时，Summary 模型能够直接访问完整论文内容。
- 所有 LLM 请求在发送前完成输入、预期输出和安全余量预算。
- 长论文按语义边界切分，不在句子、表格和相关上下文中间硬切。
- 中间 evidence 保留页码、章节、事实类别和定量结果的归属关系。
- 最终 Summary prompt 的大小同样受预算约束，不将无限增长的 facts 一次性拼接。
- 保留 Schema 校验、响应修复、调用审计和可复用缓存能力。
- 策略选择和实际 token 使用可观测。

## 非目标

- 本改进不改变 Paper、Artifact 和 ProcessingRun 的核心生命周期。
- 首阶段不实现跨论文检索或外部知识增强。
- 不根据模型名称硬编码供应商特定的 context window。
- 不以填满模型声明的最大上下文为目标；长上下文本身不保证模型能同等关注所有位置。
- 不引入 tokenizer 依赖做精确 token 计数，也不建设 benchmark 评测体系。

## 策略选择

建议提供 `auto`、`full` 和 `hierarchical` 三种策略，默认使用 `auto`：

| 策略 | 行为 | 用途 |
|---|---|---|
| `auto` | 根据请求预算自动选择全文或分层总结 | 默认生产行为 |
| `full` | 全文直接生成，预算不足时明确失败 | 对比实验和确定可容纳的模型 |
| `hierarchical` | 始终执行语义切块和 evidence 汇总 | 超长论文和对比实验 |

全文模式的判定不能只比较论文 token 数和 context window。必须计算完整请求：

```text
available_input_tokens =
    context_window_tokens
    - summary_max_output_tokens
    - safety_margin_tokens

full_request_tokens =
    instructions
    + JSON Schema
    + identity
    + serialized paper
```

仅当 `full_request_tokens <= available_input_tokens` 时进入全文模式。安全余量建议至少为
context window 的 15% 到 20%。对于超大上下文模型，可以进一步设置最大利用率，默认不
超过声明窗口的 60% 到 70%，降低 long-context attention degradation 的风险。

## Token 预算

### 配置方向

配置分为两层：与模型能力相关的全局 LLM 调用参数放在现有的 `providers.llm` 段（之后其它
LLM 调用，如 outline，也复用同一套参数）；`pipeline.summarization` 只保留与
summary 策略和切块方式相关的配置。

```yaml
providers:
  llm:
    # 全局 LLM 调用参数，对所有 LLM 调用生效
    context_window_tokens: 128000
    max_context_utilization: 0.65
    safety_margin_tokens: 8000
    chars_per_token: 4.0

pipeline:
  summarization:
    # 仅与 summary 策略和切块方式相关的配置
    strategy: auto
    chunk_max_input_tokens: 24000
    chunk_overlap_paragraphs: 1
    fact_max_output_tokens: 3000
    summary_max_output_tokens: 6000
```

`context_window_tokens` 由用户或部署配置明确提供。OpenAI-compatible API 通常不会可靠地
返回模型上下文上限，不能仅根据 `model` 字符串推断。现有 `max_chunk_characters` 应逐步
替换为上述 token 语义配置。

### Token 估算

首个版本的 Token 估算保持简单：按字符数乘以系数估算，不引入 tokenizer 依赖。

```text
estimated_tokens = char_count / chars_per_token
```

- `chars_per_token` 是可配置的全局参数，英文文本默认值约为 4.0；用户可按所用模型和
  语料语言调整。这是保守估算，安全余量用于吸收估算误差。
- 预算对象同时包含 prompt、Schema、source、输出上限和安全余量。
- 请求发送前对序列化后的完整 prompt 做一次估算检查，而不是只估算原始论文正文。
- API 返回 usage 后记录估算值与实际 `input_tokens` 的偏差，便于用户校准系数。

## 全文模式

全文模式直接根据 `ParsedPaper` 生成 `StructuredSummary`，避免先压缩成字符串 facts。

输入至少包含：

- 权威 paper identity。
- Abstract 和按顺序序列化的章节内容。
- 章节标题、层级和页码。
- 与原文绑定的表格、图注信息；解析器没有提供时不进行推断。
- `StructuredSummary` JSON Schema 和现有事实约束。

全文 prompt 应继续要求：

- 只能使用论文提供的事实。
- 保留定量结果的主体、baseline、方向、单位和条件。
- `evaluation.results` 和 `evaluation.ablations` 必须包含 evidence pages。
- 缺少证据时使用 `null` 或空列表，不根据常识补全。

全文模式需要独立的 prompt 模板，例如 `summary-full-v3.txt`。它不应复用当前只接受
`$facts` 的 reduce prompt，以免模板在两种输入形态之间产生模糊语义。

## 分层模式

### 语义切块规则

切块器按以下优先级寻找边界：

1. 一级或二级章节边界。
2. 段落边界。
3. 完整句子边界。
4. 仅在单个语义单元本身超过预算时执行受控硬切，并记录该情况。

必须遵守以下组合规则：

- Abstract 和 Introduction 可以作为论文级背景共同处理。
- 实验设置作为共享 evaluation context 附加到相关实验结果块。
- 表格或图注与紧邻的解释段落保持在同一个块中。
- 标题、章节路径和页码范围包含在每个块中。
- 相邻块使用少量段落级 overlap，而不是固定字符 overlap。
- overlap 内容在合并阶段通过来源位置去重。

每个 chunk 的 token 预算使用完整 facts prompt 计算，不能只限制 source token 数。

### 结构化 Evidence

`ExtractedFacts.facts: list[str]` 应升级为有类型的 evidence。最小结构应表达：

```json
{
  "category": "evaluation_result",
  "claim": "The system processes ...",
  "section": "6.2 Throughput",
  "evidence_pages": [11, 12],
  "subject": "System A",
  "subject_value": "...",
  "baseline": "System B",
  "baseline_value": "...",
  "conditions": ["..."],
  "source_excerpt": "..."
}
```

并非所有 evidence 都需要定量字段。Schema 可以使用分类联合类型，至少覆盖：

- problem、motivation 和 goals。
- contribution。
- design component、process 和 mechanism。
- implementation detail。
- evaluation setup、result 和 ablation。
- limitation、trade-off 和 threat to validity。
- conclusion 和 related work distinction。

合并阶段必须按来源位置和规范化 claim 去重，保留冲突事实而不是静默选择其中一个。无法
解决的冲突应同时提供给最终 Summary 模型，并要求避免生成强于证据的结论。

### 分层汇总预算

当所有 evidence 仍无法放入最终 Summary prompt 时，应按 Summary Schema 的领域分组进行
中间归并，例如 problem、design、evaluation 和 discussion。每组先生成有预算限制的
evidence bundle，最终请求再组合这些 bundle。

不得简单截断 facts 列表，因为论文后部的 limitations、threats 和 conclusions 会因此被
系统性丢失。

## Prompt 与 Artifact 版本

本改进会改变输入形态和中间 Schema，应按独立版本管理：

- 全文 Summary prompt 使用新版本。
- 分块 evidence prompt 和 reduce prompt 分别版本化。
- Evidence artifact 保存 strategy、prompt hash、Schema version 和 chunker version。
- 缓存 key 至少包含 source、prompt、evidence Schema 和 chunker 版本。
- Summary 调用审计记录选择的 strategy、估算 token、实际 token 和预算结果。
- 已有 v2 Summary 不需要原地转换；用户显式重新生成时产生新版本 artifact。

自定义模板需要分别定义全文、evidence 和 reduce 所需 placeholder。`passagen config check`
应验证每种已配置模板的 placeholder 集合。

## 实施阶段

### M1：全局 LLM 配置与全文模式

工作内容：

- 增加全局 `llm` 配置段：context window、安全余量、输出上限、`chars_per_token`。
- 实现基于字符数系数的 Token 估算和请求预算对象。
- 增加 `auto`、`full`、`hierarchical` 策略配置。
- 实现 `ParsedPaper` 的稳定全文序列化和全文 Summary prompt。
- 在请求发送前校验完整 prompt 和输出预算。
- 记录策略选择、预算和实际 usage。

验收条件：

- 全文请求在预算内时，`auto` 只执行一次 Summary 生成请求。
- 全文请求超出预算时，`auto` 稳定地进入 hierarchical 模式。
- 强制 `full` 且预算不足时，在调用 provider 前返回明确错误。
- prompt、Schema 和 identity 的 token 均计入预算。
- 现有响应校验和 repair 行为继续有效。

### M2：语义切块

工作内容：

- 用章节、段落和句子感知的 chunk builder 替换 `_chunks()`。
- 为 chunk 附加论文背景、章节路径和页码。
- 实现段落级 overlap 和来源位置去重。
- 对超长单段、表格和无结构解析结果定义降级行为。

验收条件：

- 正常文本不在句子中间切断。
- 表格或图注不会与其直接解释段落无条件分离。
- 每个渲染后的 facts prompt 都不超过其输入预算。
- 空章节、单个超长章节和缺失页码均有测试覆盖。

### M3：结构化 Evidence 与分层 Reduce

工作内容：

- 将字符串 facts 替换为结构化 evidence Schema。
- 实现 evidence 合并、去重和冲突保留。
- 最终 prompt 超预算时按 Summary 领域进行中间归并。
- 更新 facts 缓存和 artifact 版本。

验收条件：

- 定量 evidence 明确保留 subject、baseline、单位、条件和页码。
- 重叠 chunk 不会在最终 Summary 中造成重复结果。
- evidence 总量超过最终预算时不会依赖尾部截断。
- 缓存不会跨不兼容的 prompt、Schema 或 chunker 版本复用。

### M4：收尾

工作内容：

- 同步 README、example config、design 和 operations 文档。
- 删除过渡期的字符切块配置。

验收条件：

- 配置文档和代码默认值保持一致。
- `passagen config check` 能发现缺失 context window 和不合法预算。

## 质量检查

不建设独立的 benchmark 体系。每个阶段完成后，用少量代表性论文（短论文、典型系统
论文、超过全文预算的长论文各一篇）人工抽查以下方面：

- Summary 中的定量 claim 是否能在原文找到支持，主体、baseline 和方向是否正确。
- 问题、贡献、设计、实验和限制是否有明显遗漏。
- evidence pages 是否真实支持对应字段。
- 重叠切块是否造成重复结果，超长论文是否丢失结尾的 limitations 和 conclusions。
- 多次运行的结构和关键事实是否稳定。

## 测试范围

单元测试：

- token 估算边界和安全余量。
- `auto` 策略路由。
- 全文和 chunk prompt 的完整预算。
- 章节、段落、句子和超长单元切块。
- overlap 去重、evidence 合并和冲突保留。
- prompt 及 artifact cache versioning。

集成测试：

- 全文模式只调用 Summary provider。
- 分层模式按预期执行 evidence 和 reduce 调用。
- provider 返回实际 usage 后正确记录估算偏差。
- context 超限在请求发出前失败或降级，不依赖 provider 错误。
- truncated response、Schema repair 和断点缓存继续工作。

回归测试：

- Paper 状态转换保持不变。
- 失败任务停留在最后成功状态。
- 强制重新总结不会错误复用不兼容 evidence。
- Summary 和 Outline artifact 仍只基于通过 Schema 校验的数据生成。

## 完成标准

满足以下条件后，可以认为上下文改进完成：

- 普通论文默认走全文模式，超长论文自动走分层模式。
- 所有 Summary 相关请求具有明确且可审计的 token 预算。
- 生产路径不再使用固定字符硬切作为主要策略。
- 分块 evidence 能保留定量关系、章节和页码。
- 最终 reduce 不会因 facts 数量增长而无界扩张或静默截断。
- 配置、README、design、operations 和示例文件中的默认值及术语一致。
