"""Keep bilingual release guidance tied to the published Runtime installation."""

import re
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
RUNTIME_VERSION = "0.7.2"
RUNTIME_RELEASE_REF = f"v{RUNTIME_VERSION}"
LATEST_PUBLISHED_RUNTIME_REF = "v0.7.2"
# Reviewed commit that the release tag dereferences to. Closeout updates it with
# the tag target, which the release workflow also verifies for the build.
RELEASE_COMMIT = "0ce83104fefbaecabe6b9e4560d3c83ee4e20438"
REPOSITORY_URL = "https://github.com/cnkang/hermes-plugin-qdrant-memory"
READMES = ("README.md", "README.zh-CN.md")
ACTIVE_INSTALL_DOCS = {
    "docs/standalone.md": "mcp",
    "docs/mcp.md": "mcp",
    "docs/local-embedding.md": "mcp,local",
    "docs/shared-memory/README.md": "mcp",
    "docs/shared-memory/README.zh-CN.md": "mcp",
    "docs/shared-memory/remote-owner.md": "remote",
}


@pytest.fixture(params=READMES)
def readme(request):
    """Read each supported translation without importing the runtime or host."""
    return request.param, (ROOT / request.param).read_text(encoding="utf-8")


@pytest.fixture
def release_notice(readme):
    """Join the opening release notice independently of Markdown line wrapping."""
    filename, text = readme
    lines = [line.removeprefix("> ") for line in text.splitlines() if line.startswith("> ")]
    return filename, " ".join(" ".join(lines).split())


def test_release_notice_links_the_exact_published_runtime(release_notice):
    """The announcement must link the released version, not an unversioned project."""
    _, notice = release_notice
    links = re.findall(r"\[PyPI\]\(([^)]+)\)", notice)
    assert links == [f"https://pypi.org/project/hermes-plugin-qdrant-memory/{RUNTIME_VERSION}/"]


def test_active_install_docs_use_the_exact_published_runtime():
    """Keep every install example on the latest version available from PyPI."""
    for name, extras in ACTIVE_INSTALL_DOCS.items():
        text = (ROOT / name).read_text(encoding="utf-8")
        assert f"hermes-plugin-qdrant-memory[{extras}]=={RUNTIME_VERSION}" in text
        assert "hermes-plugin-qdrant-memory[mcp]==0.2.0" not in text


def test_mcp_and_launcher_select_independent_exact_runtime_dependencies(release_notice):
    """Do not promise that publishing Runtime also releases its downstream clients."""
    filename, notice = release_notice
    fragments = {
        "README.md": (
            "generic MCP distribution and managed launcher",
            "their own exact Runtime dependency",
            "released independently",
        ),
        "README.zh-CN.md": (
            "通用 MCP 发行包和托管启动器",
            "各自选择精确的 Runtime 依赖",
            "分别发布",
        ),
    }
    for fragment in fragments[filename]:
        assert fragment in notice


def test_pypi_publication_does_not_upgrade_the_catalog(release_notice):
    """Keep the catalog's reviewed pin and upgrade boundary visible in both languages."""
    filename, notice = release_notice
    fragments = {
        "README.md": ("own reviewed immutable pin", "A PyPI release does not upgrade it"),
        "README.zh-CN.md": ("独立审核的固定版本", "PyPI 发布不会", "自动升级目录"),
    }
    for fragment in fragments[filename]:
        assert fragment in notice


def test_install_guide_defaults_to_catalog_and_pins_verified_runtime():
    """Catalog is the fresh Hermes route; an optional Runtime pin is immutable."""
    guide = (ROOT / "INSTALL.md").read_text(encoding="utf-8")
    assert "hermes plugins install qdrant-memory" in guide
    assert "Catalog installs Runtime 0.6.3" in guide
    assert "b8c1d8babe125470d9278404454728d4426f1dc3" in guide
    assert "latest published Runtime is 0.7.2" in guide
    assert "0ce83104fefbaecabe6b9e4560d3c83ee4e20438" in guide
    assert (
        "hermes plugins install https://github.com/cnkang/hermes-plugin-qdrant-memory --ref"
        in guide
    )
    assert "Do not install a second Qdrant MCP server into the same Hermes profile" in " ".join(
        guide.split()
    )


def test_demo_and_source_pin_use_the_reviewed_release_commit():
    """Keep the demo tag default and the source pin on the reviewed release commit."""
    script = (ROOT / "scripts/demo.sh").read_text(encoding="utf-8")
    match = re.search(r'^RUNTIME_RELEASE_REF="\$\{RUNTIME_RELEASE_REF:-([^}]+)\}"', script, re.M)
    assert match, "scripts/demo.sh must default to a versioned Runtime release tag"
    assert match.group(1) == LATEST_PUBLISHED_RUNTIME_REF
    commit_match = re.search(
        r'^RUNTIME_RELEASE_COMMIT="\$\{RUNTIME_RELEASE_COMMIT:-([0-9a-f]{40})\}"',
        script,
        re.M,
    )
    assert commit_match, "scripts/demo.sh must record the reviewed release commit"
    assert commit_match.group(1) == RELEASE_COMMIT
    assert "runtime_sha_lower" in script.split("install_args=", 1)[0], (
        "the demo must verify the resolved tag against the recorded commit"
    )

    remote_owner = (ROOT / "docs/shared-memory/remote-owner.md").read_text(encoding="utf-8")
    source_match = re.search(r"hermes-plugin-qdrant-memory\.git@([0-9a-f]{40})", remote_owner)
    assert source_match, "remote-owner.md must pin the source install to a full commit"
    assert source_match.group(1) == RELEASE_COMMIT
    normalized = " ".join(remote_owner.split())
    assert f"`{RUNTIME_RELEASE_REF}`" in normalized


def test_pypi_description_installs_the_version_being_built():
    """Immutable package metadata must not recommend the previous PyPI release."""
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    description = (ROOT / project["readme"]).read_text(encoding="utf-8")
    assert f"hermes-plugin-qdrant-memory[mcp]=={project['version']}" in description


def test_published_071_guide_separates_the_unreleased_write_watermark():
    """Do not attribute the main-branch watermark to the 0.7.1 wheel."""
    english = (ROOT / "docs/upgrade-0.7.1.md").read_text(encoding="utf-8")
    chinese = (ROOT / "docs/upgrade-0.7.1.zh-CN.md").read_text(encoding="utf-8")
    assert "published Runtime 0.7.1 wheel" in english
    assert "does **not**\nrecord a local Ledger write watermark" in english
    assert "upgrade plan --replace" in english
    assert "已发布的 Runtime 0.7.1 wheel" in chinese
    assert "**不会**记录本地 Ledger 写入水位" in chinese
    assert "upgrade plan --replace" in chinese

    for name in ("README.md", "README.zh-CN.md"):
        contents = (ROOT / name).read_text(encoding="utf-8")
        assert "docs/upgrade-0.7.1" in contents, name
    for name in ("docs/upgrade.md", "docs/upgrade.zh-CN.md"):
        contents = (ROOT / name).read_text(encoding="utf-8")
        assert "Version boundary" in contents or "版本边界" in contents, name
        assert "upgrade-0.7.1" in contents, name
