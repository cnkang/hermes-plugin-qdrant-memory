"""Check the host pin reconciliation script against fixture projects."""

import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_host_pins.py"

HOST = """
[project]
name = "hermes-agent"
version = "0.0.0"
dependencies = ["httpx[socks]==0.28.1"]

[project.optional-dependencies]
mcp = ["mcp==2.0.0"]

[dependency-groups]
dev = ["mcp==2.0.0"]
"""

PLUGIN_OK = """
[project]
name = "hermes-plugin-example"
version = "1.0.0"
dependencies = ["httpx>=0.28,<1"]

[project.optional-dependencies]
mcp = ["mcp>=1.30,<3"]
"""

PLUGIN_CONFLICT = PLUGIN_OK.replace("mcp>=1.30,<3", "mcp>=1.30,<2")


def write(tmp_path: Path, name: str, text: str) -> Path:
    """Create one fixture project directory."""
    directory = tmp_path / name
    directory.mkdir()
    (directory / "pyproject.toml").write_text(text, encoding="utf-8")
    return directory


def run(host: Path, plugin: Path, *args: str) -> subprocess.CompletedProcess:
    """Run the reconciliation script on fixture projects."""
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--host", str(host), "--plugin", str(plugin), *args],
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_compatible_pins_pass(tmp_path):
    """A host pin inside the plugin's range passes."""
    host = write(tmp_path, "host", HOST)
    plugin = write(tmp_path, "plugin", PLUGIN_OK)
    result = run(host, plugin)
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout


def test_conflicting_pin_fails_with_names(tmp_path):
    """A host pin outside the plugin's range fails and names both sides."""
    host = write(tmp_path, "host", HOST)
    plugin = write(tmp_path, "plugin", PLUGIN_CONFLICT)
    result = run(host, plugin)
    assert result.returncode == 1
    assert "mcp==2.0.0" in result.stderr
    assert "<2,>=1.30" in result.stderr
    assert "outside the plugin's range" in result.stderr


def test_marker_filtered_pin_is_skipped(tmp_path):
    """A host pin whose marker excludes this interpreter is not a conflict."""
    filtered = HOST.replace('"mcp==2.0.0"', "\"mcp==2.0.0; python_version < '3.0'\"")
    host = write(tmp_path, "host", filtered)
    plugin = write(tmp_path, "plugin", PLUGIN_CONFLICT)
    result = run(host, plugin)
    assert result.returncode == 0, result.stderr


def test_emit_writes_shared_pins(tmp_path):
    """Emit mode writes the shared host pins."""
    host = write(tmp_path, "host", HOST)
    plugin = write(tmp_path, "plugin", PLUGIN_OK)
    output = tmp_path / "pins.txt"
    result = run(host, plugin, "--emit-requirements", str(output))
    assert result.returncode == 0, result.stderr
    content = output.read_text(encoding="utf-8")
    assert "mcp==2.0.0" in content
    assert "httpx[socks]==0.28.1" in content


def test_plugin_requirement_includes_extras(tmp_path):
    """The plugin requirement line carries the plugin's extras."""
    host = write(tmp_path, "host", HOST)
    plugin = write(tmp_path, "plugin", PLUGIN_OK)
    result = run(host, plugin, "--print-plugin-requirement")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().startswith("hermes-plugin-example[mcp] @ file://")


def test_wildcard_host_pin_passes(tmp_path):
    """A wildcard host pin inside the plugin's range passes."""
    host = write(tmp_path, "host", HOST.replace('"mcp==2.0.0"', '"mcp==2.*"'))
    plugin = write(tmp_path, "plugin", PLUGIN_OK)
    result = run(host, plugin)
    assert result.returncode == 0, result.stderr


def test_wildcard_host_pin_conflict_fails(tmp_path):
    """A wildcard host pin outside the plugin's range fails."""
    host = write(tmp_path, "host", HOST.replace('"mcp==2.0.0"', '"mcp==3.*"'))
    plugin = write(tmp_path, "plugin", PLUGIN_OK)
    result = run(host, plugin)
    assert result.returncode == 1
    assert "mcp" in result.stderr


def test_overlapping_ranges_pass(tmp_path):
    """Ranges that overlap between boundary literals pass."""
    host = write(tmp_path, "host", HOST.replace('"mcp==2.0.0"', '"mcp>1,<2"'))
    plugin = write(tmp_path, "plugin", PLUGIN_OK.replace("mcp>=1.30,<3", "mcp>=1,<=2"))
    result = run(host, plugin)
    assert result.returncode == 0, result.stderr


def test_inconsistent_host_pins_fail(tmp_path):
    """Two different exact host pins for one package fail with a clear message."""
    host = write(tmp_path, "host", HOST.replace('dev = ["mcp==2.0.0"]', 'dev = ["mcp==2.1.0"]'))
    plugin = write(tmp_path, "plugin", PLUGIN_OK)
    result = run(host, plugin)
    assert result.returncode == 1
    assert "inconsistent" in result.stderr
