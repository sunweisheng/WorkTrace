# merge_inbox

把多人 WorkTrace Markdown 放在日期目录下：

```text
merge_inbox/YYYY/MM/DD/YYYY-MM-DD-姓名.md
```

例如：

```text
merge_inbox/2026/06/29/2026-06-29-张三.md
```

运行：

```bash
python -m src.worktrace.cli merge-collected --date 2026-06-29
```

输出文件为同目录 `_merged.md`。真实人员日报和 `_merged.md` 不提交到 Git。
