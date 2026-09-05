# Passagen Python 代码风格

本文档定义 Passagen 的 Python 编码与测试约定，并使用两套互补的类型检查器和统一检查命令。

## 基线

- 使用 Python 3.12 及以上版本。
- 使用 `uv` 管理依赖、虚拟环境和命令执行。
- Ruff 同时负责格式化、导入排序和 lint，行宽为 100。
- Basedpyright 使用 standard 模式检查 `src/` 和 `tests/`，mypy 对同一范围进行交叉检查。
- pytest 负责测试，pytest-cov 默认收集分支覆盖率。
- 不直接维护工具生成的格式；以 `pyproject.toml` 为唯一配置入口。

常用命令：

```shell
uv sync --frozen
uv run ruff format .
uv run ruff check .
uv run basedpyright
uv run mypy
uv run pytest
uv run python scripts/pytest_parallel.py
```

默认 pytest 配置排除标记为 `slow` 的完整 CLI 工作流。本地按需运行慢测试或全部测试：

```shell
uv run python scripts/pytest_parallel.py -m slow -o addopts=""
uv run python scripts/pytest_parallel.py -o addopts=""
```

提交钩子安装命令：

```shell
uv run pre-commit install --hook-type pre-commit --hook-type pre-push
```

## 模块与导入

- 新模块在需要延迟解析注解时使用 `from __future__ import annotations`。现有模块已经采用该约定，新代码保持一致。
- 标准库、第三方依赖和项目内导入分组，由 Ruff 自动排序。
- 跨子包依赖使用 `passagen...` 绝对导入；相对导入只用于短小的包内重新导出。
- 文件路径使用 `pathlib.Path`，不要在业务代码中拼接路径字符串。
- 避免通配符导入。包级 `__init__.py` 只重新导出稳定、常用的公开入口。

## 命名

- 模块、函数和变量使用 `snake_case`，类型使用 `PascalCase`，常量使用 `UPPER_SNAKE_CASE`。
- 名称使用设计文档中的领域词汇，例如 `Paper`、`Artifact`、`ProcessingRun` 和 `PaperStatus`，不要为同一概念引入近义别名。
- 布尔值使用能表达判断结果的名称，例如 `is_initialized`、`has_text_layer`。
- 私有实现使用单下划线前缀。只有跨模块需要稳定调用的对象才作为公开入口。
- 异常名称以错误语义结尾，例如 `ConfigError`、`DatabaseVersionError`、`InvalidStatusTransition`。

## 类型与数据模型

- 新增或修改的函数必须标注参数和返回类型，包括测试 helper。
- 优先使用内建泛型和联合类型，例如 `list[str]`、`dict[str, object]` 和 `Path | None`。
- 不使用无边界的 `Any` 绕过类型检查。解析动态配置或第三方响应时可以在输入边界短暂使用 `Any`，但应尽快校验并转换为具体类型。
- 纯领域值对象优先使用 dataclass；不可变对象使用 `frozen=True`，明确不需要动态属性时使用 `slots=True`。
- 外部配置、LLM 结构化输出和持久化 artifact Schema 使用 Pydantic，因为这些边界需要运行时校验和可导出的 JSON Schema。
- 可替换的外部能力使用小型 `Protocol`，例如 PDF parser、metadata client 和 LLM provider。Protocol 应描述调用方真正需要的最小接口。
- 枚举值需要持久化或出现在配置中时使用字符串枚举，并把字符串值视为稳定 contract。

## 函数与类

- 函数只承担一个可描述的职责。编排函数可以顺序调用多个步骤，但不应同时实现某个步骤的解析或业务算法。
- 先保留在一个函数内；只有逻辑可独立测试、跨位置复用或代表明确领域步骤时才提取 helper。
- 依赖通过参数或构造函数显式传入。不要通过模块级可变单例隐藏数据库连接、HTTP client 或 provider。
- 对外部资源使用上下文管理器，并在同一抽象层完成获取与释放。
- CLI command 只负责参数转换、调用应用入口和呈现结果，不直接编写 SQL、解析 PDF 或调用 LLM。
- `utils` 只允许放无业务语义的通用能力。包含 Paper、summary、parser 或 pipeline 规则的代码必须回到对应业务包。

## 错误处理

- 在错误产生的层定义有业务含义的异常，在边界层将底层异常转换为该异常，并使用 `raise ... from exc` 保留原因链。
- CLI 捕获预期的配置、输入和业务错误，输出可执行的修复信息并返回非零状态；debug 模式之外不向用户输出 traceback。
- 不使用裸 `except`。事务边界允许 `except Exception` 做 rollback，但必须立即重新抛出。
- 不以 `None` 表示无法区分的失败原因。不存在是正常结果时可以返回 `None`，执行失败应抛出异常。
- 错误信息包含操作、对象和必要上下文，但不得包含 API key、完整 LLM secret 或不必要的论文正文。

## 日志与输出

- Core 使用 `logging`，不直接 `print`，也不安装 handler。CLI 和 Web 分别负责用户输出。
- INFO 记录阶段、对象标识和结果摘要；DEBUG 才能记录请求细节或较长内容。
- 日志与数据库中禁止写入 API key。
- 大型 LLM 响应和解析 artifact 应写入受管理的文件，再在日志中记录路径，不把完整内容塞入日志。

## 注释与文档字符串

- 代码可直接表达意图时不添加注释。
- 注释解释约束、取舍和不明显的失败模式，不复述下一行代码。
- 稳定公开入口、Protocol 和具有重要 invariant 的领域类型应有简短 docstring。
- 私有短 helper 不强制写 docstring。
- 修改外部 contract 时优先更新架构或设计文档，而不是只在实现旁留下说明。

## 测试风格

- 测试名称描述可观察行为，例如 `test_rejects_newer_database_schema`。
- 每个测试保持 Arrange、Act、Assert 的清晰分段，通常用空行区分，不需要写分段注释。
- 文件系统测试使用 `tmp_path`，环境变量使用 `monkeypatch`，外部 HTTP 和 LLM 默认使用固定响应；默认测试不得访问网络。
- 使用 `pytest.raises` 断言具体异常。错误消息属于 contract 时再断言消息内容。
- 优先验证公开行为；只有复杂领域算法才需要白盒单元测试。
- 测试数量增长后按 `unit/`、`component/`、`integration/`、`e2e/` 分层，并让目录结构对应 `src/passagen` 的业务边界。
- 真实 GROBID、真实 LLM 和大 PDF 测试必须显式标记为 `slow` 或 integration，不进入默认快速测试。
- fixture 按所有者归类；跨模块共享的 PDF、TEI、HTTP 和 LLM 响应放在 `tests/fixtures/`，构造 helper 放在 `tests/support/`。

## 变更检查

提交代码前至少执行：

```shell
uv run ruff check .
uv run ruff format --check .
uv run basedpyright
uv run mypy
uv run python scripts/pytest_parallel.py
```

直接运行 `uv run pytest` 便于串行调试快速测试。pre-push 运行快速测试，CI 将 fast 与 slow
分成独立 job 并覆盖完整测试集。pytest-xdist 默认使用可用物理核心的一半且最多 8 个
worker；可通过 `PASSAGEN_PYTEST_WORKERS` 显式覆盖。

修复应优先消除根因，不通过扩大忽略规则、无理由增加 `# type: ignore` 或降低检查级别让门禁通过。
