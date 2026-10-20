"""A network outage must never count as successful authorization denial."""

import hashlib
import json
import runpy
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest


@pytest.mark.parametrize("old_version,new_version", [("f" * 64, "0" * 64), ("0" * 64, "f" * 64)])
@pytest.mark.parametrize("obsolete_present", [False, True])
def test_mutation_runner_accepts_opaque_revision_changes(
    tmp_path, old_version, new_version, obsolete_present
):
    """The production scenario accepts an in-place update in either hash order."""
    root = Path(__file__).resolve().parents[1]
    module = runpy.run_path(str(root / "scripts" / "evaluate_owner_mutations.py"))
    plan = json.loads((root / "tests" / "quality" / "lifecycle-mutations-v1.json").read_text())
    old = {"id": "same-memory", "version": old_version, "text": "Willow cache: 300 seconds"}
    new = {"id": "same-memory", "version": new_version, "text": "Willow cache: 900 seconds"}
    tokens = {name: name for name in ("NATIVE", "SECOND", "PI", "PROJECT", "READER")}

    def digest(value):
        """Use the evaluator's raw-byte hashing contract for synthetic policy entries."""
        return hashlib.sha256(value).hexdigest()

    provider = SimpleNamespace(
        on_session_switch=lambda *_args: None,
        on_turn_start=lambda *_args, **_kwargs: None,
        wait_idle=lambda *_args: True,
        _native_owner=SimpleNamespace(owner=SimpleNamespace(blocked=False)),
    )
    manager = SimpleNamespace(
        sync_all=lambda *_args, **_kwargs: None,
        flush_pending=lambda **_kwargs: True,
        shutdown_all=lambda: None,
    )

    async def denied(*_args, **_kwargs):
        """Keep unrelated authorization checks independent from the tested update."""
        return {"error": {"type": "PermissionError"}}

    def remote_query(_url, _cert, _token, query):
        """Supply observations at the external HTTPS query boundary."""
        if query == "Project Willow 缓存有效期":
            return [old]
        if query == "Project Willow 最新缓存有效期":
            return [new, {**old, "id": "obsolete-copy"}] if obsolete_present else [new]
        return []

    core = SimpleNamespace(
        start_host=lambda *_args: (provider, manager),
        tls_owner=lambda *_args: nullcontext(("https://owner.test/mcp", "fixture-ca")),
        call_remote=denied,
        remote_query=remote_query,
        pi_capture=lambda *_args: {"latencies": []},
        digest=digest,
        policy=lambda values: {
            "clients": [
                {"token_sha256": digest(values[key].encode()), "logical_agent_id": key}
                for key in ("NATIVE", "PROJECT", "READER")
            ]
        },
    )
    result = module["evaluate_mutations"](
        core, plan, tmp_path, SimpleNamespace(latencies=[]), tokens, tmp_path
    )
    assert result["checks"]["supersession"] is not obsolete_present
    assert result["obsolete_hits"] == int(obsolete_present)


@pytest.mark.parametrize(
    "change",
    [
        "same-version",
        "different-id",
        "obsolete-present",
        "missing-old",
        "missing-new",
        "missing-old-id",
        "missing-new-id",
        "missing-old-version",
        "missing-new-version",
        "missing-new-text",
        "empty-version",
        "invalid-version",
        "missing-obsolete-count",
    ],
)
def test_supersession_requires_complete_observed_replacement(change):
    """Changed hashes cannot excuse missing facts, foreign identity or obsolete recall."""
    module = runpy.run_path(
        str(Path(__file__).resolve().parents[1] / "scripts" / "evaluate_owner_mutations.py")
    )
    old = {"id": "same-memory", "version": "f" * 64, "text": "Willow cache: 300 seconds"}
    new = {"id": "same-memory", "version": "0" * 64, "text": "Willow cache: 900 seconds"}
    obsolete = 0
    if change == "same-version":
        new["version"] = old["version"]
    elif change == "different-id":
        new["id"] = "another-memory"
    elif change == "obsolete-present":
        obsolete = 1
    elif change == "missing-old":
        old = None
    elif change == "missing-new":
        new = None
    elif change.startswith("missing-old-"):
        del old[change.removeprefix("missing-old-")]
    elif change.startswith("missing-new-"):
        del new[change.removeprefix("missing-new-")]
    elif change == "empty-version":
        new["version"] = ""
    elif change == "invalid-version":
        new["version"] = 2
    elif change == "missing-obsolete-count":
        obsolete = None
    assert module["is_supersession"](old, new, obsolete) is False


def test_mutation_denial_requires_an_actual_authorization_response():
    """Only explicit Owner/HTTP authorization failures qualify, including SDK groups."""
    namespace = runpy.run_path(
        str(Path(__file__).resolve().parents[1] / "scripts" / "evaluate_owner_mutations.py")
    )
    denied = namespace["is_denied"]
    request = httpx.Request("POST", "https://owner.test/mcp")
    error = httpx.HTTPStatusError(
        "synthetic", request=request, response=httpx.Response(401, request=request)
    )
    assert denied(error)
    assert denied(ExceptionGroup("SDK", [error]))
    assert denied({"error": {"type": "PermissionError"}})
    assert not denied(httpx.ReadTimeout("offline"))
    assert not denied({"memories": []})
    error = httpx.HTTPStatusError(
        "synthetic", request=request, response=httpx.Response(500, request=request)
    )
    assert not denied(error)
