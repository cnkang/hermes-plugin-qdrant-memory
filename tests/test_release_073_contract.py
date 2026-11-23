"""Keep the Runtime 0.7.3 release metadata and safety notes aligned."""

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_release_metadata_uses_the_073_notes_and_both_manifests():
    """Keep package, lockfile and native manifest versions synchronized."""
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    locked_packages = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))["package"]
    assert project["version"] == "0.7.3"
    assert project["readme"] == "docs/releases/0.7.3.md"
    assert any(
        package["name"] == "hermes-plugin-qdrant-memory" and package["version"] == "0.7.3"
        for package in locked_packages
    )
    assert "version: 0.7.3" in (ROOT / "plugin.yaml").read_text(encoding="utf-8")
    assert "version: 0.7.3" in (ROOT / "qdrant_memory/plugin.yaml").read_text(encoding="utf-8")


def test_release_notes_cover_upgrade_boundaries_in_both_languages():
    """Keep the important 0.7.3 behavior and limits in both release notes."""
    english = (ROOT / "docs/releases/0.7.3.md").read_text(encoding="utf-8")
    chinese = (ROOT / "docs/releases/0.7.3.zh-CN.md").read_text(encoding="utf-8")

    for text in (english, chinese):
        assert "upgrade plan --replace" in text
        assert "WriterLease" in text
        assert "MCP" in text
        assert "0.7.3" in text

    for phrase in (
        "write watermark",
        "reset epoch",
        "embedding fingerprint",
        "copied SQLite Ledger",
        "out of space",
        "No data migration",
        "re-embedding",
        "Qdrant Server and Cloud",
    ):
        assert phrase.lower() in english.lower()

    for phrase in (
        "写入水位",
        "reset epoch",
        "embedding fingerprint",
        "SQLite Ledger",
        "空间不足",
        "数据迁移",
        "重新嵌入",
        "Qdrant Server 和 Cloud",
    ):
        assert phrase.lower() in chinese.lower()


def test_release_notes_link_exact_public_package_version():
    """Keep installation examples pinned to the immutable 0.7.3 package."""
    for name in ("0.7.3.md", "0.7.3.zh-CN.md"):
        text = (ROOT / "docs/releases" / name).read_text(encoding="utf-8")
        assert "https://pypi.org/project/hermes-plugin-qdrant-memory/0.7.3/" in text
        assert "hermes-plugin-qdrant-memory[mcp]==0.7.3" in text
