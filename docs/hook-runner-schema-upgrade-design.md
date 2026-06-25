# WorkTrace Hook Runner Schema / Output 稳定性升级设计

## 1. 文档目标

本文档用于给 WorkTrace 当前 `HookAnalyzer + hook_runner.py` 链路提供一套可直接落地的升级方案，使其具备类似 MyAgentWiki `agent_cli_hook.py` 的两项核心能力：

- 在调用模型前显式声明本轮期望的输出 schema
- 在模型输出回流后做更稳健的 JSON 提取、解包和归一化

本文档只讨论 Hook 通道升级，不讨论首轮 prompt 语义、事件合并策略、缓存策略或飞书抓取逻辑。

## 2. 当前现状

### 2.1 当前调用链

WorkTrace 当前默认分析链路如下：

1. `HookAnalyzer` 生成 prompt
2. `HookAnalyzer` 通过 `hook_command` 启动外部子进程
3. `hook_runner.py` 从 `stdin` 读取 prompt
4. `hook_runner.py` 调 `codex exec`
5. `hook_runner.py` 读取输出文件内容
6. `hook_runner.py` 将内容原样写回 `stdout`
7. `HookAnalyzer` 从子进程 `stdout` 读取文本并直接 `json.loads(...)`
8. 对应 protocol parser 解析业务结构

当前关键文件：

- [src/worktrace/analyzers/hook.py](/Users/sunweisheng/Documents/GitHub/WorkTrace/src/worktrace/analyzers/hook.py:1)
- [src/worktrace/hook_runner.py](/Users/sunweisheng/Documents/GitHub/WorkTrace/src/worktrace/hook_runner.py:1)
- [src/worktrace/analyzers/codex.py](/Users/sunweisheng/Documents/GitHub/WorkTrace/src/worktrace/analyzers/codex.py:1)

### 2.2 当前优点

- 结构简单，容易理解
- hook 后端和主流程解耦
- 可以替换成任意外部命令
- 本地额外处理少，单次链路开销低

### 2.3 当前问题

当前链路的主要问题不是“不能跑”，而是“结构稳定性不足”。

#### 2.3.1 Hook 通道没有显式 output schema

`HookAnalyzer` 当前只把 prompt 交给 hook，不会把“本轮必须返回什么形状的 JSON”传递给 hook runner。  
这导致：

- `hook_runner.py` 无法把 schema 继续传给 `codex exec`
- hook 路径和直连 `CodexAnalyzer` 路径的输出约束能力不一致
- 同一个任务在 `codex` 直连和 `hook` 模式下，结构可靠性不同

#### 2.3.2 hook_runner 对输出完全信任

当前 `hook_runner.py` 只是把输出文件原样写回 `stdout`。  
只要输出里出现以下任一情况，就可能导致上层直接失败：

- 前后夹杂解释性文字
- 返回外层包装对象而不是业务载荷本身
- 某些 provider 把真正 JSON 放在 `content` / `result` / `data` 中
- 输出是合法 JSON，但顶层不是当前业务真正想要的那层对象或数组

#### 2.3.3 HookAnalyzer 对 stdout 的要求过于脆弱

`HookAnalyzer._invoke_hook(...)` 当前要求：

- 返回码必须是 `0`
- `stdout` 不能为空
- `stdout.strip()` 必须能直接 `json.loads(...)`

这套约束虽然清晰，但对上游输出格式的微小波动缺少缓冲。

#### 2.3.4 Hook 路径与 CodexAnalyzer 路径能力不对齐

WorkTrace 当前直连 `CodexAnalyzer` 已经具备一定 schema 能力：

- 不同任务有不同的 `output_schema`
- `codex` 直连模式可以把 schema 文件传给 `codex exec`

但 Hook 路径缺少这一层，导致两条分析通道在结构稳定性上不一致。

## 3. 升级目标

本次升级目标如下：

1. Hook 路径具备与 `CodexAnalyzer` 相同级别的 schema 约束能力
2. Hook runner 能对模型输出做一次通用、保守的归一化
3. 不破坏现有 `hook_command` 作为“任意外部命令”的扩展性
4. 不要求大改现有 prompt builder 和 protocol parser
5. 对现有默认 `codex-stdin` 模式改动最小

本次升级不追求：

- 一次性支持所有 provider
- 在 hook runner 内部重做 protocol parser
- 在 hook runner 内部加入复杂业务逻辑
- 把所有错误吞掉并静默降级

## 4. 设计原则

### 4.1 Schema 只约束结构，不替代业务解析

schema 的职责是告诉模型“返回的 JSON 形状应该是什么”。  
schema 不负责验证业务语义是否正确。

例如：

- `candidate_events` 是否存在，可以由 schema 约束
- `candidate_events` 中每个事件的语义是否真的合理，仍由 protocol parser 校验

### 4.2 hook_runner 只做通用输出归一化，不做业务判断

hook runner 只负责：

- 调模型
- 提取 JSON
- 解包常见 envelope
- 输出干净 JSON

hook runner 不负责：

- 判断是否为有效工作事项
- 推断字段默认值
- 修补缺失业务字段
- 代替 protocol parser 做最终业务校验

### 4.3 保持 HookAnalyzer 的严格失败语义

升级后仍建议保留 `HookAnalyzer` 当前“遇到结构问题就失败”的风格。  
稳定性的提升来自更好的 schema 和更稳健的归一化，而不是通过“吞错误继续跑”来假装成功。

### 4.4 尽量不破坏自定义 hook 命令兼容性

`hook_command` 当前是自由字符串。  
如果直接在命令行参数后面追加 `--schema-path` 之类的新参数，会破坏已有自定义 hook 实现。

因此本设计优先使用环境变量在 `HookAnalyzer -> hook runner` 之间透传 schema 信息。

## 5. 目标架构

升级后的默认 Hook 调用链建议如下：

1. `HookAnalyzer` 根据任务构造 prompt
2. `HookAnalyzer` 根据任务选择 `output_schema`
3. `HookAnalyzer` 把 schema 写入临时文件
4. `HookAnalyzer` 通过环境变量把 schema path 传给 hook runner
5. `hook_runner.py` 从 `stdin` 读取 prompt
6. `hook_runner.py` 从环境变量读取 schema path
7. `hook_runner.py` 调 `codex exec --output-schema <schema_path>`
8. `hook_runner.py` 读取模型输出文件
9. `hook_runner.py` 对输出文本执行通用 JSON normalize
10. `hook_runner.py` 把标准化后的 JSON 串写回 `stdout`
11. `HookAnalyzer` 读取 `stdout` 并执行 `json.loads(...)`
12. 对应 protocol parser 做最终业务协议校验

## 6. 改动总览

建议改动 5 个位置：

1. 新增共享 schema 模块
2. 让 `HookAnalyzer` 按任务传 schema
3. 让 `HookAnalyzer` 支持通过环境变量传 schema path
4. 让 `hook_runner.py` 支持 `--output-schema`
5. 新增通用 JSON 提取和 envelope 解包 helper

建议涉及文件：

- 新增 [src/worktrace/analyzers/output_schemas.py](/Users/sunweisheng/Documents/GitHub/WorkTrace/src/worktrace/analyzers/output_schemas.py)
- 修改 [src/worktrace/analyzers/codex.py](/Users/sunweisheng/Documents/GitHub/WorkTrace/src/worktrace/analyzers/codex.py)
- 修改 [src/worktrace/analyzers/hook.py](/Users/sunweisheng/Documents/GitHub/WorkTrace/src/worktrace/analyzers/hook.py)
- 修改 [src/worktrace/hook_runner.py](/Users/sunweisheng/Documents/GitHub/WorkTrace/src/worktrace/hook_runner.py)
- 修改 [src/worktrace/utils/json_io.py](/Users/sunweisheng/Documents/GitHub/WorkTrace/src/worktrace/utils/json_io.py)

## 7. 共享 Output Schema 设计

### 7.1 目标

当前 `CodexAnalyzer` 已经定义了多类输出 schema，但它们位于 `codex.py` 内部。  
为了让 Hook 路径复用同一套协议定义，需要把这些 schema 抽离为共享模块。

### 7.2 建议新文件

建议新增：

`src/worktrace/analyzers/output_schemas.py`

建议把以下函数迁移进去：

- `batch_output_schema()`
- `anchor_batch_output_schema()`
- `merge_output_schema()`
- `bucket_output_schema()`
- `cross_bucket_merge_output_schema()`
- `summary_output_schema()`

### 7.3 迁移后职责

- `CodexAnalyzer` 从共享模块 import schema
- `HookAnalyzer` 从共享模块 import schema
- 未来如果要新增 hook provider，也继续复用同一套 schema

### 7.4 这样做的收益

- 避免 hook 路径和直连路径 schema 漂移
- 避免后续改协议时漏改一边
- 降低 `hook.py` 对 `codex.py` 的不必要耦合

## 8. HookAnalyzer 升级设计

### 8.1 当前问题

`HookAnalyzer` 当前只接收 `prompt: str`，不知道每一轮调用期待什么输出结构。

### 8.2 目标改造

把 `_invoke_hook(...)` 从：

```python
def _invoke_hook(self, prompt: str) -> object:
    ...
```

升级为：

```python
def _invoke_hook(
    self,
    prompt: str,
    *,
    output_schema: dict[str, object] | None = None,
) -> object:
    ...
```

### 8.3 各分析入口传 schema

建议每个分析方法显式传入对应 schema：

- `analyze_batch(...)` -> `batch_output_schema()`
- `analyze_anchor_batch(...)` -> `anchor_batch_output_schema()`
- `merge_day_candidates(...)` -> `merge_output_schema()`
- `bucket_cross_merge_candidates(...)` -> `bucket_output_schema()`
- `decide_cross_bucket_merges(...)` -> `cross_bucket_merge_output_schema()`
- `summarize_for_manager(...)` -> `summary_output_schema()`

这意味着 Hook 路径会和直连 Codex 路径对齐。

### 8.4 通过环境变量传 schema path

为避免破坏已有 `hook_command`，建议不要在命令行尾部追加参数，而是：

1. `HookAnalyzer` 在本地生成 schema 临时文件
2. 调 hook 命令时设置环境变量

建议环境变量名：

`WORKTRACE_HOOK_SCHEMA_PATH`

可选再预留一个：

`WORKTRACE_HOOK_TASK_NAME`

第二个变量不是本次必须，但对未来自定义 hook runner 排障或路由可能有帮助。

### 8.5 _run_command 签名调整

当前 `_run_command(...)` 不支持 `env`。  
建议改为：

```python
def _run_command(
    self,
    args: Sequence[str],
    *,
    cwd: Path | None = None,
    timeout: int | float | None = None,
    input_text: str | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    ...
```

并在 `subprocess.run(...)` 中把 `env=env` 传下去。

### 8.6 环境变量合并规则

建议 `HookAnalyzer` 在调用前：

1. 以 `os.environ.copy()` 为基础
2. 如有 schema 临时文件，则覆写 `WORKTRACE_HOOK_SCHEMA_PATH`
3. 调用结束后删除临时文件

这样不会影响其他环境变量，也不会要求外部 hook 感知 WorkTrace 的内部状态。

## 9. hook_runner 升级设计

### 9.1 当前问题

当前 `hook_runner.py` 只会：

- 从 `stdin` 读 prompt
- 调 `codex exec`
- 读取输出文件
- 把内容原样输出

这导致它没有 schema 透传能力，也没有输出 normalize 能力。

### 9.2 目标行为

升级后 `hook_runner.py` 应做到：

1. 从环境变量读取 schema path
2. 如果 schema path 存在，则把它传给 `codex exec --output-schema`
3. 从输出文件读取结果文本
4. 调用通用 normalize helper
5. 将归一化后的 JSON 串输出到 `stdout`

### 9.3 建议环境变量读取

建议在 runner 内增加：

```python
schema_path = os.environ.get("WORKTRACE_HOOK_SCHEMA_PATH", "").strip()
```

如果为空，则保持当前行为，只是不追加 `--output-schema`。

### 9.4 建议命令构造方式

当前命令：

```python
[
    "codex",
    "exec",
    "--skip-git-repo-check",
    "--ephemeral",
    "--color",
    "never",
    "-s",
    "read-only",
    "-o",
    str(output_path),
    "-",
]
```

建议升级为先构造基础命令，再按条件扩展：

```python
command = [
    "codex",
    "exec",
    "--skip-git-repo-check",
    "--ephemeral",
    "--color",
    "never",
    "-s",
    "read-only",
    "-o",
    str(output_path),
]
if schema_path:
    command.extend(["--output-schema", schema_path])
command.append("-")
```

### 9.5 为什么不在 runner 内做业务 parser

不建议在 `hook_runner.py` 里根据任务类型判断该提取哪个字段，因为：

- runner 不应该依赖业务协议细节
- 不同任务有 object，也有 array
- 业务字段校验已经在 protocol parser 中存在

runner 只应该做“把模型响应标准化成一个干净 JSON 值”。

## 10. 通用 JSON Normalize 设计

### 10.1 目标

把模型产出从“可能带壳、可能混杂文本、可能是内嵌 JSON 字符串”的状态，变成：

- 干净的 JSON object，或
- 干净的 JSON array

供 `HookAnalyzer` 上层直接 `json.loads(...)`。

### 10.2 为什么需要兼容 object 和 array

WorkTrace 的不同任务返回形状不同：

- 批分析、锚点分析、summary 多为 object
- merge 可能直接返回 array

因此不能沿用只支持 object 的工具函数。

### 10.3 建议新增 helper

建议在 [json_io.py](/Users/sunweisheng/Documents/GitHub/WorkTrace/src/worktrace/utils/json_io.py:1) 中新增两个 helper：

```python
def parse_json_value_from_text(text: str) -> object:
    ...

def unwrap_common_json_envelope(value: object) -> object:
    ...
```

### 10.4 parse_json_value_from_text 行为建议

建议按以下顺序处理：

1. `text.strip()` 为空则抛错
2. 直接尝试 `json.loads(stripped)`
3. 如果失败，尝试从文本中截取最外层 JSON object
4. 如果仍失败，尝试从文本中截取最外层 JSON array
5. 截取成功后再次 `json.loads(...)`
6. 解析成功后交给 `unwrap_common_json_envelope(...)`

### 10.5 unwrap_common_json_envelope 行为建议

如果传入值是 dict，则按顺序尝试以下常见包裹字段：

- `structured_output`
- `result`
- `content`
- `message`
- `data`

建议规则：

1. 若当前值本身已经是 array，直接返回
2. 若当前值是 dict，先看它是否已经满足“顶层业务载荷”的直观特征
3. 如果明显是 envelope，再递归尝试上述字段
4. 如果字段值是字符串，且像 JSON，再继续解析
5. 找到第一个可稳定解包的值后返回
6. 若没有可解包字段，则保留原 dict 返回

### 10.6 “顶层业务载荷”识别策略

这里不建议做过强的业务识别。  
建议只做保守判断：

- array 直接认为是最终值
- dict 若包含显著 envelope 字段且其值可继续解析，则优先向内解
- 否则保留当前 dict

原因是：

- runner 不应知道 `candidate_events`、`results`、`merge_decisions` 等全部业务字段
- 过强的解包策略可能把原本正确的业务对象误拆掉

### 10.7 截取 JSON 的策略建议

为避免误截取，建议使用“从外向内找合法 JSON”的保守策略，而不是简单找第一个 `{` 和最后一个 `}`。

建议优先实现两个小函数：

- `try_extract_json_object_fragment(text: str) -> str | None`
- `try_extract_json_array_fragment(text: str) -> str | None`

最低可接受实现：

- 先尝试最简单的首尾截取
- 如失败，再按括号配对方式扫描

如果首版想控制复杂度，也可以先只做：

1. 直接 `json.loads`
2. 失败则尝试“首个 `{` 到最后 `}`”
3. 再失败则尝试“首个 `[` 到最后 `]`”

这已经足以覆盖大部分“前后多一层解释文字”的情况。

### 10.8 Normalize 后输出形式

建议 `hook_runner.py` 对 normalize 后的 Python 值再做一次：

```python
json.dumps(value, ensure_ascii=False)
```

再写回 `stdout`。  
这样可以保证：

- `stdout` 永远是干净 JSON
- 上层 `HookAnalyzer` 继续只需要 `json.loads(...)`

## 11. 与现有 Protocol Parser 的关系

升级后，仍建议保留当前 parser 体系不变。

职责边界如下：

- `output_schema`：约束模型输出大形状
- `hook_runner normalize`：提取并标准化 JSON
- `protocol parser`：校验业务字段、默认值、最终语义结构

三层关系中：

- schema 是第一道护栏
- normalize 是第二道缓冲层
- parser 是第三道业务兜底

这三层不能互相替代。

## 12. 失败语义与错误处理

### 12.1 仍然保留失败

本次升级不建议像 MyAgentWiki `agent_cli_hook.py` 那样在失败时直接返回空数组或空对象。  
WorkTrace 当前是核心分析流水线，静默返回空结果风险较高。

建议保持：

- 子进程返回码非 0 -> 抛 `AnalyzerProtocolError`
- `stdout` 为空 -> 抛 `AnalyzerProtocolError`
- normalize 后仍无法解析 -> 抛 `AnalyzerProtocolError`

### 12.2 建议新增的错误信息

可考虑增加更细的错误描述，例如：

- `Hook analysis returned invalid JSON after normalization.`
- `Hook runner output schema file is missing.`
- `Hook runner returned JSON envelope without usable payload.`

但不建议在第一版过度细分异常类型，优先保持主路径实现简单。

## 13. 向后兼容性

### 13.1 对现有默认配置兼容

当前默认配置：

```python
hook_command = "python3 -m src.worktrace.hook_runner --mode codex-stdin"
```

升级后应继续可用，无需用户修改配置。

### 13.2 对自定义 hook_command 兼容

因为采用环境变量传 schema：

- 不认识 `WORKTRACE_HOOK_SCHEMA_PATH` 的自定义 hook 仍可继续工作
- 只有默认 `hook_runner.py` 会主动消费这个变量

因此这一方案的兼容性明显优于“追加命令行参数”。

### 13.3 对现有 parser 兼容

只要 `hook_runner.py` 最终输出仍是与现有 parser 兼容的 JSON 结构，就不需要同步改 parser。

## 14. 实现顺序建议

建议分 4 步实施。

### 第一步：抽共享 schema

先新增 `output_schemas.py`，并让 `CodexAnalyzer` 改为从共享模块 import。  
这一步不改行为，风险最低。

### 第二步：HookAnalyzer 支持 output_schema

把 `hook.py` 改成按任务传 schema，并通过环境变量把 schema path 传下去。  
此时即便 runner 还没消费 schema，整体也不会破。

### 第三步：hook_runner 支持 schema + normalize

修改 `hook_runner.py`：

- 读取 `WORKTRACE_HOOK_SCHEMA_PATH`
- 透传给 `codex exec`
- 对输出执行 normalize

这是行为变化最大的一步。

### 第四步：补测试并跑基准

补单测、集成测试，并复用现有 benchmark 对比：

- 升级前后的成功率
- 升级前后的耗时
- 出错时的可诊断性

## 15. 测试策略

### 15.1 output schema 共享测试

目标：

- 确认 `CodexAnalyzer` 和 `HookAnalyzer` 取的是同一份 schema 定义

建议测试：

- `batch_output_schema()` 等函数可被两个 analyzer 同时 import
- schema 内容与原协议保持一致

### 15.2 HookAnalyzer 传 schema 测试

目标：

- 确认 `_invoke_hook(..., output_schema=...)` 会生成 schema 文件并透传环境变量

建议测试：

- mock `command_runner`
- 断言 `env["WORKTRACE_HOOK_SCHEMA_PATH"]` 存在
- 断言 schema 文件内容正确

### 15.3 hook_runner 命令构造测试

目标：

- 确认当环境变量存在时，runner 会追加 `--output-schema`

建议测试：

- mock `subprocess.run`
- 断言命令包含 `--output-schema`
- 断言环境变量为空时不追加

### 15.4 JSON normalize 测试

建议至少覆盖以下情况：

1. 纯 object JSON
2. 纯 array JSON
3. 前后有解释文字的 object JSON
4. 前后有解释文字的 array JSON
5. envelope 形如 `{"result": {...}}`
6. envelope 形如 `{"content": "[...]"}`
7. envelope 多层嵌套
8. 非法 JSON，确认抛错

### 15.5 业务回归测试

建议覆盖：

- `analyze_batch`
- `analyze_anchor_batch`
- `merge_day_candidates`
- `summarize_for_manager`

尤其注意 `merge_day_candidates` 返回 array，避免只支持 object 的 helper 误伤。

## 16. 性能影响预估

本次升级会引入以下额外成本：

- 生成 schema 临时文件
- 读取 schema 文件
- 执行一次 normalize

这些开销相对模型调用时长通常很小。  
在真实端到端耗时中，主要成本仍然来自 `codex exec` 本身。

潜在收益是：

- 结构错误率下降
- 因格式波动导致的重试减少
- 端到端成功率更高

因此整体上更可能提升“真实可用性能”，而不是拉低它。

## 17. 风险与注意事项

### 17.1 误解包风险

如果 envelope 解包策略写得太激进，可能把原本正确的业务对象拆错层。  
因此建议：

- 只识别少量常见 envelope 字段
- 优先保守返回原 dict

### 17.2 array 场景被 object-only 逻辑误伤

这是本次升级最需要小心的兼容点之一。  
必须确保 merge 场景能原样保留 array。

### 17.3 schema 和 parser 漂移

如果未来修改业务协议，只改了 parser 没改 schema，或只改了 schema 没改 parser，会带来新的不一致。  
因此共享 schema 模块是本次升级的必要前提。

### 17.4 自定义外部 hook 可能忽略 schema

若用户自定义了自己的 hook 命令且不消费 `WORKTRACE_HOOK_SCHEMA_PATH`，它仍然可以运行，但不会获得 schema 约束收益。  
这属于预期内兼容行为，不是错误。

## 18. 建议的最小可落地版本

如果希望先快速落地，再逐步增强，建议第一版只做以下 4 件事：

1. 抽离共享 `output_schemas.py`
2. `HookAnalyzer` 按任务传 `output_schema`
3. `hook_runner.py` 支持 `WORKTRACE_HOOK_SCHEMA_PATH` 并透传 `--output-schema`
4. `hook_runner.py` 新增保守版 `parse_json_value_from_text(...)`

这一版已经能显著提升 Hook 路径稳定性，而且改动范围可控。

## 19. 后续可扩展方向

本次升级完成后，后续可继续考虑：

- `hook_runner.py` 支持多 provider
- 支持自定义命令模板
- 支持更强的 envelope 识别
- 支持输出 normalize 日志埋点
- 在 preflight 中加入 schema-aware probe

但这些都不应阻塞本次最小升级方案。

## 20. 结论

WorkTrace 当前 Hook 路径的主要短板不是分析能力，而是“缺少 schema 透传”和“缺少稳健输出 normalize”。  
最合适的升级方式不是重写 analyzer，而是补齐以下三层能力：

1. 共享 output schema
2. HookAnalyzer 透传 schema
3. hook_runner 标准化输出

这样可以在不破坏现有架构边界的前提下，把 Hook 通道升级到接近 MyAgentWiki `agent_cli_hook.py` 的稳定性水平，同时保留 WorkTrace 当前严格、清晰的失败语义和业务 parser 体系。
