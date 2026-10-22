"""Cloud model selection is explicit, free-only and independent for each tier."""

import runpy
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def isolated_selection(monkeypatch):
    """Do not inherit an operator's model choices in unit tests."""
    for name in (
        "OPENROUTER_SMOKE_CHAT_MODEL",
        "OPENROUTER_SMOKE_EMBEDDING_MODEL",
        "OPENROUTER_QUALITY_CHAT_MODEL",
        "OPENROUTER_QUALITY_EMBEDDING_MODEL",
        "OPENROUTER_QUALITY_MODEL",
        "OPENROUTER_CHAT_MODEL_INPUT",
        "OPENROUTER_EMBEDDING_MODEL_INPUT",
    ):
        monkeypatch.delenv(name, raising=False)


def support():
    """Load the same configuration support used by the actual runner."""
    return runpy.run_path(str(ROOT / "scripts/quality_openrouter.py"))


@pytest.mark.parametrize(
    "tier,chat,embedding,dimensions",
    [
        ("smoke", "google/gemma-4-31b-it:free", "nvidia/nemotron-3.5-embed-1b:free", 2048),
        ("quality", "google/gemma-4-26b-a4b-it:free", "nvidia/nemotron-3.5-embed-1b:free", 2048),
    ],
)
def test_cloud_defaults_need_only_the_api_key(tier, chat, embedding, dimensions):
    """No optional selection variable is needed for either cloud tier."""
    module = support()
    selected = module["resolve_models"](tier, environ={})
    assert selected == {"chat": chat, "embedding": embedding, "expected_dimensions": dimensions}


@pytest.mark.parametrize("tier", ["smoke", "quality"])
@pytest.mark.parametrize("kind", ["CHAT", "EMBEDDING"])
def test_each_tier_variable_can_override_one_model(tier, kind):
    """Changing one model cannot silently change the other model."""
    module = support()
    original = module["resolve_models"](tier, environ={})
    selected = module["resolve_models"](
        tier, environ={f"OPENROUTER_{tier.upper()}_{kind}_MODEL": "fixture/alternate:free"}
    )
    assert selected[kind.lower()] == "fixture/alternate:free"
    other = "embedding" if kind == "CHAT" else "chat"
    assert selected[other] == original[other]
    if kind == "EMBEDDING":
        assert selected["expected_dimensions"] is None


@pytest.mark.parametrize("tier", ["smoke", "quality"])
def test_empty_variables_use_defaults(tier):
    """Empty GitHub Variables have the same behavior as absent variables."""
    module = support()
    env = {f"OPENROUTER_{tier.upper()}_{kind}_MODEL": "" for kind in ("CHAT", "EMBEDDING")}
    env["OPENROUTER_QUALITY_MODEL"] = ""
    assert module["resolve_models"](tier, environ=env) == module["resolve_models"](tier, environ={})


def test_dispatch_then_tier_then_legacy_then_default():
    """Explicit inputs win and the legacy quality variable stays compatible."""
    resolve = support()["resolve_models"]
    env = {
        "OPENROUTER_QUALITY_CHAT_MODEL": "fixture/tier:free",
        "OPENROUTER_QUALITY_EMBEDDING_MODEL": "fixture/tier-embed:free",
        "OPENROUTER_QUALITY_MODEL": "fixture/legacy:free",
    }
    assert resolve("quality", environ=env)["chat"] == "fixture/tier:free"
    result = resolve(
        "quality",
        environ=env,
        chat="fixture/dispatch:free",
        embedding="fixture/dispatch-embed:free",
    )
    assert result["chat"] == "fixture/dispatch:free"
    assert result["embedding"] == "fixture/dispatch-embed:free"
    del env["OPENROUTER_QUALITY_CHAT_MODEL"]
    assert resolve("quality", environ=env)["chat"] == "fixture/legacy:free"
    assert resolve("smoke", environ=env)["chat"] == "google/gemma-4-31b-it:free"


@pytest.mark.parametrize("kind", ["chat", "embedding"])
@pytest.mark.parametrize("name", ["fixture/paid", "openrouter/free", "fixture/a:free\nsecret"])
def test_paid_dynamic_and_unsafe_names_fail_before_network(kind, name):
    """Fixed quality baselines cannot select paid or random routes."""
    with pytest.raises(ValueError):
        support()["resolve_models"]("quality", environ={}, **{kind: name})


@pytest.mark.parametrize("tier,dimensions", [("smoke", 2048), ("quality", 2048)])
def test_cloud_profiles_use_the_existing_remote_facades(tier, dimensions):
    """Cloud profiles use the existing OpenAI-compatible adapters without Ollama."""
    core = runpy.run_path(str(ROOT / "scripts/evaluate_owner_lifecycle.py"))
    profile = core["model_profile"]("openrouter-cloud-" + tier)
    assert profile["local_models"] == {}
    assert profile["llm"]["base_url"] == support()["BASE_URL"]
    assert profile["embedding"]["base_url"] == support()["BASE_URL"]
    assert profile["embedding"]["provider"] == "openai-compatible"
    assert profile["embedding"]["api_key_env"] == "OPENROUTER_API_KEY"
    assert profile["embedding"]["dimensions"] == dimensions
    assert profile["expected_embedding_dimensions"] == dimensions


def test_hybrid_keeps_local_embeddings_and_accepts_the_new_chat_variable(monkeypatch):
    """The optional hybrid control keeps its original local embedding pipeline."""
    monkeypatch.setenv("OPENROUTER_QUALITY_CHAT_MODEL", "fixture/new:free")
    monkeypatch.setenv("OPENROUTER_QUALITY_MODEL", "fixture/legacy:free")
    core = runpy.run_path(str(ROOT / "scripts/evaluate_owner_lifecycle.py"))
    profile = core["model_profile"]("openrouter-free")
    assert profile["llm"]["model"] == "fixture/new:free"
    assert profile["embedding"]["provider"] == "ollama"
    assert profile["local_models"]


def test_dynamic_router_cannot_be_a_fixed_quality_baseline(monkeypatch):
    """A legacy dynamic choice must not certify a fixed-model quality result."""
    monkeypatch.setenv("OPENROUTER_QUALITY_MODEL", "openrouter/free")
    core = runpy.run_path(str(ROOT / "scripts/evaluate_owner_lifecycle.py"))
    with pytest.raises(ValueError, match="fixed"):
        core["model_profile"]("openrouter-free")
