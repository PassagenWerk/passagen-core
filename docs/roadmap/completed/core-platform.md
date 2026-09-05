# Core 平台

**状态：已完成并持续维护**

Passagen Core 当前提供：

- 内容寻址的 PDF 导入和重复检测。
- Canonical paper identity、metadata source tracking 和用户值优先级。
- SQLite migration、repository transaction 和 managed artifact 完整性检查。
- 通过稳定 `ParsedPaper` contract 提供 GROBID 和 PyMuPDF 解析。
- 显式 Metadata、Full text、Abstract clean、Summary 和 Outline 阶段。
- 可恢复 processing run、冲突检测、progress event、warning 和 failure。
- 经过 LLM response validation 的结构化 Summary 和 Outline schema。
- Paper、Tag、有序 Collection 和 PDF/artifact 的 Catalog service。
- CLI 和 Web 共同使用的 settings。

当前行为以用户文档和开发文档为准，不以历史 milestone 描述作为 contract。
