# merge_inbox

把多人 WorkTrace Markdown 放在日期目录下：

```text
merge_inbox/YYYY/MM/DD/YYYY-MM-DD-姓名.md
```

例如：

```text
merge_inbox/2026/06/29/2026-06-29-张三.md
```

也可以按日期目录下一级子目录分组：

```text
merge_inbox/YYYY/MM/DD/项目A/YYYY-MM-DD-姓名.md
merge_inbox/YYYY/MM/DD/项目B/YYYY-MM-DD-姓名.md
```

如果上游已经先按部门或小组做过一次汇总，也可以直接放入：

```text
merge_inbox/YYYY/MM/DD/YYYY-MM-DD-负责人-merged.md
```

运行：

```bash
python -m src.worktrace.cli merge-collected --date 2026-06-29
```

当前生成文件使用 v3 隐藏来源信息。每条输入事件具备合法的同日会话指纹、
当前人工修订类型或下级人工修订类型中的任一项即可参与合并；人工新增
事件不要求补造聊天证据。v2 文件仍兼容，缺少会话证据的完整事件会按规则
标记为“人工修订，类型无法确认”。有效 v1 事件若三类信息都没有，整次
合并会在模型调用前停止并列出文件，需重新生成。

输出文件为各自合并目录下的 `YYYY-MM-DD-登录人姓名-merged.md`。日期根目录和每个一级子目录会分别合并；旧 `_merged.md` 和当前目录本次输出同名文件会被跳过，其他新版上游 `*-merged.md` 仍可继续参与更高层汇总。

两级人工收集方式：

1. 部门负责人使用自己的飞书身份，收集本部门符合上述来源要求的个人 MD 后运行，得到 `YYYY-MM-DD-部门负责人-merged.md`。
2. 中心负责人收集各部门的 `*-merged.md`；需要其他个人事项参与时，也可以同时放入个人 MD。
3. 中心负责人使用自己的飞书身份再次运行，得到 `YYYY-MM-DD-中心负责人-merged.md`。

个人 MD 和已经包含该人员的部门 MD 同时存在时，两份都会被正常读取；程序不比较来源事件 ID，不拦截，也不提示重复来源，文件组合由负责人人工控制。上游 `*-merged.md` 的负责人会保留到中心结果的“来源负责人”字段。一级子目录是并列汇总范围，不会自动形成部门到中心的先后关系。真实人员日报和 `*-merged.md` 不提交到 Git。

没有飞书 CLI 的环境可显式执行离线汇总，必须提供输出姓名：

```bash
python3 -m src.worktrace.cli merge-collected --date 2026-06-29 --owner-name 部门负责人 --offline
```

离线模式跳过飞书身份查询和自送达，仍会请求配置的模型服务。
默认使用 Codex 加可选 Online 备用；设置 `WORKTRACE_LLM_MODE=online_only`
后只使用 Online，无需安装或配置 Codex。非离线汇总按
`config/self_delivery.json` 决定是否把每个结果文件发送给当前登录用户。
