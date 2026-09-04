# Roadmap: Split Passagen Core

本文档定义第一阶段工作：将当前 `passagen-cli` 中可被多个入口复用的业务能力拆分为
`passagen-core`，使 CLI 和 Web 成为 Core 之上的两个独立适配器。此阶段只调整代码和
发布边界，不增加新的产品功能，也不重写已经稳定的 pipeline。

状态：Distribution、源码、测试、依赖和文档边界已经完成拆分；统一的 Core
run/diagnostic artifact store 仍作为后续迁移项保留。

## 背景

当前 `passagen-web` 已经依赖 `passagen-cli` 发布的 `passagen` Python 包，并通过
`CatalogService` 复用 paper、collection、tag 和 artifact 能力。底层业务逻辑没有形成
两套实现，但项目名称和依赖关系表达了错误的职责：Web 不应依赖一个 CLI 项目，Typer、
Rich 和终端执行日志也不应成为 Web 的间接依赖。

随着 collection research、exploration 和对话能力进入计划，LLM 不再只是 CLI 的实现
细节。继续按照“CLI 负责 LLM，Web 负责阅读”划分功能，会迫使 Web 调用 CLI、复制处理
逻辑，或者只能读取由其他进程预先生成的结果。

## 目标结构

```text
                    passagen-core
                         |
             +-----------+-----------+
             |                       |
       passagen-cli             passagen-web
```

依赖必须保持单向：

```text
passagen-cli -> passagen-core <- passagen-web
```

CLI 与 Web 不直接依赖彼此，不通过 subprocess 或 HTTP 调用对方。它们可以提供重叠的
用户功能，但必须调用相同的 Core service、事务和持久化规则。

## 包与发布边界

建议使用三个 distribution：

| Distribution | Python package | 职责 |
|---|---|---|
| `passagen-core` | `passagen` | 稳定模型、服务、存储和 provider |
| `passagen-cli` | `passagen_cli` | Typer 命令和终端适配 |
| `passagen-web` | `passagen_web` | FastAPI、后台执行和前端 |

`passagen-core` 继续提供 `passagen` import package，可以尽量保留 Web 当前的
`from passagen.catalog import ...` 导入。`passagen-cli` 继续注册 `passagen` 可执行命令，
但入口改为 `passagen_cli.app`。Distribution 名称和 Python package 名称不要求一致。

三个项目可以继续位于同一 workspace，开发环境使用本地 editable dependency；发布时 CLI
和 Web 分别声明兼容的 Core 版本范围。

## Core 职责

以下代码和资源归入 `passagen-core`：

- `domain`：Paper 状态、标识和值对象。
- `catalog`：Paper、collection、tag 和 artifact 的公开 facade。
- `storage`：SQLAlchemy model、repository、Alembic migration 和 artifact store。
- `parsing`、`prompting` 和内置 prompt resources。
- `external` 和 `providers`：外部协议、调用策略、指标和错误转换。
- `stages` 或后续统一的 processing application service。
- 配置中的稳定业务模型，例如 provider、parser 和 pipeline 参数。
- 业务错误、run contract、进度事件及诊断 artifact contract。

Core 不依赖 Typer、Rich、FastAPI、HTTP response、浏览器或终端。Core service 必须可以在
普通 Python 测试中直接构造和调用。

数据库 Schema 和 Alembic migration 只由 Core 拥有。CLI 与 Web 不保存自己的业务表定义，
也不能各自决定不同的级联、兼容或升级规则。

## CLI 职责

`passagen-cli` 保留：

- Typer composition root 和命令参数。
- 当前目录、`--config`、`--data-dir` 和环境变量的入口解析。
- Core 依赖的实例化和命令生命周期。
- Rich 表格、进度、warning、error 和退出码。
- CLI 自身的运行日志 handler。
- 面向自动化、批处理、数据库维护和 artifact 运维的命令。

CLI 不直接操作 SQLAlchemy Session，不实现 pipeline 状态转换，也不解析 provider response。

## Web 职责

`passagen-web` 保留：

- FastAPI route、request/response schema 和 HTTP 状态码。
- Web 配置、服务启动、origin protection 和 access log。
- 后台 runner 生命周期和任务进度 API。
- React 页面、交互状态和结果展示。
- Web 层的权限边界，包括诊断 artifact 是否允许查看。

Web 不调用 `passagen` 命令，不导入 `passagen_cli`，也不复制 Core 的业务校验。

## 配置边界

Core 定义 provider、pipeline 和数据路径所需的类型化配置 contract，但不决定配置来源。

- CLI 负责 YAML、环境变量和命令行 override 的合并，以及默认当前工作目录语义。
- Web 负责其服务监听地址、origin 和静态资源等 Web-only 配置。
- 两个入口最终都将经过校验的 Core 配置传给 application service。
- API key 只通过明确配置的环境变量读取，不能写入普通日志、数据库或诊断 artifact。

## 日志与诊断责任

拆包时必须区分普通运行日志、业务执行记录和大型诊断内容。

### Core 负责

- 使用标准库 `logging` 发送结构化业务事件，但不安装 handler，不调用
  `logging.basicConfig()`，也不决定日志级别和输出目的地。
- 创建稳定的 `run_id` 和 `call_id`。
- 持久化 processing run、LLM call、状态、token、模型、耗时和错误分类。
- 将完整 prompt、request、raw response、parsed response 和 validation error 保存为受管理的
  诊断 artifact。
- 生成与 UI 无关的结构化进度事件。
- 实现诊断数据保留、清理和级联删除规则。

LLM raw response 不是普通日志。它可能体积较大并包含论文正文、用户问题或未发表内容，
不能写入终端日志、Web access log 或 systemd journal。

建议将共享诊断数据放入 `data_dir`：

```text
data/
  runs/<run-id>/
    run.json
    llm/<call-id>/
      request.json
      prompt.txt
      response.txt
      parsed.json
      error.json
```

数据库中的 run 和 LLM call 表保存索引、状态、token 统计和 artifact 相对路径；大型内容
保存在文件中。任何路径都必须受现有 artifact 路径逃逸检查保护。

### CLI 负责

- 配置终端及 CLI 文件日志 handler。
- 将 Core 进度事件渲染为 Rich 进度和最终汇总。
- 输出本次运行的 LLM token 统计。
- 提供 run/diagnostics 的查看和清理命令；清理规则本身调用 Core maintenance service。

### Web 负责

- HTTP access log、服务器生命周期和后台 worker 日志。
- 将 Core run 状态和进度事件转换为轮询或 SSE API。
- 提供诊断信息查看或下载入口，并执行 Web 权限检查。
- 不重复保存 LLM response，不将完整 prompt 或回答写入 access log。

### 安全与保留

所有入口统一遵守：

- 永不保存 API key、Authorization header、完整环境变量或认证对象。
- prompt/raw response 保留策略可配置，并具有安全默认值。
- 删除 conversation、report 或 run 时，Core 明确定义关联诊断内容是否同时删除。
- 普通日志只记录 ID、状态、模型、token、耗时和诊断 artifact 路径。

## 拆分步骤

这是一个架构迁移阶段，按以下顺序完成，但不改变产品行为：

1. 建立 `passagen-core` distribution，并原样迁移当前 `passagen` 核心包和 resources。
2. 将 `passagen/cli` 移为 `passagen_cli`，建立仅依赖 Core 的 CLI distribution。
3. 修改 Web 依赖为 `passagen-core`，保持现有 `passagen` 导入和 API 行为。
4. 将 execution logging 拆为宿主 handler 与 Core run/diagnostic recorder。
5. 将 LLM 诊断从 CLI 工作目录逐步迁移到共享 `data_dir/runs`；对已有日志只读兼容，
   不静默删除。
6. 更新 workspace、锁文件、构建、发布和安装文档。
7. 在拆分稳定后再整理 service 名称或内部目录，不在本阶段同时重写 pipeline。

## 兼容要求

- `passagen` 命令名称和当前参数保持不变。
- 数据库和已有 artifact 可原地继续使用。
- 已发布的 migration 不修改；需要持久化 run/diagnostic 路径时新增 forward migration。
- Web route 和前端行为在本阶段保持不变。
- Core 的公开业务异常不泄漏 Typer 或 FastAPI 类型。
- CLI 和 Web 使用同一 Core 版本测试，不允许各自绑定不同的本地业务实现。

## 验收条件

- 可以分别构建 `passagen-core`、`passagen-cli` 和 `passagen-web`。
- CLI 安装后仍提供 `passagen` 命令，且不需要安装 Web。
- Web 安装后不依赖 Typer、Rich 或 `passagen-cli` distribution。
- CLI 和 Web 的现有测试全部通过，数据库和 artifact contract 未发生非计划变化。
- 架构测试确认 Core 不导入 `passagen_cli`、`passagen_web`、Typer、Rich 或 FastAPI。
- 同一个 Core operation 从 CLI 或 Web 发起时产生一致的 run、LLM call 和诊断 artifact。
- 普通日志和诊断 artifact 均不包含 API key。
- `data_dir` 被整体复制后，run 和 LLM 诊断仍可解析，不依赖原执行工作目录。

## 非目标

本阶段不实现：

- Collection 综述、探索或问答。
- Web 论文处理页面。
- 分布式任务队列或多 worker 调度。
- 向量数据库和 embedding。
- 对现有 pipeline 的算法重写。

完成本阶段后，再按
[`roadmap-collection-research-and-exploration.md`](../roadmap-collection-research-and-exploration.md)
实现 collection 级 LLM 能力。
