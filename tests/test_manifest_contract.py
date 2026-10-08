"""Keep directory and packaged Hermes manifest metadata aligned with the project."""

import tomllib
from pathlib import Path

from ruamel.yaml import YAML

ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATHS = (ROOT / "plugin.yaml", ROOT / "qdrant_memory/plugin.yaml")


def test_manifests_declare_v2_metadata_and_match_runtime_dependencies():
    """Verify both supported manifests declare v2 metadata and project dependencies."""
    yaml = YAML(typ="safe")
    manifests = [yaml.load(path.read_text(encoding="utf-8")) for path in MANIFEST_PATHS]
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]

    assert manifests[0] == manifests[1]
    manifest = manifests[0]
    assert manifest["manifest_version"] == 2
    assert manifest["author"] == "Kang Liu"
    assert manifest["license"] == "MIT"
    assert manifest["homepage"] == "https://github.com/cnkang/hermes-plugin-qdrant-memory"
    assert manifest["tags"] == ["memory", "qdrant", "vector-search"]
    assert manifest["python_dependencies"] == project["dependencies"]
    assert manifest["provides_hooks"] == []
    assert "hooks" not in manifest


def test_hermes_manifest_parser_surfaces_v2_metadata():
    """Verify Hermes parses the published v2 metadata and dependencies."""
    from hermes_cli.plugins_manifest import parse_manifest_file

    parsed = parse_manifest_file(MANIFEST_PATHS[0], ROOT, "project", "")

    assert parsed is not None
    assert parsed.manifest_version == 2
    assert parsed.author == "Kang Liu"
    assert parsed.license == "MIT"
    assert parsed.homepage == "https://github.com/cnkang/hermes-plugin-qdrant-memory"
    assert parsed.tags == ["memory", "qdrant", "vector-search"]
    assert parsed.python_dependencies == [
        "qdrant-client>=1.15,<2",
        "httpx>=0.28,<1",
        "portalocker>=3.2,<4",
    ]
    assert parsed.provides_hooks == []
