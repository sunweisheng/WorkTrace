# WorkTrace Markdown 输出简化设计

## 1. 文档目标

本文档说明 WorkTrace 当前已经落地的“去掉管理者总结，直接输出事件清单”实现。

当前目标已经实现为：

- 不再生成管理者总结
- 不再在 Markdown 中写“给上级汇报的当日总结”段落
- 最终文件直接输出正式“工作事件日报”事件列表

本文档不讨论首轮会话提炼、会话内扩窗重跑或跨会话事件合并。

## 2. 当前背景

### 2.1 旧链路的问题

旧思路是在得到最终事件列表后，再额外执行一次“管理者总结生成”。

然后把“总结段落 + 事项列表”一起写入 Markdown。

这种做法的问题是：

1. 多一次 LLM 调用
2. 总结和事件列表可能表达不一致
3. 输出链路多一层额外不确定性

### 2.2 新主流程已经不需要总结层

当前主流程在写 Markdown 之前，已经完成：

1. 单会话事件提炼
2. 会话内扩窗与附件补充
3. 全日跨会话分组归并
4. `build_work_events(...)`

因此最终 `WorkEvent` 列表已经是当天相对稳定的结构化产物，不再需要额外总结层。

## 3. 当前实现

### 3.1 runner 主流程

当前 [runner.py](/Users/sunweisheng/Documents/GitHub/WorkTrace/src/worktrace/runner.py) 中，在 `build_work_events(...)` 之后直接调用：

- `event_store.replace_day(target_date, events, owner_display_name=self_identity.display_name)`

主流程已经不再调用任何“管理者总结”方法。

### 3.2 analyzer 契约

当前 analyzer 主契约中，管理者总结已经不属于日主流程必需接口。

当前日处理链路只依赖：

- 单会话分析
- 全日跨会话分组

### 3.3 store 接口

当前 [stores/base.py](/Users/sunweisheng/Documents/GitHub/WorkTrace/src/worktrace/stores/base.py) 中：

- `replace_day(...)` 接收 `target_date`、`events` 和可选 `owner_display_name`

当前 store 接口不再承载任何额外总结层输入；`owner_display_name` 只用于个人日报文件名。

## 4. 输出模型变化

### 4.1 `DayDocument`

当前 `DayDocument` 已经只保留：

- `date`
- `events`
- `generated_at`

不再包含任何总结层字段。

对应模型位于 [models.py](/Users/sunweisheng/Documents/GitHub/WorkTrace/src/worktrace/models.py)。

### 4.2 Markdown store

当前 [markdown.py](/Users/sunweisheng/Documents/GitHub/WorkTrace/src/worktrace/stores/markdown.py) 中：

- front matter 仅包含日期、事件数、生成时间和生成器
- 正文包含 `# 工作事件日报 · YYYY-MM-DD`、`## 事件列表` 和逐条编号事件
- `retention_reason` 作为内部枚举保存在隐藏注释中，对外展示为中文“保留理由”

已完全去掉：

- `## 给上级汇报的当日总结`
- summary 正文段落

## 5. 当前 Markdown 结构

当前 Markdown 文件结构为：

1. front matter
2. `# 工作事件日报 · YYYY-MM-DD`
3. `## 事件列表`
4. 逐条编号事件块
5. 底部生成说明

每条事件块当前包含：

- HTML 注释包裹的 `event_id`
- 隐藏注释保存内部 `retention_reason` 枚举
- `### 序号. 事件标题`
- `日期`
- `事件标题`
- `内容`
- `具体对象`
- 中文 `保留理由`
- `保留依据`
- `涉及文件`

底部生成说明包含：

- `生成时间`
- `来源: 飞书沟通记录自动整理`
- `隐私声明: 仅含与本人直接相关的工作事件，不含原始聊天记录`

## 6. 当前实现原则

### 6.1 事件清单就是最终产物

当前设计把结构化事件清单直接视为日报主产物，不再额外生成自由文本总结。

事件列表本身就是面向人工阅读的正式日报，同时不改变底层结构化事件输出。

### 6.2 输出结构优先

相比再次让 LLM 写总结：

- 结构化事件更稳定
- 更容易核查
- 更适合作为后续导出或聚合的上游输入

### 6.3 减少额外调用

去掉管理者总结后，主流程少了一次独立 LLM 调用，有助于降低耗时与不确定性。

## 7. 当前代码落点

本设计当前主要落在以下文件：

- [runner.py](/Users/sunweisheng/Documents/GitHub/WorkTrace/src/worktrace/runner.py)
- [stores/base.py](/Users/sunweisheng/Documents/GitHub/WorkTrace/src/worktrace/stores/base.py)
- [stores/markdown.py](/Users/sunweisheng/Documents/GitHub/WorkTrace/src/worktrace/stores/markdown.py)
- [models.py](/Users/sunweisheng/Documents/GitHub/WorkTrace/src/worktrace/models.py)

## 8. 当前状态总结

截至当前版本，WorkTrace 的最终输出链路已经简化为：

- 构建最终 `WorkEvent`
- 写入正式“工作事件日报”事件列表
- 保留隐藏机器字段以支持回读、合并和后续校验

系统不再在主流程中生成管理者总结，Markdown 文件保留正式事件列表和必要的机器可读注释。
