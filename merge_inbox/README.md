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

运行：

```bash
python -m src.worktrace.cli merge-collected --date 2026-06-29
```

输出文件为各自合并目录下的 `_merged.md`。日期根目录和每个一级子目录会分别合并；真实人员日报和 `_merged.md` 不提交到 Git。
