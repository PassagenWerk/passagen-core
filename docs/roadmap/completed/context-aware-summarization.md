# 上下文感知 Summary

**状态：已完成并持续维护**

Summary 生成支持三种策略：

- `full` 在论文适合上下文预算时发送经过校验的全文表示。
- `hierarchical` 创建语义块、提取结构化 evidence，再 reduce 为最终 Summary。
- `auto` 根据模型上下文、利用率、安全余量、prompt 开销和输出预留自动选择。

实现包含章节感知语义切块、可配置段落重叠、evidence cache、prompt hash、截断检测、token
统计、schema validation 和有限 repair。两条路径生成相同的 Structured Summary contract。
