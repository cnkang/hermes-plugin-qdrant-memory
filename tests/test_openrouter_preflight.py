"""Official metadata and actual probes must agree before any Qdrant state exists."""

import copy
import json
import runpy
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]


def profile(expected=3):
    """Return a synthetic remote profile without any ambient configuration."""
    return {
        "name": "openrouter-cloud-smoke",
        "local_models": {},
        "expected_embedding_dimensions": expected,
        "llm": {
            "mode": "override",
            "provider": "openai-compatible",
            "model": "fixture/chat:free",
            "base_url": "https://openrouter.ai/api/v1",
        },
        "embedding": {
            "provider": "openai-compatible",
            "model": "fixture/embed:free",
            "dimensions": 3,
            "base_url": "https://openrouter.ai/api/v1",
            "timeout_seconds": 30,
            "batch_size": 64,
        },
    }


def metadata(kind):
    """Model identity and zero pricing come from the official catalog shape."""
    return {
        "id": f"fixture/{kind}:free",
        "canonical_slug": f"fixture/{kind}-20261022",
        "pricing": {"prompt": "0", "completion": "0"},
        "architecture": {
            "input_modalities": ["text"],
            "output_modalities": ["text" if kind == "chat" else "embeddings"],
        },
        "supported_parameters": ["response_format", "structured_outputs"] if kind == "chat" else [],
    }


def prepare(*, change=None, width=3, expected=3, ready=True, response_base=False):
    """Serve public metadata and actual model responses through MockTransport."""
    module = runpy.run_path(str(ROOT / "scripts/quality_openrouter.py"))
    calls = []
    rows = {kind: metadata(kind) for kind in ("chat", "embed")}
    if change:
        change(rows)

    def handler(request):
        calls.append((request.method, request.url.path))
        path = request.url.path
        if request.method == "GET":
            if path.endswith("/endpoints"):
                kind = "embed" if "/embed:free/" in path else "chat"
                row = rows[kind]
                return httpx.Response(
                    200,
                    json={
                        "data": {
                            "id": row["id"],
                            "endpoints": [
                                {
                                    "status": 0,
                                    "pricing": row["pricing"],
                                    "supported_parameters": row["supported_parameters"],
                                }
                            ],
                        }
                    },
                )
            kind = "embed" if path.endswith("/embeddings/models") else "chat"
            return httpx.Response(200, json={"data": [rows[kind]]})
        body = json.loads(request.content)
        if path.endswith("/chat/completions"):
            assert body["provider"]["require_parameters"] is True
            assert body["provider"]["max_price"] == {"prompt": 0, "completion": 0}
            if "structured_outputs" in rows["chat"]["supported_parameters"]:
                assert body["response_format"]["json_schema"]["strict"] is True
            else:
                assert body["response_format"] == {"type": "json_object"}
            return httpx.Response(
                200,
                json={
                    "model": "fixture/chat" if response_base else "fixture/chat-20261022",
                    "usage": {"cost": 0},
                    "choices": [{"message": {"content": json.dumps({"ready": ready})}}],
                },
            )
        assert len(body["input"]) == 2
        return httpx.Response(
            200,
            json={
                "model": "fixture/embed" if response_base else "fixture/embed-20261022",
                "usage": {"cost": 0},
                "data": [{"index": i, "embedding": [1.0] * width} for i in range(2)],
            },
        )

    selected = profile(expected)
    session = module["OpenRouterSession"](
        selected,
        key="synthetic-private-key",
        transport=httpx.MockTransport(handler),
        clock=lambda: 0.0,
        sleep=lambda _: None,
    )
    return session, selected, calls


@pytest.mark.parametrize("ready", [True, "wrong-type"])
def test_json_only_metadata_selects_locally_validated_object_mode(ready):
    """A free route can support JSON objects without native JSON Schema decoding."""

    def change(rows):
        rows["chat"]["supported_parameters"] = ["response_format"]

    session, selected, _ = prepare(change=change, ready=ready)
    try:
        if ready is True:
            session.preflight()
            assert session.report()["preflight"]["structured_json_verified"] is True
        else:
            with pytest.raises(RuntimeError) as error:
                session.preflight()
            assert error.value.classification == "capability_incompatible"
        assert selected["llm"]["response_format_mode"] == "json_object"
        assert session.report()["preflight"]["chat"]["schema_enforcement"] == "local"
    finally:
        session.close()


def test_preflight_checks_real_json_and_batched_dimensions():
    """Only actual vector width is used to construct the new isolated pipeline."""
    session, selected, calls = prepare()
    try:
        session.preflight()
        assert selected["embedding"]["dimensions"] == 3
        report = session.report()
        assert report["preflight"]["structured_json_verified"] is True
        assert report["embedding_dimensions"] == 3
        assert report["api_attempts"]["chat"] == 1
        assert report["api_attempts"]["embedding"] == 1
        assert len([c for c in calls if c[0] == "POST"]) == 2
        assert "synthetic-private-key" not in json.dumps(report)
    finally:
        session.close()
    assert session.report()["resources_closed"] is True


@pytest.mark.parametrize("issue", ["paid", "wrong_endpoint", "no_json", "missing"])
def test_metadata_failures_stop_before_inference(issue):
    """A :free suffix alone cannot authorize network model calls."""

    def change(rows):
        if issue == "paid":
            rows["chat"]["pricing"]["prompt"] = "0.001"
        elif issue == "wrong_endpoint":
            rows["chat"]["architecture"]["output_modalities"] = ["embeddings"]
        elif issue == "no_json":
            rows["chat"]["supported_parameters"] = []
        else:
            rows["chat"]["id"] = "fixture/another:free"

    session, _, calls = prepare(change=change)
    try:
        with pytest.raises(RuntimeError):
            session.preflight()
        assert not any(method == "POST" for method, _ in calls)
    finally:
        session.close()


@pytest.mark.parametrize("ready", ["not-a-boolean", 1, 1.0, False])
def test_actual_structured_response_is_required(ready):
    """Metadata cannot substitute for the live schema probe."""
    session, _, _ = prepare(ready=ready)
    try:
        with pytest.raises(RuntimeError) as error:
            session.preflight()
        assert error.value.classification == "capability_incompatible"
    finally:
        session.close()


def test_default_dimension_mismatch_fails_before_collection_creation():
    """A moved default model cannot silently alter the baseline vector schema."""
    session, _, _ = prepare(width=2)
    try:
        with pytest.raises(RuntimeError) as error:
            session.preflight()
        assert error.value.classification == "dimension_mismatch"
    finally:
        session.close()


def test_override_dimension_is_discovered_without_reusing_a_collection():
    """Explicit alternate models get a fresh pipeline with their actual dimensions."""
    session, selected, _ = prepare(width=2, expected=None)
    try:
        session.preflight()
        assert selected["embedding"]["dimensions"] == 2
    finally:
        session.close()


def test_embedding_probe_remains_observable_when_chat_schema_is_incompatible():
    """Independent vector evidence cannot be hidden by the Chat capability gate."""
    session, _, _ = prepare(ready="wrong-type")
    try:
        with pytest.raises(RuntimeError) as error:
            session.preflight()
        assert error.value.classification == "capability_incompatible"
        assert session.report()["embedding_dimensions"] == 3
        assert session.report()["observed_models"]["embedding"] == {"fixture/embed-20261022": 1}
        assert session.report()["preflight"].get("structured_json_verified") is not True
    finally:
        session.close()


def test_missing_key_is_a_configuration_error(monkeypatch):
    """Manual cloud requests cannot produce a passing availability-only result."""
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    module = runpy.run_path(str(ROOT / "scripts/quality_openrouter.py"))
    with pytest.raises(RuntimeError) as error:
        module["OpenRouterSession"](copy.deepcopy(profile()))
    assert error.value.classification == "configuration_error"


def test_free_variant_response_base_identity_requires_catalog_family():
    """The official free variant may report the catalog-bound base model ID."""
    session, _, _ = prepare(response_base=True)
    try:
        session.preflight()
        assert session.report()["observed_models"]["chat"] == {"fixture/chat": 1}
    finally:
        session.close()


def test_unrelated_canonical_cannot_authorize_a_stripped_response_identity():
    """A suffix rule alone must not bypass the selected catalog model family."""

    def change(rows):
        rows["chat"]["canonical_slug"] = "fixture/unrelated-20261022"

    session, _, _ = prepare(change=change, response_base=True)
    try:
        with pytest.raises(RuntimeError) as error:
            session.preflight()
        assert error.value.classification == "model_identity_mismatch"
    finally:
        session.close()
