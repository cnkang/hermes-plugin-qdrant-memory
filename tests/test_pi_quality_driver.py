"""Driver attribution uses actual Pi hooks and a deterministic Owner transport."""

import json
import os
import subprocess
from pathlib import Path

import pytest


def test_pi_quality_driver_attributes_retries_to_durable_origin(tmp_path):
    """Duplicate prompts retain their case IDs across delayed and retried delivery."""
    selected = os.environ.get("QDRANT_QUALITY_ADAPTER_ROOT")
    if not selected:
        pytest.skip("Set QDRANT_QUALITY_ADAPTER_ROOT to a locked MCP adapter checkout")
    adapter = Path(selected).resolve()
    sdk = adapter / "integrations" / "agent-hosts"
    extension = adapter / "integrations" / "pi" / "owner-memory.mjs"
    assert (sdk / "node_modules" / "@earendil-works" / "pi-coding-agent").is_dir()
    assert extension.is_file()
    fixture = tmp_path / "adapter"
    (fixture / "integrations" / "pi").mkdir(parents=True)
    (fixture / "integrations" / "agent-hosts").symlink_to(sdk, target_is_directory=True)
    transport = f"export {{ createOwnerMemoryExtension }} from {json.dumps(extension.as_uri())};\n"
    transport += """
let prompts = 0, firstEvent, releaseRetry;
const attempts = new Map();
export async function ownerRequest(mode, value) {
  if (mode === "remote-call") {
    prompts++;
    releaseRetry?.();
    return { memories: [] };
  }
  firstEvent ??= value.event_id;
  const attempt = (attempts.get(value.event_id) ?? 0) + 1;
  attempts.set(value.event_id, attempt);
  if (value.event_id === firstEvent && attempt === 2 && prompts === 1) {
    await new Promise(resolve => { releaseRetry = resolve; });
  }
  return {
    status: value.event_id === firstEvent && prompts < 3 ? "PENDING" : "COMMITTED",
    prompt_number: prompts,
    attempt
  };
}
"""
    (fixture / "integrations" / "pi" / "owner-memory.mjs").write_text(transport)
    home = tmp_path / "home"
    home.mkdir()
    cases = [{"id": case, "user": "The same valid benchmark prompt."} for case in "ABC"]
    result = subprocess.run(
        [
            "node",
            str(Path(__file__).resolve().parents[1] / "scripts" / "pi_lifecycle_quality.mjs"),
            str(fixture),
            str(home),
        ],
        input=json.dumps(cases),
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout.splitlines()[-1])
    events = {}
    for capture in observed["captures"]:
        events.setdefault(capture["event"]["event_id"], []).append(capture)
    assert len(events) == 3
    deliveries = list(events.values())
    for case_id, receipts in zip("ABC", deliveries, strict=True):
        assert {capture["case_id"] for capture in receipts} == {case_id}
        assert receipts[-1]["status"] == "COMMITTED"
    first = deliveries[0]
    assert len(first) >= 3
    assert first[0]["status"] == "PENDING"
    assert first[1]["receipt"]["prompt_number"] == 2
    assert deliveries[1][0]["receipt"]["prompt_number"] == 3
    assert observed["history_leak"] is False
