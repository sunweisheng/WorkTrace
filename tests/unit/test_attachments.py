from __future__ import annotations

from src.worktrace.attachments import AttachmentTextSettings, TextAttachmentExtractor
from src.worktrace.config import RuntimeConfig


def test_text_attachment_extractor_uses_configured_extensions_and_limits(tmp_path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "attachment_text.json").write_text(
        """{
  "enabled": true,
  "max_files_per_run": 1,
  "max_file_bytes": 32,
  "allowed_extensions": [".note"]
}
""",
        encoding="utf-8",
    )
    settings = AttachmentTextSettings.load(RuntimeConfig(), cwd=tmp_path)
    extractor = TextAttachmentExtractor(config=RuntimeConfig(), settings=settings)
    supported = tmp_path / "evidence.note"
    supported.write_text("正文结论", encoding="utf-8")
    unsupported = tmp_path / "evidence.md"
    unsupported.write_text("不应读取", encoding="utf-8")

    assert extractor.is_supported("evidence.note")
    assert not extractor.is_supported("evidence.md")
    assert extractor.extract(supported, file_name="evidence.note") == "正文结论"
    assert not extractor.is_supported("evidence.note")
    assert extractor.extract(unsupported, file_name="evidence.md") == ""


def test_text_attachment_extractor_skips_file_above_configured_size(tmp_path) -> None:
    settings = AttachmentTextSettings(
        enabled=True,
        max_files_per_run=1,
        max_file_bytes=3,
        allowed_extensions=(".txt",),
    )
    extractor = TextAttachmentExtractor(config=RuntimeConfig(), settings=settings)
    path = tmp_path / "large.txt"
    path.write_text("超过限制", encoding="utf-8")

    assert extractor.extract(path, file_name="large.txt") == ""
