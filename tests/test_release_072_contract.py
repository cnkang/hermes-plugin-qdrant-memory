"""Keep the unreleased 0.7.2 patch description within its verified scope."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_072_release_history_and_scope_boundary():
    """Preserve 0.7.2 release history and its local-only watermark limit."""
    english = (ROOT / "docs/releases/0.7.2.md").read_text(encoding="utf-8")
    chinese = (ROOT / "docs/releases/0.7.2.zh-CN.md").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    unreleased = changelog.split("## Unreleased", 1)[1].split("## 0.7.3", 1)[0]
    release = changelog.split("## 0.7.2", 1)[1].split("## 0.7.1", 1)[0]

    assert "another Runtime process" in english
    assert "It does not protect an entire Qdrant Cloud destination" in english
    assert "其他客户端对" in chinese and "它不保护整个 Qdrant Cloud destination" in chinese
    assert "local Ledger write watermark" in release
    assert "watermark" not in unreleased
