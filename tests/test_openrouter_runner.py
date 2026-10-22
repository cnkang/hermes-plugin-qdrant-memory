"""Cloud runner wiring retains failure evidence, isolation and smoke semantics."""

import json
import runpy
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_quality_failure_causes_keep_the_safe_provider_status(tmp_path, monkeypatch):
    """A guarded API failure retains its status without exposing provider text."""
    module = runpy.run_path(str(ROOT / "scripts/evaluate_owner_lifecycle.py"))
    failure = module["OPENROUTER"].HTTP["OpenRouterFailure"]("capability_incompatible", 404)

    def rejected():
        raise failure

    session = SimpleNamespace(
        preflight=rejected,
        close=lambda: None,
        http=SimpleNamespace(aliases={}),
        report=lambda: {
            "resources_closed": True,
            "final_failure_classification": "capability_incompatible",
        },
    )
    monkeypatch.setattr(module["OPENROUTER"], "OpenRouterSession", lambda *_a, **_kw: session)
    output = tmp_path / "provider-failure.json"
    monkeypatch.setattr(
        "sys.argv",
        [
            "runner",
            "--adapter-root",
            str(tmp_path),
            "--output",
            str(output),
            "--model-profile",
            "openrouter-cloud-quality",
        ],
    )
    assert module["main"]() == 1
    report = json.loads(output.read_text())
    assert report["failure_causes"][0]["http_status"] == 404


def test_smoke_value_error_before_recorder_is_a_configuration_failure(tmp_path, monkeypatch):
    """An inner default category must not hide setup errors from the CLI artifact."""
    module = runpy.run_path(str(ROOT / "scripts/smoke_owner_lifecycle.py"))
    core = module["core"]
    session = SimpleNamespace(
        preflight=lambda: None,
        close=lambda: None,
        report=lambda: {"resources_closed": True, "final_failure_classification": None},
    )
    monkeypatch.setattr(core.OPENROUTER, "OpenRouterSession", lambda *_a, **_kw: session)

    def invalid(_cfg):
        raise ValueError("synthetic-private-value")

    monkeypatch.setattr(core, "OwnerLLM", invalid)
    output = tmp_path / "setup-failure.json"
    monkeypatch.setattr(
        "sys.argv",
        [
            "runner",
            "--adapter-root",
            str(tmp_path),
            "--output",
            str(output),
            "--model-profile",
            "openrouter-cloud-smoke",
        ],
    )
    assert module["main"]() == 1
    report = json.loads(output.read_text())
    assert report["failure_classification"] == "configuration_error"
    assert report["openrouter"]["resources_closed"] is True
    assert "synthetic-private-value" not in output.read_text()


@pytest.mark.parametrize(
    "script,profile,tier",
    [
        ("smoke_owner_lifecycle.py", "openrouter-cloud-smoke", "SMOKE"),
        ("evaluate_owner_lifecycle.py", "openrouter-cloud-quality", "QUALITY"),
    ],
)
def test_paid_model_configuration_writes_a_fail_closed_artifact(
    tmp_path, monkeypatch, script, profile, tier
):
    """Bad configuration is diagnosed before model calls without echoing its value."""
    module = runpy.run_path(str(ROOT / "scripts" / script))
    monkeypatch.setenv(f"OPENROUTER_{tier}_CHAT_MODEL", "synthetic/paid-private-value")
    output = tmp_path / "configuration.json"
    monkeypatch.setattr(
        "sys.argv",
        [
            "runner",
            "--adapter-root",
            str(tmp_path),
            "--output",
            str(output),
            "--model-profile",
            profile,
        ],
    )
    assert module["main"]() == 1
    report = json.loads(output.read_text())
    assert report["status"] == "FAIL"
    assert report["failure_classification"] == "configuration_error"
    assert "paid-private-value" not in output.read_text()


def test_smoke_policy_probes_a_distinct_owner_with_the_same_agent_and_space():
    """The user isolation probe must not rely on a different logical Agent."""
    module = runpy.run_path(str(ROOT / "scripts/smoke_owner_lifecycle.py"))
    tokens = {key: key.lower() for key in ("NATIVE", "SECOND", "PI", "PROJECT", "READER")}
    document = module["smoke_policy"](tokens, "synthetic-foreign-user-token")
    native, foreign = document["clients"][0], document["clients"][-1]
    assert foreign["owner_id"] != native["owner_id"]
    assert foreign["logical_agent_id"] == native["logical_agent_id"]
    assert foreign["space_id"] == native["space_id"]
    assert foreign["permission"] == "read"
    assert "synthetic-foreign-user-token" not in json.dumps(document)
    assert "user_isolation" in module["CHECKS"]


def test_pi_driver_does_not_inherit_openrouter_account_credentials(tmp_path, monkeypatch):
    """Only the Owner process needs the external model account credential."""
    module = runpy.run_path(str(ROOT / "scripts/evaluate_owner_lifecycle.py"))
    monkeypatch.setenv("OPENROUTER_API_KEY", "synthetic-private-key")
    rows = [{"id": "synthetic", "user": "An anonymous project uses Vim."}]

    def driver(_command, **kwargs):
        assert "OPENROUTER_API_KEY" not in kwargs["env"]
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "captures": [{"case_id": "synthetic", "status": "COMMITTED"}],
                    "history_leak": False,
                }
            ),
        )

    monkeypatch.setattr(module["subprocess"], "run", driver)
    module["pi_capture"](tmp_path, tmp_path / "pi", rows, "url", "ca", "capture", "read")


@pytest.mark.parametrize(
    "script,profile",
    [
        ("smoke_owner_lifecycle.py", "openrouter-cloud-smoke"),
        ("evaluate_owner_lifecycle.py", "openrouter-cloud-quality"),
    ],
)
def test_manual_cloud_without_key_writes_redacted_failure(
    tmp_path, monkeypatch, capsys, script, profile
):
    """Missing credentials fail before any model, Ollama or lifecycle request."""
    module = runpy.run_path(str(ROOT / "scripts" / script))
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    output = tmp_path / "failure.json"
    monkeypatch.setattr(
        "sys.argv",
        [
            "runner",
            "--adapter-root",
            str(tmp_path),
            "--output",
            str(output),
            "--model-profile",
            profile,
        ],
    )
    assert module["main"]() == 1
    report = json.loads(output.read_text())
    assert report["status"] == "FAIL"
    assert report["failure_classification"] == "configuration_error"
    assert report["required_secret"] == "OPENROUTER_API_KEY"
    assert report["baseline_executed"] is False
    assert "Traceback" not in capsys.readouterr().out


@pytest.mark.parametrize("problem", [None, "isolation", "exception", "quota"])
def test_cloud_smoke_uses_preflight_and_closes_resources(tmp_path, monkeypatch, problem):
    """Cloud wiring cannot skip a lifecycle gate or promote smoke to quality PASS."""
    module = runpy.run_path(str(ROOT / "scripts/smoke_owner_lifecycle.py"))
    core = module["core"]
    calls, homes = [], []
    extension = tmp_path / "integrations/pi/owner-memory.mjs"
    extension.parent.mkdir(parents=True)
    extension.write_text("// Synthetic adapter identity for runner contracts.\n")
    monkeypatch.setenv("OPENROUTER_API_KEY", "synthetic-private-key")

    class Session:
        """Replace only external API I/O while exercising the actual smoke runner."""

        def __init__(self, profile, **_kwargs):
            """Bind the mutable isolated model profile."""
            self.profile, self.closed = profile, False

        def preflight(self):
            """Simulate a verified response dimension before Owner construction."""
            calls.append("preflight")
            self.profile["embedding"]["dimensions"] = 2

        def attach_llm(self, _llm):
            """The lifecycle uses the guarded LLM client."""
            calls.append("attach")

        def close(self):
            """Record actual runner cleanup."""
            self.closed = True

        def report(self):
            """Return safe transport observations."""
            return {
                "resources_closed": self.closed,
                "embedding_dimensions": 2,
                "final_failure_classification": "quota_exhausted" if problem == "quota" else None,
            }

    class LLM:
        """A unit-only facade; actual lifecycle behavior is tested separately."""

        def __init__(self, _cfg):
            """Do not construct a network client."""

        def close(self):
            """Record release of the owned model facade."""
            calls.append("llm-close")

    def exercise(_adapter, home, recorder, _tokens):
        """Observe input ordering and enforce all unchanged lifecycle checks."""
        calls.append("exercise")
        homes.append(home)
        assert recorder.model_profile["embedding"]["dimensions"] == 2
        if problem in {"exception", "quota"}:
            raise RuntimeError("synthetic-private-key")
        return {
            name: name != "private_isolation" or problem != "isolation" for name in module["CHECKS"]
        }

    monkeypatch.setattr(core.OPENROUTER, "OpenRouterSession", Session)
    monkeypatch.setattr(core, "OwnerLLM", LLM)
    monkeypatch.setitem(module["run_smoke"].__globals__, "exercise", exercise)
    output = tmp_path / "smoke.json"
    monkeypatch.setattr(
        "sys.argv",
        [
            "runner",
            "--adapter-root",
            str(tmp_path),
            "--output",
            str(output),
            "--model-profile",
            "openrouter-cloud-smoke",
        ],
    )
    assert module["main"]() == (0 if problem is None else 1)
    report = json.loads(output.read_text())
    assert report["status"] == ("SMOKE_PASS" if problem is None else "FAIL")
    assert report["quality_evaluated"] is False and report["baseline_executed"] is False
    assert calls[:3] == ["preflight", "attach", "exercise"]
    assert "llm-close" in calls
    assert homes and all(not home.exists() for home in homes)
    observed = report["openrouter"]
    assert observed["resources_closed"] is True
    assert report["temporary_data_cleaned"] is True
    assert "synthetic-private-key" not in output.read_text()
    if problem == "quota":
        assert report["failure_classification"] == "quota_exhausted"


@pytest.mark.parametrize("problem", [None, "no_facts", "mutation"])
def test_cloud_quality_uses_the_frozen_scorer_and_mutation_gate(tmp_path, monkeypatch, problem):
    """HTTP and lifecycle observations cannot replace annotated quality scoring."""
    module = runpy.run_path(str(ROOT / "scripts/evaluate_owner_lifecycle.py"))
    scope = module["main"].__globals__
    monkeypatch.setenv("OPENROUTER_API_KEY", "synthetic-private-key")
    monkeypatch.setenv("OPENROUTER_QUALITY_CHAT_MODEL", "fixture/chat:free")
    monkeypatch.setenv("OPENROUTER_QUALITY_EMBEDDING_MODEL", "fixture/embed:free")
    extension = tmp_path / "integrations/pi/owner-memory.mjs"
    extension.parent.mkdir(parents=True)
    extension.write_text("// Synthetic adapter for runner boundary contracts.\n")
    called = []

    class Session:
        """Replace only network boundaries and retain actual scorer decisions."""

        def __init__(self, profile):
            """Bind the selected fixed response identity."""
            self.closed = False
            self.http = SimpleNamespace(aliases={"chat": {profile["llm"]["model"]}})

        def preflight(self):
            """Record ordering before any disposable Owner is constructed."""
            called.append("preflight")

        def attach_llm(self, _llm):
            """Retain the production adapter injection boundary."""

        def prefetch_queries(self, _cfg, texts):
            """Observe batching of the frozen anonymous inputs."""
            called.append(("prefetch", len(texts)))

        def close(self):
            """Retain closure evidence even when scoring fails."""
            self.closed = True

        def report(self):
            """Return only safe transport state."""
            return {"resources_closed": self.closed, "final_failure_classification": None}

    def evaluate(data, _adapter, _home, recorder, _tokens):
        """Feed annotated observations into the real frozen scorer."""
        called.append("baseline")
        recorder.provider_models["fixture/chat:free"] += 1
        recorder.extractions.update(
            {
                row["id"]: []
                if problem == "no_facts"
                else [" ".join(group[0] for group in fact["terms"]) for fact in row["expected"]]
                for row in data["cases"]
            }
        )
        return {
            "extractions": dict(recorder.extractions),
            "retrievals": {row["id"]: [row["expected"]] for row in data["queries"]},
            "scope_leaks": 0,
            "provider_failures": 0,
            "replay_safe": True,
            "restart_safe": True,
            "restart_raw_equal": True,
            "restart_changed_fields": [],
            "restart_model_calls": 0,
            "post_restart_quality": {"status": "PASS"},
            "context_quality": {"status": "PASS"},
            "retrieval_latency": {},
        }

    def mutations(*_args):
        """A mutation failure independently fails the same final quality gate."""
        return {"status": "FAIL" if problem == "mutation" else "PASS"}

    monkeypatch.setattr(scope["OPENROUTER"], "OpenRouterSession", Session)
    monkeypatch.setitem(scope, "OwnerLLM", lambda _cfg: SimpleNamespace(close=lambda: None))
    monkeypatch.setitem(scope, "evaluate", evaluate)
    monkeypatch.setattr(scope["runpy"], "run_path", lambda _path: {"evaluate_mutations": mutations})
    output = tmp_path / "quality.json"
    monkeypatch.setattr(
        "sys.argv",
        [
            "runner",
            "--adapter-root",
            str(tmp_path),
            "--output",
            str(output),
            "--model-profile",
            "openrouter-cloud-quality",
            "--scenarios",
            str(ROOT / "tests/quality/lifecycle-mutations-v1.json"),
        ],
    )
    assert module["main"]() == (0 if problem is None else 1)
    report = json.loads(output.read_text())
    assert report["status"] == ("PASS" if problem is None else "FAIL")
    assert report["baseline_executed"] is True
    assert called[0] == "preflight" and "baseline" in called
    assert report["openrouter"]["resources_closed"] is True
    assert report["temporary_data_cleaned"] is True
    if problem == "no_facts":
        assert report["positive_case_recall"] == 0
