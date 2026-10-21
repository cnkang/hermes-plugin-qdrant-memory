"""Real-model CI profiles must preserve routing, model identity and failure gates."""

import json
import runpy
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def isolate_model_selection(monkeypatch):
    """Unit contracts never inherit an operator's external model selection."""
    monkeypatch.delenv("OPENROUTER_QUALITY_MODEL", raising=False)


def runner():
    """Load the actual evaluator without an ambient scripts package."""
    return runpy.run_path(str(ROOT / "scripts/evaluate_owner_lifecycle.py"))


def test_smoke_profile_uses_small_fixed_local_models():
    """Smoke does not require external credentials or the quality embedding model."""
    profile = runner()["model_profile"]("smoke-ollama")
    assert profile["llm"]["model"] == "qwen3.5:0.8b"
    assert profile["llm"]["reasoning_effort"] == "none"
    assert profile["embedding"]["model"] == "all-minilm:22m"
    assert profile["embedding"]["dimensions"] == 384
    assert all(len(value) == 64 for value in profile["local_models"].values())


def test_hosted_models_are_separate_from_the_reference_models():
    """The hosted profile has its own fixed models and embedding dimensions."""
    module = runner()
    hosted = module["model_profile"]("hosted-ollama")
    reference = module["model_profile"]("reference")
    assert hosted["embedding"]["model"] != reference["embedding"]["model"]
    assert hosted["embedding"]["dimensions"] == 1024
    assert reference["embedding"]["dimensions"] == 2560
    assert hosted["llm"]["base_url"] == "http://127.0.0.1:19134/v1"
    assert hosted["llm"]["api_key_env"] == "QM_QUALITY_UNUSED_KEY"
    assert hosted["llm"]["reasoning_effort"] == "none"
    assert "reasoning_effort" not in reference["llm"]
    assert len(hosted["local_models"]) == 2
    assert all(len(value) == 64 for value in hosted["local_models"].values())


@pytest.mark.parametrize("name", ["hosted-ollama", "hosted-qwen35", "hosted-qwen35-4b"])
def test_hosted_model_digest_mismatch_is_not_accepted(name):
    """A moved model tag or unexpected cache cannot silently change the test model."""
    module = runner()
    profile = module["model_profile"](name)
    installed = [{"name": name, "digest": value} for name, value in profile["local_models"].items()]
    assert len(module["select_installed_models"](profile, installed)) == 2
    installed[0]["digest"] = "0" * 64
    with pytest.raises(ValueError, match="digest"):
        module["select_installed_models"](profile, installed)


@pytest.mark.parametrize(
    "name,model", [("hosted-qwen35", "qwen3.5:2b"), ("hosted-qwen35-4b", "qwen3.5:4b")]
)
def test_qwen35_comparison_keeps_the_same_embedding_and_generation_budget(name, model):
    """Change only the generation model in the hosted comparison."""
    module = runner()
    baseline = module["model_profile"]("hosted-ollama")
    candidate = module["model_profile"](name)
    assert candidate["embedding"] == baseline["embedding"]
    assert candidate["llm"] == {**baseline["llm"], "model": model}
    assert len(candidate["local_models"]) == 2


def test_free_router_has_no_paid_fallback_and_only_local_embeddings():
    """External generation is explicit; the embedding model stays on the runner."""
    module = runner()
    profile = module["model_profile"]("openrouter-free")
    assert profile["llm"]["model"] == "openrouter/free"
    assert profile["llm"]["base_url"] == "https://openrouter.ai/api/v1"
    assert profile["llm"]["api_key_env"] == "OPENROUTER_API_KEY"
    assert list(profile["local_models"]) == ["qwen3-embedding:0.6b"]
    assert "models" not in profile["llm"]
    assert "reasoning_effort" not in profile["llm"]
    assert profile["llm"]["provider_require_parameters"] is True


@pytest.mark.parametrize("model", ["nvidia/nemotron-3-super-120b-a12b:free", "openrouter/free"])
def test_openrouter_quality_route_can_select_an_explicit_free_model(monkeypatch, model):
    """Model selection is explicit configuration, never a paid fallback."""
    monkeypatch.setenv("OPENROUTER_QUALITY_MODEL", model)
    profile = runner()["model_profile"]("openrouter-free")
    assert profile["llm"]["model"] == model
    assert runner()["model_profile"]("smoke-ollama")["llm"]["model"] == "qwen3.5:0.8b"


@pytest.mark.parametrize("model", ["openai/gpt-4.1", "https://untrusted.invalid", "x:free\nsecret"])
def test_openrouter_quality_rejects_paid_or_invalid_model_configuration(monkeypatch, model):
    """A free-only CI credential cannot silently request a paid or injected route."""
    monkeypatch.setenv("OPENROUTER_QUALITY_MODEL", model)
    with pytest.raises(ValueError, match="free"):
        runner()["model_profile"]("openrouter-free")


@pytest.mark.parametrize(
    "route,observed,expected",
    [
        ("openrouter/free", {"nvidia/example": 1}, False),
        ("nvidia/example:free", {}, False),
        ("nvidia/example:free", {"nvidia/example": 2}, True),
        ("nvidia/example:free", {"nvidia/example:free": 2}, True),
        ("nvidia/example:free", {"nvidia/other": 1}, False),
        ("nvidia/example:free", {"nvidia/example": 1, "nvidia/other": 1}, False),
    ],
)
def test_fixed_quality_model_requires_matching_actual_observations(route, observed, expected):
    """Dynamic routing, absent observations and a changed model cannot certify a fixed model."""
    assert runner()["fixed_generation_observed"](route, observed) is expected


def test_background_failure_observation_keeps_stages_without_private_details():
    """Observe existing background failures without retaining messages or local values."""
    module = runner()
    recorder = module["RecordingLLM"](SimpleNamespace(), [])

    def fail():
        raise ValueError("synthetic-private-data")

    with pytest.raises(ValueError, match="synthetic-private-data"):
        recorder.observe_background_job(fail)()
    assert recorder.background_failures[0]["type"] == "ValueError"
    assert recorder.background_failures[0]["stages"][-1]["function"] == "fail"
    assert "synthetic-private-data" not in json.dumps(recorder.background_failures)


@pytest.mark.parametrize("selected", ["qwen/qwen3:free", "synthetic-secret"])
def test_real_facade_records_selected_models_without_disclosing_credentials(monkeypatch, selected):
    """Observe the actual HTTP response rather than inferring a model from the route name."""
    module = runner()
    profile = module["model_profile"]("openrouter-free")
    monkeypatch.setenv("OPENROUTER_API_KEY", "synthetic-secret")
    llm = module["OwnerLLM"](profile["llm"])
    llm.client.close()
    llm.client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={
                    "model": selected,
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {
                                "content": json.dumps({"memories": []}),
                            },
                        }
                    ],
                },
                request=request,
            )
        )
    )
    recorder = module["RecordingLLM"](llm, [{"id": "one", "user": "synthetic"}], profile)
    try:
        result = recorder.complete_structured(
            purpose="qdrant-memory.extract",
            instructions="Synthetic fixture",
            input=[{"text": json.dumps({"user": "synthetic"})}],
            json_schema={"type": "object"},
        )
        assert result.parsed == {"memories": []}
        assert recorder.http_statuses == {200: 1}
        if selected == "synthetic-secret":
            assert not recorder.provider_models
            assert recorder.recording_errors
        else:
            assert recorder.provider_models == {selected: 1}
        assert "synthetic-secret" not in json.dumps(dict(recorder.provider_models))
    finally:
        llm.close()
