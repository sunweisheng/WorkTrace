# WorkTrace Markdown 输出简化设计

> 历史说明：本文最初包含的工作流字段和分组步骤已被 [取消工作流概念并改进事件分组](workstream-free-event-grouping-design.md) 替代；去掉管理者总结的输出简化仍是现行实现。

> 状态：当前 Markdown 输出说明；完整输出契约以 [detailed-design.md](detailed-design.md) 和新事件分组设计为准。

## 1. 文档目标

本文档说明 WorkTrace 当前已经落地的“去掉管理者总结，直接输出事件清单”实现。

当前目标已经实现为：

- 不再生成管理者总结
- 不再在 Markdown 中写“给上级汇报的当日总结”段落
- 最终文件直接输出正式“工作事件日报”事件列表

本文档不讨论上游分段提炼、上下文扩展或跨会话事件合并。

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

1. 锚点窗口分段和会话内片段批处理
2. 片段级扩窗、附件和链接正文补充
3. 全日初步分组、Python 完整性校验、全部组标题发现和完整内容复核
4. 多成员内容重写与 `build_work_events(...)`

因此最终 `WorkEvent` 列表已经是当天相对稳定的结构化产物，不再需要额外总结层。

## 3. 当前实现

### 3.1 runner 主流程

当前 `runner.py` 中，在 `build_work_events(...)`、文件证据聚合和最终过滤之后调用：

- `event_store.replace_day(target_date, events, owner_display_name=self_identity.display_name)`

主流程已经不再调用任何“管理者总结”方法。

### 3.2 analyzer 契约

当前 analyzer 主契约中，管理者总结已经不属于日主流程必需接口。

当前日处理链路的 analyzer 任务包括：

- 锚点窗口分段
- 会话内片段批量分析
- 锚点失败回退
- 临时协作复核和个人事实复核
- 全日跨会话分组
- 全部组标题发现、完整内容复核和多成员内容重写

### 3.3 store 接口

当前 `stores/base.py` 中：

- `replace_day(...)` 接收 `target_date`、`events` 和可选 `owner_display_name`

当前 store 接口不再承载任何额外总结层输入；`owner_display_name` 只用于个人日报文件名。

## 4. 输出模型变化

### 4.1 `DayDocument`

当前 `DayDocument` 已经只保留：

- `date`
- `events`
- `generated_at`

不再包含任何总结层字段。

对应模型位于 `src/worktrace/models.py`。

### 4.2 Markdown store

当前 `stores/markdown.py` 中：

- front matter 仅包含日期、事件数、生成时间、生成器和 Skill 版本号
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
- 隐藏 `merge_meta` 保存参与方式英文键、消息证据指纹、同日会话指纹、文件标识、内容指纹、人工修订类型，以及可选来源事件 ID、来源负责人和下级修订类型
- `### 序号. 事件标题`
- `日期`
- `主要动作`
- `内容`
- `具体对象`
- `本人参与方式`
- 中文 `保留理由`
- `保留依据`
- 存在人工修订时显示配置定义的 `修订标记`
- `涉及文件`

团队汇总事件把“本人参与方式”替换为“协作方式”，并额外公开显示：

- `来源人员`
- 存在上游汇总时的 `来源负责人`

事件标题只显示在三级标题中，不在字段列表中重复输出。个人事件字段顺序为：日期、主要动作、内容、具体对象、本人参与方式、保留理由、保留依据、涉及文件。主要动作或参与方式为空时显示“未明确”。团队汇总的来源事件 ID 只写入隐藏 `merge_meta`。

隐藏信息格式：

```html
<!-- worktrace:merge_meta {"version":3,"self_relations":["initiated"],"evidence_fingerprints":["sha256:..."],"conversation_fingerprints":["sha256:..."],"file_keys":["sha256:..."],"manual_edit_type":"manual_modified","source_manual_edit_types":[],"content_fingerprint":"sha256:...","source_report_owners":["部门负责人"],"source_event_ids":["事件ID"]} -->
```

`evidence_fingerprints` 由 Python 对每个来源消息 ID 分别计算 SHA-256，`conversation_fingerprints` 由目标日期和来源会话 ID 计算，`file_keys` 由去参数后的链接或附件 ID 计算 SHA-256。注释不得包含原始消息、会话、用户 ID。只有文件名而没有稳定链接或附件 ID 时不生成文件标识。

读取器仍能读取旧 Markdown 中重复的“事件标题”和可见的“来源事件 ID”，也兼容 v1、v2 隐藏信息。v2 空会话证据事件和隐藏信息损坏但正文完整的事件标记为“人工修订，类型无法确认”；有效 v1 缺少会话证据且没有修订标记时仍停止多人合并。外部编辑器新增的完整标准事件标记为“人工新增”，v3 可见业务字段改变时标记为“人工修改”。删除事件直接消失，不保存其编号、内容、指纹、删除数量或数量差值。不批量改写历史文件。

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

- `src/worktrace/runner.py`
- `src/worktrace/collected_merge.py`
- `src/worktrace/stores/base.py`
- `src/worktrace/stores/markdown.py`
- `src/worktrace/models.py`

## 8. 当前状态总结

截至当前版本，WorkTrace 的最终输出链路已经简化为：

- 构建最终 `WorkEvent`
- 写入正式“工作事件日报”事件列表
- 保留隐藏机器字段以支持回读、合并和后续校验

系统不再在主流程中生成管理者总结，Markdown 文件保留正式事件列表和必要的机器可读注释。
