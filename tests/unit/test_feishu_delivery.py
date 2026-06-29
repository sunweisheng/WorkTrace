from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from src.worktrace.delivery.feishu_cli import FeishuCliSelfDelivery
from src.worktrace.errors import DeliveryError
from src.worktrace.models import SelfIdentity


def test_deliver_to_self_uses_date_and_display_name_for_uploaded_filename(
    tmp_path: Path,
) -> None:
    markdown_path = tmp_path / "data" / "2026" / "06" / "2026-06-29.md"
    markdown_path.parent.mkdir(parents=True)
    markdown_path.write_text("# WorkTrace\n", encoding="utf-8")

    captured: dict[str, Path] = {}

    def fake_runner(args, *, cwd=None):
        assert args[args.index("--as") + 1] == "bot"
        assert args[args.index("--user-id") + 1] == "ou_self"
        file_arg = args[args.index("--file") + 1]
        upload_path = Path(cwd) / file_arg
        captured["upload_path"] = upload_path
        assert upload_path.name == "2026-06-29-孙伟盛.md"
        assert upload_path.read_text(encoding="utf-8") == "# WorkTrace\n"
        return SimpleNamespace(returncode=0, stderr="")

    delivery = FeishuCliSelfDelivery(command_runner=fake_runner, cwd=tmp_path)

    status, target = delivery.deliver_to_self(
        self_identity=SelfIdentity(
            open_id="ou_self",
            display_name="孙伟盛",
            source="lark-cli",
        ),
        markdown_path=markdown_path,
    )

    assert status == "success"
    assert target == "ou_self"
    assert markdown_path.exists()
    assert not captured["upload_path"].exists()


def test_deliver_to_self_does_not_append_display_name_twice(tmp_path: Path) -> None:
    markdown_path = tmp_path / "data" / "2026" / "06" / "2026-06-29-孙伟盛.md"
    markdown_path.parent.mkdir(parents=True)
    markdown_path.write_text("# WorkTrace\n", encoding="utf-8")

    def fake_runner(args, *, cwd=None):
        file_arg = args[args.index("--file") + 1]
        assert Path(file_arg).name == "2026-06-29-孙伟盛.md"
        return SimpleNamespace(returncode=0, stderr="")

    delivery = FeishuCliSelfDelivery(command_runner=fake_runner, cwd=tmp_path)

    status, target = delivery.deliver_to_self(
        self_identity=SelfIdentity(
            open_id="ou_self",
            display_name="孙伟盛",
            source="lark-cli",
        ),
        markdown_path=markdown_path,
    )

    assert status == "success"
    assert target == "ou_self"


def test_deliver_to_self_cleans_up_temp_copy_when_send_fails(tmp_path: Path) -> None:
    markdown_path = tmp_path / "data" / "2026" / "06" / "2026-06-29.md"
    markdown_path.parent.mkdir(parents=True)
    markdown_path.write_text("# WorkTrace\n", encoding="utf-8")

    captured: dict[str, Path] = {}

    def fake_runner(args, *, cwd=None):
        file_arg = args[args.index("--file") + 1]
        captured["upload_path"] = Path(cwd) / file_arg
        return SimpleNamespace(returncode=1, stderr="send failed")

    delivery = FeishuCliSelfDelivery(command_runner=fake_runner, cwd=tmp_path)

    with pytest.raises(DeliveryError, match="send failed"):
        delivery.deliver_to_self(
            self_identity=SelfIdentity(
                open_id="ou_self",
                display_name="孙伟盛",
                source="lark-cli",
            ),
            markdown_path=markdown_path,
        )

    assert markdown_path.exists()
    assert not captured["upload_path"].exists()
