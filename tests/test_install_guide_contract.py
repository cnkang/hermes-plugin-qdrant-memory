"""Guard the documented installation paths and their cross-repository pins."""

import re
import shlex
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GUIDE_URL = "https://github.com/cnkang/hermes-plugin-qdrant-memory/blob/main/INSTALL.md"
RUNTIME_SHA = "0ce83104fefbaecabe6b9e4560d3c83ee4e20438"
CATALOG_SHA = "b8c1d8babe125470d9278404454728d4426f1dc3"
MCP_QUICKSTART = "https://github.com/cnkang/qdrant-memory-mcp/blob/main/docs/quickstart.md"


def test_bilingual_readmes_link_the_public_guide_on_the_first_screen():
    """Keep the copyable prompt and guide visible in both README locales."""
    for filename in ("README.md", "README.zh-CN.md"):
        lines = (ROOT / filename).read_text(encoding="utf-8").splitlines()
        first_screen = "\n".join(lines[:30])
        assert "INSTALL.md" in first_screen
        assert "INSTALL.md" in (ROOT / filename).read_text(encoding="utf-8")

    guide = (ROOT / "INSTALL.md").read_text(encoding="utf-8")
    assert GUIDE_URL in guide
    assert "identify my Agent, active profile, and existing memory provider or data" in guide
    assert "Do not migrate or overwrite existing memories" in guide


def test_hermes_route_uses_catalog_by_default_and_a_verified_optional_sha():
    """Keep the default Catalog route and optional immutable source pin exact."""
    guide = (ROOT / "INSTALL.md").read_text(encoding="utf-8")
    english = guide.split("## 中文", 1)[0]
    normalized = " ".join(english.split())
    assert "hermes plugins install qdrant-memory" in guide
    assert "Catalog installs Runtime 0.6.3" in guide
    assert CATALOG_SHA in guide
    assert "latest published Runtime is 0.7.2" in guide
    assert RUNTIME_SHA in guide

    commands = [
        shlex.split(line.strip())
        for line in english.splitlines()
        if line.strip().startswith(
            "hermes plugins install https://github.com/cnkang/hermes-plugin-qdrant-memory --ref"
        )
    ]
    assert commands == [
        [
            "hermes",
            "plugins",
            "install",
            "https://github.com/cnkang/hermes-plugin-qdrant-memory",
            "--ref",
            RUNTIME_SHA,
        ]
    ]
    assert "Do not run `ollama pull` as a default step" in normalized
    assert "Do not install a second Qdrant MCP server into the same Hermes profile" in normalized


def test_existing_memory_section_stays_read_only_and_separates_actions():
    """Prevent the existing-data route from running setup or migration commands."""
    guide = (ROOT / "INSTALL.md").read_text(encoding="utf-8")
    existing = guide.split("## 2. Hermes with existing memory data or provider", 1)[1]
    existing = existing.split("## 3. Codex and other non-Hermes Agents", 1)[0]
    normalized = " ".join(existing.split())
    assert "only read-only checks" in normalized
    assert "Treat installation, provider switching and migration as separate actions" in normalized
    assert "Do not initialize an existing collection" in normalized
    assert "without explicit authorization" in normalized
    assert "hermes qdrant-memory init" not in existing
    code = "\n".join(re.findall(r"```(?:sh|bash)\n(.*?)```", existing, re.S))
    assert "init" not in code
    assert "migrate" not in code


def test_non_hermes_route_points_to_fixed_stdio_mcp_and_states_manual_boundary():
    """Route non-Hermes users to the fixed MCP release and state its limits."""
    guide = (ROOT / "INSTALL.md").read_text(encoding="utf-8")
    assert MCP_QUICKSTART in guide
    assert "qdrant-memory-mcp==0.3.5" in guide
    assert "four `qdrant_memory_*` tools" in guide
    assert "does not automatically\nextract memories" in guide
    assert "does not prove automatic\nextraction or prefetch" in guide


def test_advanced_integrations_are_links_to_their_owning_docs():
    """Keep advanced host links owned by their integration documentation."""
    guide = (ROOT / "INSTALL.md").read_text(encoding="utf-8")
    for target in (
        "docs/dsh-integration.md",
        "integrations/workbuddy/README.md",
        "docs/codex-plugin-verification.md",
        "docs/shared-memory/README.md",
    ):
        assert target in guide
