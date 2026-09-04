# Passagen 开发 Roadmap

本文档描述 Passagen 首个可用版本的开发顺序。开发采用 Python + SQLite，以可独立运行和验收的纵向里程碑推进。

## 首版目标

首版完成以下闭环：

```text
本地 PDF
  -> 扫描与去重
  -> 导入受管理 PDF 存储
  -> 论文结构解析
  -> 元数据补全
  -> 英文结构化摘要
  -> 英文 outline
  -> 本地归档和状态查询
```

首版完成的判断标准：用户可以通过一次 `passagen run <directory>` 处理一批带文本层的 PDF；任务失败后可以查看原因并重试；重复执行不会重复归档论文或重复调用 LLM。

## 技术基线

- Python 3.12+
- `uv`：项目、依赖和虚拟环境管理
- `Typer`：CLI
- `Pydantic` 和 `pydantic-settings`：Schema 与配置
- `PyYAML`：分区 YAML 配置解析
- SQLAlchemy 2.0：typed ORM、repository 和短 Session 事务
- Alembic：只向前执行的 Schema migration，并同步 SQLite `PRAGMA user_version`
- `httpx`：GROBID、Crossref、arXiv 和 OpenAI-compatible LLM API 请求
- Python 标准库 XML parser：GROBID TEI XML 解析
- `PyMuPDF`：轻量及降级 PDF 解析
- `Rich`：CLI 输出
- `pytest` 和 `respx`：测试与 HTTP mock

首版使用同步执行模型，每次处理一篇论文。实际验证存在吞吐瓶颈后，再考虑有限并发。

## 数据库 Schema 策略

数据库 Schema 采用以下规则：

- `SCHEMA_VERSION` 保持为 `1`，当前完整表结构直接维护在初始 Schema 中。
- Roadmap 阶段、代码模块或配置变化不触发 Schema version 递增。
- 已提交的 Alembic revision 不再修改；表结构变化通过新的 forward migration 完成。
- 现有无 Alembic 标记的 Schema v1 经校验后原地 stamp，不要求删除或重建数据库。

满足以下任一条件后开始维护只向前执行的 migration：

- 发布首个承诺保留用户数据的版本。
- 数据库已经被真实用户或团队共享使用。
- 重新导入和处理数据的成本已经不可接受。
- 项目明确承诺旧数据库可升级到新程序。

进入该阶段后，不再修改已经发布的 migration；只有持久化表结构变化才增加 `SCHEMA_VERSION`。数据库 Schema、artifact Schema 和 prompt Schema 分别独立版本化。

## M0：项目骨架

### 工作内容

- 使用 `uv` 初始化 Python 项目和锁文件。
- 建立 `src/passagen` 包、CLI 入口和测试目录。
- 实现配置加载，支持配置文件、环境变量和命令行覆盖。
- 默认将不受 Git 跟踪的分区式 `passagen.yaml` 放在数据目录内（`<data_dir>/passagen.yaml`，默认 `./data/passagen.yaml`），仓库提供 `passagen.example.yaml` 模板，数据库和 artifact 同样写入 `data/`；`data_dir` 只能由 `--data-dir` 命令行参数指定。
- 建立统一日志和用户可读错误输出。
- 配置格式化、静态检查和测试命令。

### 交付物

- `passagen --help`
- `passagen config check`
- 基础配置模型
- 可运行的测试框架

### 验收条件

- 全新环境中可通过 `uv sync` 安装。
- `uv run passagen --help` 正常退出。
- 默认运行不读取或创建用户 Home 下的 Passagen 配置和数据目录。
- 显式 `--config`、`--data-dir` 和环境变量仍可覆盖当前目录默认值。
- 配置错误不会输出 Python traceback，而是给出明确字段和原因；调试模式除外。

## M1：数据模型与 SQLite

### 工作内容

- 定义 `Paper`、`Artifact`、`ProcessingRun` 和 `LLMCall` 数据模型。
- 建立可重建的初始数据库 Schema，并保留未来前向 migration 的执行机制。
- 为标准化 DOI、arXiv ID 和 PDF SHA-256 建立唯一约束。
- 实现不可变的内部 `paper_id`。
- 定义任务状态及合法状态转换。
- 实现数据库初始化和版本检查。

### 交付物

- `passagen db init`
- `passagen db status`
- SQLite Schema v1
- Repository/storage 层的最小实现

### 验收条件

- 重复 DOI、arXiv ID 或 SHA-256 无法产生两条论文记录。
- 更新论文元数据不会改变 `paper_id`。
- 非法状态跳转会被拒绝并记录原因。
- 数据库初始化具备幂等性，并拒绝高于当前程序版本的 Schema。

## M2：扫描、哈希与基础管理

状态：已实现。

### 工作内容

- 递归或非递归扫描指定目录中的 PDF。
- 将 PDF 流式复制到临时文件并计算 SHA-256，避免将整个文件载入内存。
- 按 `pdfs/<sha256-prefix>/<sha256>.pdf` 原子写入 `data_dir` 下的内容寻址存储。
- 为受管理 PDF 建立 `original_pdf` artifact，路径相对 `data_dir`；不保存扫描源路径。
- 已存在相同 SHA-256 时复用受管理文件，不重复复制。
- 保存原文件名、文件大小和导入时间作为审计信息，不将原文件名用作文件定位依据。
- 实现论文列表、详情和状态过滤。

### 交付物

- `passagen scan <directory>`
- `passagen list [--status STATUS]`
- `passagen show <paper-id>`

### 验收条件

- 对同一目录重复执行 `scan` 不会增加重复记录。
- 文件名不同但内容相同的 PDF 会被识别为重复文件。
- 导入成功后移动或删除扫描源文件，不影响 `parse` 和后续处理。
- 数据库中的 PDF artifact 不包含扫描目录的绝对路径。
- 复制或数据库登记失败不会留下可见的半成品 artifact。
- 损坏文件、无权限文件和非 PDF 文件不会中断整个扫描过程。
- CLI 能显示新增、跳过和失败文件的数量。

## M3：论文标识与基础元数据

状态：已实现。

### 工作内容

- 只从 `original_pdf` artifact 指向的受管理文件读取 PDF，不回退到扫描源路径。
- 使用 PyMuPDF 读取 PDF metadata/XMP 和前几页文本，不执行全文结构解析。
- metadata 缺失时利用前几页字体/坐标版式和原文件名候选提取 title 与 authors，并过滤出版方封面、机构和 URL。
- 从 metadata、首页和前几页中提取并标准化 DOI 与 arXiv ID。
- 实现 Crossref REST API 客户端，使用 DOI 精确查询元数据。
- 实现 arXiv API 客户端，使用规范化 arXiv ID 精确查询元数据。
- 实现默认关闭的 GROBID `processHeaderDocument` fallback，在本地身份信息不足或 Crossref 标题冲突时解析 TEI header。
- 同时存在 DOI 和 arXiv ID 时查询两者，并按 `user > crossref > arxiv > grobid > pdf` 合并字段。
- 保存 title、authors、year、venue、source URL、标识和字段来源。
- GROBID、Crossref 或 arXiv 未命中、限流或不可用时使用已有元数据继续处理。
- 没有 DOI/arXiv ID 时仍推进到 `metadata_resolved`，不执行标题模糊查询。

### 交付物

- `passagen metadata <paper-id> [--force]`
- `passagen update [paper-id] [--force]`，将单篇或全部 Paper 推进到当前最新实现阶段，或强制重建全部阶段
- 轻量 PDF 标识提取器
- GROBID header、Crossref 和 arXiv API 客户端
- 字段来源与元数据持久化

### 验收条件

- DOI 只通过 Crossref 精确查询，arXiv ID 只通过 arXiv API 精确查询。
- 不根据模糊标题自动定位或合并论文。
- 任一 API 超时、限流和未命中都不会阻塞后续全文解析。
- 每个元数据字段保存 `user`、`crossref`、`arxiv`、`grobid` 或 `pdf` 来源。
- PDF metadata 为空且未找到标识时也能保存本地最小结果。
- 使用 HTTP mock 覆盖 GROBID、Crossref 和 arXiv 的成功、未命中、限流及服务错误。
- `update <paper-id>` 只推进指定 Paper；省略 ID 时处理全部落后记录并跳过已完成项。
- 批量 update 隔离单篇失败，输出 updated/skipped/failed 汇总并以非零状态报告部分失败。

## M4：全文结构解析

状态：已实现。

### 工作内容

- 定义统一 `PaperParser` 接口和 `ParsedPaper` 模型。
- 实现 GROBID 健康检查和 `processFulltextDocument` 调用。
- 将 TEI XML 转换为统一的 metadata、sections 和 references 结构。
- 保留章节对应的页码或坐标信息。
- 实现 PyMuPDF 全文解析器和自动降级。
- 检测无文本层、解析结果过短和异常页面顺序。
- 保存 `extracted.json`，避免后续阶段重复解析 PDF。

### 交付物

- `passagen parse <paper-id> [--parser grobid|pymupdf|auto]`
- GROBID 默认后端
- PyMuPDF 降级后端
- 统一解析结果 Schema

### 验收条件

- GROBID 可用时能够获得标题、章节和参考文献结构。
- GROBID 不可用时，`auto` 模式自动降级并记录实际 parser。
- 扫描版 PDF 会以明确的 `no_text_layer` 原因失败。
- 对选定的单栏、双栏和 arXiv PDF 样本建立固定回归测试。

## M5：英文结构化摘要

状态：已实现。

### 工作内容

- 用 Pydantic 定义版本化的结构化摘要 Schema，并允许可选字段为 `null`。
- 实现 OpenAI-compatible LLM provider。
- 按章节和上下文窗口限制切分输入。
- 对长论文先生成章节事实摘要，再合成为最终摘要。
- 要求关键实验结果保留 evidence pages。
- 保存原始响应、中间摘要和规范化 `summary.json`。
- 从 JSON 导出便于阅读的 `summary.yaml`。

### Schema 校验与修复

1. 解析 JSON 并执行 Pydantic 校验。
2. 本地移除 code fence，补齐缺失的可空字段，并拒绝未知结构。
3. 本地修复失败后，将校验错误交给 LLM 修复。
4. LLM 修复最多重试两次。
5. 最终失败时保留原始响应和完整校验错误。

### 交付物

- `passagen summarize <paper-id> [--force]`
- 通用 summary Schema v2
- OpenAI-compatible provider
- 分块、合并、校验和修复流程

### 验收条件

- 成功产物始终通过当前版本 Pydantic Schema 校验。
- 论文未提供的可选信息保存为 `null` 或空列表，不强制生成内容。
- 相同成功分块在重试时不会重复调用 LLM。
- 记录 provider、模型、prompt/Schema 版本、token 用量和错误。
- 使用固定 LLM 响应测试合法输出、格式损坏、类型错误和修复失败。

## M6：英文 Outline

状态：已实现。

### 工作内容

- 只读取通过校验的 `summary.json` 生成英文 outline。
- 固定 Introduction、Background、Design、Implementation、Evaluation 和 Related Work 章节。
- 对 `null` 和空列表对应内容执行省略，不允许补充摘要之外的事实。
- 保存生成所使用的 summary、prompt 和模型版本。
- 将结果保存为 `outline.md`。

### 交付物

- `passagen outline <paper-id> [--force]`
- 英文 outline prompt
- Markdown renderer

### 验收条件

- 没有合法 `summary.json` 时拒绝生成 outline。
- outline 中的事实均可在结构化摘要中找到对应内容。
- summary 更新后，旧 outline 会被标记为过期。
- 空章节不会产生模型臆测的占位内容。

## M7：完整流水线与恢复能力

状态：已实现。

### 工作内容

- 实现各阶段编排和状态持久化。
- 随已实现阶段扩展 `update [paper-id]` 的目标，不改变其单篇/全量接口。
- 阶段失败时保留最后成功状态，由调用者重新执行 `update`；不在程序内自动重试。
- 保持幂等执行和 `--force` 从 metadata 显式重建。
- 在受管理存储中保存 PDF，并在论文 artifact 目录中保存解析结果、摘要和 outline。
- 中断时终止当前 processing run，不推进 Paper 状态，并依靠短事务和原子文件写入安全退出。
- 提供单篇论文和批量处理进度。

### 交付物

- `passagen run <directory>`

### 验收条件

- `run` 可以从空数据库完成扫描、解析、摘要、outline 和归档。
- 对同一目录再次运行不会重复调用 Crossref、arXiv 或 LLM。
- 任意阶段失败后，再次执行 `update` 会从最后成功阶段恢复。
- `Ctrl+C` 不会推进 Paper 状态或损坏数据库。
- 单篇论文处理具备不依赖真实外部 API 的端到端测试。

## M8：首版发布准备

状态：已实现；真实 GROBID/LLM 联调作为发布前人工验收执行。

### 工作内容

- 补充安装、GROBID Docker、配置和常见错误文档。
- 提供示例配置，但不包含 API key。
- 检查日志和数据库中不存在 API key。
- 增加数据库备份和产物目录检查命令。
- 在 Linux 环境执行干净安装和真实论文验收。
- 固定首版 Schema、prompt 和数据库版本。

### 交付物

- `README.md`
- 示例配置
- GROBID 启动说明
- 首个版本标签

### 验收条件

- 新环境可仅根据文档完成安装和一次论文处理。
- GROBID 不可用或 LLM key 缺失时错误信息明确；Crossref/arXiv 不可用时降级到本地元数据。
- 用户数据目录可以整体复制并在另一环境中继续使用。
- 所有自动化测试、静态检查和数据库 Schema 版本检查通过。

## 测试策略

### 单元测试

- DOI 和 arXiv ID 标准化。
- SHA-256 和去重规则。
- 状态转换。
- TEI XML 到内部章节模型的转换。
- Pydantic Schema 校验和本地修复。
- outline 输入过滤。

### 集成测试

- 当前完整 Schema v1、事务、外键和唯一约束。
- GROBID、Crossref、arXiv 和 LLM 的 mock HTTP 交互。
- GROBID 失败后的 PyMuPDF 降级。
- 分块成功后合并失败的断点续跑。
- prompt 或 Schema 升级后的产物过期判断。

### 端到端测试

- 使用少量可提交到测试目录的 PDF fixture，或在测试时生成最小 PDF。
- 外部 API 默认使用固定响应，测试结果不得依赖网络和模型随机性。
- 真实 GROBID 和真实 LLM 测试单独标记，不进入默认测试集。

## 版本优先级

### v0.1：可处理单篇论文

完成 M0-M5。能够从 PDF 生成经过校验的英文结构化摘要。

### v0.2：完整产物闭环

完成 M6-M7。能够批量生成英文 outline，并支持失败恢复和幂等执行。

### v1.0：稳定的个人 CLI 工具

完成 M8。安装、迁移、配置、错误处理和真实论文验证达到可日常使用状态。

## 暂缓事项

以下内容不进入首版 Roadmap：

- Web UI 和多用户管理；
- OCR 和扫描版 PDF 支持；
- 自动搜索或下载论文；
- 向量数据库和语义检索；
- 多模型自动路由；
- 异步批处理和分布式任务队列；
- Rust 重写或 Python/Rust 混合模块。

只有在首版实际使用暴露明确需求后，才为这些能力建立新的里程碑。
