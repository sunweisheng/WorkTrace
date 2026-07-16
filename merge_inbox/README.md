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

输入文件必须由当前 v2 流程重新生成，并为每条事件带有同日会话指纹。只要任一文件存在缺少会话证据的事件，整次合并会在模型调用前停止并列出文件，旧 v1 文件不能混用。

输出文件为各自合并目录下的 `YYYY-MM-DD-登录人姓名-merged.md`。日期根目录和每个一级子目录会分别合并；旧 `_merged.md` 和当前目录本次输出同名文件会被跳过，其他新版上游 `*-merged.md` 仍可继续参与更高层汇总。先完成各部门合并、再把当次部门结果放入根目录重新运行，即可进行跨部门汇总。真实人员日报和 `*-merged.md` 不提交到 Git。
