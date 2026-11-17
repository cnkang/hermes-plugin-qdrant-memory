"""Keep the unreleased 0.7.2 patch description within its verified scope."""

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_072_release_metadata_and_scope_boundary():
    """Require consistent metadata and document the local-only watermark limit."""
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert project["version"] == "0.7.2"
    assert project["readme"] == "docs/releases/0.7.2.md"

    english = (ROOT / project["readme"]).read_text(encoding="utf-8")
    chinese = (ROOT / "docs/releases/0.7.2.zh-CN.md").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    unreleased = changelog.split("## Unreleased", 1)[1].split("## 0.7.2", 1)[0]
    release = changelog.split("## 0.7.2", 1)[1].split("## 0.7.1", 1)[0]

    assert "another Runtime process" in english
    assert "It does not protect an entire Qdrant Cloud destination" in english
    assert "其他客户端对" in chinese and "它不保护整个 Qdrant Cloud destination" in chinese
    assert "local Ledger write watermark" in release
    assert "watermark" not in unreleased
