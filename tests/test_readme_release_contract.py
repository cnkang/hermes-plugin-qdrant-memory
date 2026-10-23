"""Keep bilingual release guidance tied to the published Runtime installation."""

import re
import shlex
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
RUNTIME_VERSION = "0.5.0"
RELEASE_COMMIT = "2687a56631d5a7183d3fbe959324e3fc07491139"
REPOSITORY_URL = "https://github.com/cnkang/hermes-plugin-qdrant-memory"
READMES = ("README.md", "README.zh-CN.md")


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


@pytest.fixture
def recommended_installation(readme):
    """Locate the sole recommended installation and its executable shell example."""
    _, text = readme
    sections = re.findall(r"^\*\*([^\n]+?)\*\*\s*\n(.*?)(?=^\*\*|^## |\Z)", text, re.M | re.S)
    recommended = [
        (heading, body)
        for heading, body in sections
        if heading.startswith(("Recommended:", "推荐："))
    ]
    assert len(recommended) == 1, "Each README must have one recommended installation"
    heading, body = recommended[0]
    examples = re.findall(r"^```bash\n(.*?)^```", body, re.M | re.S)
    assert len(examples) == 1, "The recommendation must include one shell example"
    commands = [
        shlex.split(line, comments=True) for line in examples[0].replace("\\\n", " ").splitlines()
    ]
    return heading, [command for command in commands if command]


def test_release_notice_links_the_exact_published_runtime(release_notice):
    """The announcement must link the released version, not an unversioned project."""
    _, notice = release_notice
    links = re.findall(r"\[PyPI\]\(([^)]+)\)", notice)
    assert links == [f"https://pypi.org/project/hermes-plugin-qdrant-memory/{RUNTIME_VERSION}/"]


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


def test_catalog_option_warns_that_its_revision_can_predate_the_release(readme):
    """Catalog installation must explain why the release recommendation is separate."""
    filename, text = readme
    matches = re.findall(r"^\*\*Catalog[^\n]*\*\*\s*\n(.*?)^```bash", text, re.M | re.S)
    assert len(matches) == 1
    guidance = " ".join(matches[0].split())
    fragments = {
        "README.md": ("own fixed revision", f"older than Runtime {RUNTIME_VERSION}"),
        "README.zh-CN.md": ("独立选择固定提交", f"可能早于 Runtime {RUNTIME_VERSION}"),
    }
    for fragment in fragments[filename]:
        assert fragment in guidance


def test_current_runtime_is_the_recommended_installation(recommended_installation):
    """Reject a return to recommending the catalog or a previous Runtime release."""
    heading, _ = recommended_installation
    assert f"Runtime {RUNTIME_VERSION}" in heading
    assert "Hermes CLI" in heading


def test_recommended_install_pins_the_published_commit(recommended_installation):
    """Reject the preview SHA, abbreviated SHAs, mutable refs, and catalog shortcuts."""
    _, commands = recommended_installation
    installs = [command for command in commands if command[:3] == ["hermes", "plugins", "install"]]
    assert installs == [["hermes", "plugins", "install", REPOSITORY_URL, "--ref", RELEASE_COMMIT]]


def test_pinned_install_preserves_the_profile_and_guided_setup(recommended_installation):
    """The release example must select its profile before enabling and initializing."""
    _, commands = recommended_installation
    install = ["hermes", "plugins", "install", REPOSITORY_URL, "--ref", RELEASE_COMMIT]
    install_index = commands.index(install)
    assert ["export", "HERMES_HOME=/absolute/path/to/your/hermes-profile"] in commands[
        :install_index
    ]
    assert commands[install_index + 1 :] == [
        ["hermes", "plugins", "enable", "qdrant-memory"],
        ["hermes", "memory", "setup"],
        ["hermes", "config", "set", "memory.provider", "qdrant-memory"],
        ["hermes", "qdrant-memory", "init"],
    ]


def test_demo_script_pins_the_published_release_commit():
    """The shipped demo script must install the same released commit."""
    script = (ROOT / "scripts/demo.sh").read_text(encoding="utf-8")
    match = re.search(r'^RELEASE_REF="([0-9a-f]{40})"', script, re.M)
    assert match, "scripts/demo.sh must pin a full release commit"
    assert match.group(1) == RELEASE_COMMIT
