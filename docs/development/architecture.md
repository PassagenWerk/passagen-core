# Passagen Core 架构

Passagen Core 是 CLI 和 Web 共同依赖的业务与持久化层。它不包含 Typer 命令、FastAPI schema
或浏览器组件。

## 依赖方向

```text
passagen-cli        passagen-web
      \                 /
       \               /
        public Core services
          -> stages / processing / catalog
          -> providers -> external
          -> storage / parsing / domain
```

依赖只能从入口适配器指向 Core。Core 不导入 CLI 或 Web；领域模型不依赖 HTTP、终端或数据库
展示细节。

## 模块职责

| 路径 | 职责 |
|---|---|
| `config/` | 共享 settings、YAML 与环境变量合并、prompt path 解析。 |
| `domain/` | Paper、PaperStatus、metadata 值对象和标识规范化。 |
| `storage/` | SQLAlchemy models、transaction、repository、migration 和 artifact 索引。 |
| `parsing/` | `ParsedPaper` contract、本地 parser 和 PDF metadata 提取。 |
| `external/` | HTTP transport、外部 provider DTO 解码和可用性探测。 |
| `providers/` | Provider 使用策略、fallback、LLM 调用统计和上下文预算。 |
| `stages/` | 单篇 stage、update 编排、前置条件、校验和 artifact 提交。 |
| `processing/` | 持久化 run、冲突控制、worker-facing execution 和 progress event。 |
| `catalog/` | 论文、Tag、Collection 和 artifact 的入口无关应用 API。 |

## Public Boundaries

CLI 和 Web 应通过 `passagen.catalog`、`passagen.processing` 与公开 stage entry point 使用
Core。适配器不得导入 ORM model、直接创建 Session、扫描 artifact 目录或复制业务 schema。

新增共享能力时先建立普通 Python contract，再由 CLI 映射为命令、由 Web 映射为 HTTP。

## 数据与事务

SQLite transaction 只覆盖短时间的状态读写。PDF parser、metadata provider 和 LLM 请求不得
在写事务持有期间执行。

文件 artifact 使用临时文件完成写入和校验，再原子替换目标文件，最后登记数据库索引。失败时
保留最后成功状态，不登记部分输出。

## Processing

`ProcessingService` 创建持久化 run 并调用 `update_papers`。同一 Paper 不能同时属于多个活动
run。进程重启时，遗留 queued/running run 被标记为 interrupted。

`UpdateEvent` 是入口无关的进度 contract。CLI 将其显示为终端进度，Web 将其保存并通过 API
读取。业务 service 不依赖 Rich、FastAPI 或 React。

Abstract clean 在 rebuild 顺序中有独立位置，但没有 PaperStatus checkpoint。其 warning 被
记录在 run result，不能转化为整篇 Paper 的失败。

## 配置边界

Core 定义字段、默认值和校验。CLI 与 Web 决定 `data_dir`、显式 config path 和宿主进程日志。
共享字段见[配置文档](../user/configuration.md)。

## 错误边界

Core 暴露稳定的领域与应用错误。CLI 转换为消息和退出码，Web 转换为 HTTP status 和 error
payload。适配器不得改变 transaction、重试、失效或恢复语义。

## 测试边界

- Domain、storage、provider、stage 和 catalog 行为由 Core tests 覆盖。
- 外部服务使用 fake transport 或固定响应，默认测试不访问网络。
- CLI/Web 只重复覆盖各自的参数、HTTP 和展示 contract。
- Architecture tests 防止适配器依赖方向倒置。

相关仓库：

- Passagen CLI 仓库（同级目录 passagen-cli）
- Passagen Web 仓库（同级目录 passagen-web）
