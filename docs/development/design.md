# Passagen Core 设计

本文档记录 Passagen 共享业务层的产品方法和稳定设计决策。模块依赖见
[架构文档](architecture.md)，用户配置见[共享配置](../user/configuration.md)。

## 目标

Passagen 将本地 PDF 转换为可恢复、可验证和可浏览的论文库，同时保留原始材料与生成内容的
边界。

- PDF 导入后由 Passagen 管理，不依赖原扫描路径。
- 每个阻塞式阶段在成功提交 artifact 后才推进 PaperStatus。
- 生成内容必须经过 schema 校验，并可追溯到输入、prompt 和模型。
- 用户编辑值优先于自动补全，不被后台处理静默覆盖。
- CLI 与 Web 共享完全相同的业务规则、配置和数据目录。

## 论文身份

Paper 使用不可变内部 ID。以下标识分别规范化并建立唯一约束：

- 原始 PDF SHA-256
- DOI
- arXiv ID

标题只用于检索和人工判断，不作为自动去重依据。原始文件名用于展示和审计，不作为后续文件
读取路径。

## 数据所有权

SQLite 保存论文身份、状态、用户组织数据、run 和 artifact 索引。文件系统保存原始 PDF 与
较大的生成 artifact。数据库中的 artifact path 始终相对于 data directory，并附带版本、大小
和可用时的 SHA-256。

删除、迁移或恢复必须同时维护数据库和文件。应用通过 catalog/repository contract 获取
artifact，不根据约定文件名直接访问。

## Metadata

Metadata 候选来自本地 PDF、GROBID、Crossref 和 arXiv。合并遵循以下原则：

- 用户编辑值优先级最高。
- DOI 和 arXiv 只做精确查询。
- 外部 provider 失败可降级到已经取得的本地值。
- 自动刷新不得覆盖被标记为 user source 的字段。
- 乐观并发校验防止两个写入者静默覆盖。

## Processing Pipeline

```text
Metadata -> Full text -> Abstract clean -> Summary -> Outline
```

阻塞式状态：

```text
discovered -> metadata_resolved -> parsed -> summarized -> outlined
```

Abstract clean 是显式、独立、非阻塞阶段。它保留 canonical Author Abstract，并将 cleaned view
保存为单独 artifact。失败只产生 warning；单独重建时不影响 Summary、Outline 或 PaperStatus。

每个阶段满足以下提交规则：

1. 读取已提交的上游状态和 artifact。
2. 在数据库事务之外执行 parser 或 provider 调用。
3. 校验完整输出。
4. 原子写入文件。
5. 通过短事务登记 artifact 并推进状态。

批量处理中单篇失败不取消其他论文。默认 update 从最后成功状态继续；rebuild 明确指定失效
边界。

## Full Text

Parser 输出统一 `ParsedPaper`，使 Summary 不依赖 GROBID TEI 或 PyMuPDF 的原始格式。
GROBID 提供更丰富的学术结构；PyMuPDF 提供不依赖外部服务的本地路径。两者都要求 PDF
具有文本层，OCR 不属于 Core 当前职责。

## Abstract

Canonical Abstract 属于 Paper metadata。Cleaned Abstract 只修复抽取边界、断词、错误换行和
明确的字符损坏，不做摘要、论点改写或事实补充。

Cleaned artifact 记录原文 hash、prompt hash 和模型。验证包括长度、数字和文本相似度；原文
改变后，旧 cleaned artifact 不再对外显示。

## Summary 和 Outline

Summary 使用经过 schema 校验的结构化 JSON。`auto` 策略根据上下文预算选择全文生成或
hierarchical evidence reduce；无论路径如何，最终 contract 相同。

Outline 只读取合法 Summary，不独立解释原始全文。这样 Summary 与 Outline 对论文问题、贡献
和证据的表达保持一致。

LLM 响应必须经过 JSON 解码和 Pydantic 校验。有限 repair 只修复 schema 形状，不能替代事实
校验或静默接受截断输出。

## Library Organization

Tag 是用户维护的无序分类，可组合筛选。Collection 是有顺序的论文集合，可保存阅读上下文。
Summary keywords 是生成内容，不自动转换为 Tag。

## 安全与隐私

- 数据默认保存在用户指定的本地 data directory。
- API key 不进入 YAML、数据库、日志或诊断 artifact。
- Web 不返回本地文件路径，只通过受校验的 API 提供 artifact。
- 所有文件解析必须阻止相对路径逃逸并验证 artifact 完整性。

## 非目标

- 多用户身份与权限系统
- 分布式 worker 调度
- 内置 OCR
- 自动下载受版权保护的论文
- 将外部 provider 响应直接作为稳定内部数据模型
