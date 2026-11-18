"""Policy planning, adoption and resume contracts for the 0.7.0 upgrade path."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from qdrant_client import QdrantClient

from qdrant_memory import strategy, upgrade
from qdrant_memory.config import ledger_namespace
from qdrant_memory.ownership import WriterBusyError
from qdrant_memory.qdrant_store import IDENTITY_ID, QdrantStore

from .helpers import Embedder, config

FINGERPRINT = "test-embedding-space-v1"


def embedded_cfg(home, **overrides):
    """Return an embedded fixture configuration bound to one temporary profile."""
    values = {"qdrant": {"mode": "embedded", "path": str(Path(home) / "qdrant")}}
    values.update(overrides)
    return config(**values)


def legacy_store(home, cfg):
    """Create a 0.6.x-style disk collection whose identity matches the target."""
    store = QdrantStore(QdrantClient(path=cfg["qdrant"]["path"]), cfg, Embedder())
    store.initialize()
    target = strategy.desired_policy(cfg)
    store.client.set_payload(
        store.collection,
        payload={"embedding_fingerprint": target["embedding_fingerprint"]},
        points=[IDENTITY_ID],
        wait=True,
    )
    return store


def seed(store, extra=""):
    """Write two scopes and one near-duplicate through the real store contract."""
    from qdrant_memory.models import Scope, payload, point_id

    for user, agent, text in (
        ("alice", None, "cats preferred"),
        ("alice", "hermes", "cats preferred"),
        ("bob", "hermes", "dogs preferred" + extra),
    ):
        scope = Scope(user, agent)
        value = payload(
            text,
            scope,
            "fixture",
            session_id=user + text,
            category=["preference"],
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-02T00:00:00Z",
        )
        store.upsert([(point_id(scope, "fixture", user + text), value)])


def plan_args(**overrides):
    """Build the operator arguments one upgrade route receives."""
    values = {
        "upgrade_action": "plan",
        "plan": None,
        "confirm_writers_isolated": False,
        "accept_operator_backup": False,
        "policy": None,
        "dry_run": False,
        "collection": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def run_plan(home, cfg, **overrides):
    """Plan a destination and return the CLI-shaped result and saved state."""
    result = upgrade.describe(home, cfg, plan_args(**overrides))
    return result, strategy.read_state(home, ledger_namespace(cfg), upgrade.STATE_SUFFIX)


def test_plan_refuses_uninitialized_without_creating_storage(tmp_path):
    """A plan must never create the local store it is supposed to inspect."""
    cfg = embedded_cfg(tmp_path)
    result, state = run_plan(tmp_path, cfg, dry_run=True)
    assert result["ok"] is False
    assert [item["code"] for item in result["blockers"]] == ["not_initialized"]
    assert state is None
    assert not Path(cfg["qdrant"]["path"]).exists()


def test_plan_marks_legacy_and_persists_only_control_plane(tmp_path):
    """A destination without a policy record is legacy until an apply accepts it."""
    cfg = embedded_cfg(tmp_path)
    store = legacy_store(tmp_path, cfg)
    seed(store)
    store.close()
    before = sorted(path.name for path in tmp_path.iterdir())
    result, state = run_plan(tmp_path, cfg)
    assert result["ok"] is True
    assert result["source_policy"]["status"] == "missing"
    assert result["source_policy"]["effective"]["status"] == "legacy"
    assert result["checkpoint"]["points"] == 3
    assert result["indexes"]["status"] == strategy.NOT_APPLICABLE
    assert state["phase"] == upgrade.PHASE_PLANNED
    assert sorted(path.name for path in tmp_path.iterdir()) == sorted(
        set(before) | {"qdrant-memory"}
    )
    assert not (tmp_path / "qdrant-memory" / "state.db").exists()


def test_apply_adopts_existing_embedded_destination_without_moving_data(tmp_path):
    """Adoption keeps points, payloads, identity and the ledger destination."""
    cfg = embedded_cfg(tmp_path)
    store = legacy_store(tmp_path, cfg)
    seed(store)
    ids = sorted(point.id for point in store.scroll())
    store.close()
    _, state = run_plan(tmp_path, cfg)
    result = upgrade.describe(
        tmp_path, cfg, plan_args(upgrade_action="apply", plan=state["plan_id"])
    )
    assert result["ok"] is True
    assert result["phase"] == upgrade.PHASE_READY
    assert result["steps"]["backup"]["verified"] is True
    assert result["steps"]["backup"]["points"] == 3
    reopened = QdrantClient(path=cfg["qdrant"]["path"])
    try:
        stored = reopened.scroll(cfg["qdrant"]["collection"])[0]
        assert sorted(point.id for point in stored if str(point.id) != IDENTITY_ID) == ids
        identity = reopened.retrieve(cfg["qdrant"]["collection"], ids=[IDENTITY_ID])[0]
        assert (
            identity.payload["embedding_fingerprint"]
            == (strategy.desired_policy(cfg)["embedding_fingerprint"])
        )
        assert identity.payload["_qdrant_memory_schema"] == 1
    finally:
        reopened.close()


def test_apply_commits_one_policy_generation_and_is_idempotent(tmp_path):
    """A repeated apply never creates a second generation or backup."""
    cfg = embedded_cfg(tmp_path)
    store = legacy_store(tmp_path, cfg)
    store.close()
    _, state = run_plan(tmp_path, cfg)
    first = upgrade.describe(
        tmp_path, cfg, plan_args(upgrade_action="apply", plan=state["plan_id"])
    )
    second = upgrade.describe(
        tmp_path, cfg, plan_args(upgrade_action="apply", plan=state["plan_id"])
    )
    assert first["generation"] == 1
    assert second["already_applied"] is True
    record = strategy.read(tmp_path, ledger_namespace(cfg))
    assert record["generation"] == 1
    assert record["policy"] == strategy.desired_policy(cfg)
    backups = list((tmp_path / "qdrant-memory" / "backups").iterdir())
    assert len(backups) == 1


def test_apply_requires_the_saved_plan(tmp_path):
    """An invented or missing plan identifier is refused before any work."""
    cfg = embedded_cfg(tmp_path)
    store = legacy_store(tmp_path, cfg)
    store.close()
    with pytest.raises(upgrade.UpgradeStateError):
        upgrade.describe(tmp_path, cfg, plan_args(upgrade_action="apply", plan="0" * 64))
    _, state = run_plan(tmp_path, cfg)
    with pytest.raises(upgrade.UpgradePlanStaleError):
        upgrade.describe(tmp_path, cfg, plan_args(upgrade_action="apply", plan="1" * 64))
    assert strategy.read(tmp_path, ledger_namespace(cfg)) is None


def test_plan_is_stale_after_the_collection_changes(tmp_path):
    """A destination that moved after the plan must be planned again."""
    cfg = embedded_cfg(tmp_path)
    store = legacy_store(tmp_path, cfg)
    seed(store)
    store.close()
    _, state = run_plan(tmp_path, cfg)
    client = QdrantClient(path=cfg["qdrant"]["path"])
    store = QdrantStore(client, cfg, Embedder())
    seed(store, "-later")
    store.close()
    with pytest.raises(upgrade.UpgradePlanStaleError):
        upgrade.describe(tmp_path, cfg, plan_args(upgrade_action="apply", plan=state["plan_id"]))


def test_plan_refuses_a_different_embedding_pipeline(tmp_path):
    """A fingerprint mismatch blocks in-place adoption instead of re-embedding."""
    cfg = embedded_cfg(tmp_path)
    store = QdrantStore(QdrantClient(path=cfg["qdrant"]["path"]), cfg, Embedder())
    store.initialize()
    store.close()
    result, _ = run_plan(tmp_path, cfg, dry_run=True)
    assert result["ok"] is False
    assert "fingerprint_mismatch" in {item["code"] for item in result["blockers"]}


def test_pending_destructive_intent_blocks_plan(tmp_path):
    """Unresolved delete or reset intent must be recovered before an upgrade."""
    from qdrant_memory.ledger import Ledger
    from qdrant_memory.reset import begin_scope_delete, scope_delete_intent

    cfg = embedded_cfg(tmp_path)
    store = legacy_store(tmp_path, cfg)
    seed(store)
    store.close()
    ledger = Ledger(tmp_path, ledger_namespace(cfg))
    try:
        begin_scope_delete(ledger, scope_delete_intent("alice", None))
    finally:
        ledger.close()
    result, _ = run_plan(tmp_path, cfg, dry_run=True)
    assert "scope_delete_intent" in {item["code"] for item in result["blockers"]}


def test_upgraded_destination_matches_a_fresh_install(tmp_path):
    """The same data reaches the same observable state by install or by upgrade."""
    legacy_home = tmp_path / "legacy"
    fresh_home = tmp_path / "fresh"
    legacy_cfg = embedded_cfg(legacy_home)
    fresh_cfg = embedded_cfg(fresh_home)
    store = legacy_store(legacy_home, legacy_cfg)
    seed(store)
    store.close()
    fresh = QdrantStore(QdrantClient(path=fresh_cfg["qdrant"]["path"]), fresh_cfg, Embedder())
    fresh.initialize()
    target = strategy.desired_policy(fresh_cfg)
    fresh.client.set_payload(
        fresh.collection,
        payload={"embedding_fingerprint": target["embedding_fingerprint"]},
        points=[IDENTITY_ID],
        wait=True,
    )
    seed(fresh)
    fresh.close()
    _, state = run_plan(legacy_home, legacy_cfg)
    upgrade.describe(
        legacy_home, legacy_cfg, plan_args(upgrade_action="apply", plan=state["plan_id"])
    )
    left = snapshot_destination(legacy_cfg)
    right = snapshot_destination(fresh_cfg)
    assert left == right
    assert left["count"] == 3
    assert (
        strategy.effective_summary(legacy_home, ledger_namespace(legacy_cfg), legacy_cfg)["status"]
        == "accepted"
    )


def snapshot_destination(cfg):
    """Return comparable memory state, excluding the reserved identity payload key."""
    client = QdrantClient(path=cfg["qdrant"]["path"])
    try:
        points, _ = client.scroll(
            cfg["qdrant"]["collection"], limit=100, with_payload=True, with_vectors=True
        )
        memories = []
        for point in points:
            if str(point.id) == IDENTITY_ID:
                continue
            payload = dict(point.payload)
            memories.append(
                {
                    "id": str(point.id),
                    "payload": payload,
                    "vector": point.vector["dense"],
                }
            )
        return {"count": len(memories), "memories": sorted(memories, key=lambda x: x["id"])}
    finally:
        client.close()


def test_embedded_backup_copies_the_ledger_and_profile_config(tmp_path):
    """The recovery point carries the Ledger and profile settings, not only Qdrant."""
    from qdrant_memory.ledger import Ledger

    cfg = embedded_cfg(tmp_path)
    (tmp_path / "qdrant-memory.json").write_text(
        json.dumps({"qdrant": {"mode": "embedded", "path": str(tmp_path / "qdrant")}})
    )
    store = legacy_store(tmp_path, cfg)
    seed(store)
    store.close()
    ledger = Ledger(tmp_path, ledger_namespace(cfg))
    ledger.close()
    _, state = run_plan(tmp_path, cfg)
    result = upgrade.describe(
        tmp_path, cfg, plan_args(upgrade_action="apply", plan=state["plan_id"])
    )
    backup = tmp_path / result["steps"]["backup"]["path"]
    assert (backup / "state.db").is_file()
    assert (backup / "qdrant-memory.json").is_file()
    assert (backup / "strategy").is_dir()
    assert (backup / "qdrant" / "meta.json").is_file()


def test_apply_rechecks_the_ledger_under_the_lease(tmp_path):
    """Work that appears after planning blocks the apply instead of being ignored."""
    from qdrant_memory.ledger import Ledger
    from qdrant_memory.reset import begin_scope_delete, scope_delete_intent

    cfg = embedded_cfg(tmp_path)
    store = legacy_store(tmp_path, cfg)
    seed(store)
    store.close()
    _, state = run_plan(tmp_path, cfg)
    ledger = Ledger(tmp_path, ledger_namespace(cfg))
    try:
        begin_scope_delete(ledger, scope_delete_intent("alice", None))
    finally:
        ledger.close()
    with pytest.raises(upgrade.UpgradeBlockedError) as blocked:
        upgrade.describe(tmp_path, cfg, plan_args(upgrade_action="apply", plan=state["plan_id"]))
    assert "scope_delete_intent" in {item["code"] for item in blocked.value.blockers}
    assert strategy.read(tmp_path, ledger_namespace(cfg)) is None


def test_apply_refuses_to_publish_a_corrupt_ledger_backup(tmp_path, monkeypatch):
    """A fresh backup must pass the same Ledger checks as a resumed backup."""
    from qdrant_memory.ledger import Ledger

    cfg = embedded_cfg(tmp_path)
    legacy_store(tmp_path, cfg).close()
    namespace = ledger_namespace(cfg)
    Ledger(tmp_path, namespace).close()
    _, state = run_plan(tmp_path, cfg)
    copy_file = upgrade.shutil.copy2

    def corrupt_ledger_copy(source, destination, *args, **kwargs):
        copied = copy_file(source, destination, *args, **kwargs)
        if Path(source).name == "state.db":
            Path(copied).write_bytes(b"invalid SQLite copy")
        return copied

    with monkeypatch.context() as patch:
        patch.setattr(upgrade.shutil, "copy2", corrupt_ledger_copy)
        with pytest.raises(upgrade.UpgradeRefusedError, match="corrupt local Ledger"):
            upgrade.describe(
                tmp_path, cfg, plan_args(upgrade_action="apply", plan=state["plan_id"])
            )

    assert strategy.read(tmp_path, namespace) is None
    backups = tmp_path / "qdrant-memory" / "backups"
    assert not list(backups.glob(f"*/{upgrade.BACKUP_MARKER}"))
    resumed = upgrade.describe(
        tmp_path, cfg, plan_args(upgrade_action="resume", plan=state["plan_id"])
    )
    assert resumed["phase"] == upgrade.PHASE_READY


def test_backup_verification_uses_the_collection_under_upgrade(tmp_path):
    """A recovery copy is checked against the upgraded collection, not the first one."""
    cfg = embedded_cfg(tmp_path)
    store = legacy_store(tmp_path, cfg)
    seed(store)
    store.close()
    other = embedded_cfg(tmp_path)
    other["qdrant"]["collection"] = "another_collection"
    extra = QdrantStore(QdrantClient(path=cfg["qdrant"]["path"]), other, Embedder())
    extra.initialize()
    extra.close()
    _, state = run_plan(tmp_path, cfg)
    result = upgrade.describe(
        tmp_path, cfg, plan_args(upgrade_action="apply", plan=state["plan_id"])
    )
    assert result["steps"]["backup"]["points"] == 3
    assert result["steps"]["backup"]["verified"] is True


def test_plan_accepts_a_protected_destination_policy(tmp_path, monkeypatch):
    """An enrolled destination can be planned with its protected policy file."""
    cfg = embedded_cfg(tmp_path)
    store = legacy_store(tmp_path, cfg)
    store.close()
    seen = {}

    class Lease:
        """Record the destination policy handed to the writer lease."""

        @classmethod
        def for_config(cls, home, cfg_value, *, destination_policy=None):
            """Capture the binding and return a no-op lease."""
            seen["policy"] = destination_policy
            return cls()

        def close(self):
            """Release nothing."""

    monkeypatch.setattr(upgrade, "WriterLease", Lease)
    monkeypatch.setattr("qdrant_memory.cli._destination_policy", lambda args: {"marker": True})
    args = plan_args(dry_run=True)
    args.policy = Path("policy.json")
    upgrade.describe(tmp_path, cfg, args)
    assert seen["policy"] == {"marker": True}


def test_status_reads_the_saved_plan_without_cloud_credentials(tmp_path):
    """A cloud status call must not need the API key it never uses."""
    cfg = config(
        qdrant={
            "mode": "cloud",
            "url": "https://fixture.cloud.qdrant.io",
            "collection": "fixture_cloud",
        }
    )
    namespace = ledger_namespace(cfg)
    state = {
        "schema": 1,
        "plan_id": "b" * 64,
        "phase": upgrade.PHASE_PLANNED,
        "plan": {
            "namespace": namespace,
            "collection": "fixture_cloud",
            "backend": "cloud",
        },
        "blockers": [],
        "steps": {},
    }
    strategy.write_state(tmp_path, namespace, upgrade.STATE_SUFFIX, state)
    unresolved = dict(cfg)
    unresolved["qdrant"] = dict(cfg["qdrant"], url=None)
    result = upgrade.status(tmp_path, unresolved)
    assert result["phase"] == upgrade.PHASE_PLANNED
    assert result["plan_id"] == "b" * 64


def test_plan_is_stale_when_only_the_embedding_identity_moves(tmp_path):
    """Instruction and normalization changes also invalidate a saved plan."""
    cfg = embedded_cfg(tmp_path)
    store = legacy_store(tmp_path, cfg)
    store.close()
    _, state = run_plan(tmp_path, cfg)
    moved = embedded_cfg(tmp_path, embedding={"send_dimensions": True})
    with pytest.raises(upgrade.UpgradePlanStaleError):
        upgrade.describe(tmp_path, moved, plan_args(upgrade_action="apply", plan=state["plan_id"]))


def test_unreadable_policy_record_is_corrupt_not_missing(tmp_path, monkeypatch):
    """A record that exists but cannot be read must block, not look like a fresh install."""
    cfg = embedded_cfg(tmp_path)
    store = legacy_store(tmp_path, cfg)
    store.close()
    namespace = ledger_namespace(cfg)
    path = strategy.record_path(tmp_path, namespace)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}")
    original = Path.read_text

    def unreadable(self, *args, **kwargs):
        """Simulate a permission failure without relying on POSIX mode bits."""
        if self == path:
            raise PermissionError("denied")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", unreadable)
    assert strategy.read(tmp_path, namespace) == {"corrupt": True}
    result, _ = run_plan(tmp_path, cfg, dry_run=True)
    assert "policy_corrupt" in {item["code"] for item in result["blockers"]}


def test_symlinked_strategy_directory_is_refused(tmp_path, monkeypatch):
    """A substituted policy directory must not receive control-plane records."""
    cfg = embedded_cfg(tmp_path)
    target = tmp_path / "qdrant-memory" / "strategy"
    target.mkdir(parents=True)
    (target / "sentinel").write_text("untouched")
    original = Path.is_symlink

    def substituted(self):
        """Report the managed directory as a substituted path on every platform."""
        return True if self == target else original(self)

    monkeypatch.setattr(Path, "is_symlink", substituted)
    with pytest.raises(ValueError, match="symlink"):
        strategy.write(tmp_path, ledger_namespace(cfg), {"schema": 1})
    assert (target / "sentinel").read_text() == "untouched"


def test_activation_reverifies_before_committing(tmp_path, monkeypatch):
    """Activation re-proves the post-conditions instead of trusting a saved marker.

    The verify step calls the action captured in the step table, so the patched
    module function is reached only by the extra activation check.
    """
    cfg = embedded_cfg(tmp_path)
    store = legacy_store(tmp_path, cfg)
    seed(store)
    store.close()
    _, state = run_plan(tmp_path, cfg)
    calls = []
    original = upgrade._verify

    def counting(ctx):
        """Count verification passes and delegate to the real check."""
        calls.append(1)
        return original(ctx)

    monkeypatch.setattr(upgrade, "_verify", counting)
    upgrade.describe(tmp_path, cfg, plan_args(upgrade_action="apply", plan=state["plan_id"]))
    assert len(calls) == 1


def test_failed_acquire_does_not_rewrite_the_checkpoint(tmp_path, monkeypatch):
    """A second apply that cannot take the lease leaves the owner's state alone."""
    cfg = embedded_cfg(tmp_path)
    store = legacy_store(tmp_path, cfg)
    store.close()
    _, state = run_plan(tmp_path, cfg)
    before = strategy.read_state(tmp_path, ledger_namespace(cfg), upgrade.STATE_SUFFIX)

    def busy(*args, **kwargs):
        """Refuse the lease like a concurrent writer."""
        raise WriterBusyError("busy")

    monkeypatch.setattr(upgrade.WriterLease, "for_config", busy)
    with pytest.raises(WriterBusyError):
        upgrade.describe(tmp_path, cfg, plan_args(upgrade_action="apply", plan=state["plan_id"]))
    after = strategy.read_state(tmp_path, ledger_namespace(cfg), upgrade.STATE_SUFFIX)
    assert after == before


def test_status_is_read_only_and_reports_the_saved_plan(tmp_path):
    """Status never opens the destination or writes a control-plane file."""
    cfg = embedded_cfg(tmp_path)
    store = legacy_store(tmp_path, cfg)
    store.close()
    pending = upgrade.describe(tmp_path, cfg, plan_args(upgrade_action="status"))
    assert pending["phase"] == "unplanned"
    _, state = run_plan(tmp_path, cfg)
    snapshot = {
        path: path.stat().st_mtime_ns
        for path in (tmp_path / "qdrant-memory").rglob("*")
        if path.is_file()
    }
    result = upgrade.describe(tmp_path, cfg, plan_args(upgrade_action="status"))
    assert result["phase"] == upgrade.PHASE_PLANNED
    assert result["plan_id"] == state["plan_id"]
    assert result["effective"] == "missing"
    assert snapshot == {
        path: path.stat().st_mtime_ns
        for path in (tmp_path / "qdrant-memory").rglob("*")
        if path.is_file()
    }


def test_resume_completes_a_step_that_succeeded_before_its_record(tmp_path, monkeypatch):
    """A crash after activation is detected instead of repeating the change."""
    cfg = embedded_cfg(tmp_path)
    store = legacy_store(tmp_path, cfg)
    store.close()
    _, state = run_plan(tmp_path, cfg)
    original = upgrade._Context.mark

    def interrupt(self, step, status, **detail):
        """Record every transition except the final activation marker."""
        if step == "activate" and status == "done":
            raise RuntimeError("simulated crash after activation")
        return original(self, step, status, **detail)

    monkeypatch.setattr(upgrade._Context, "mark", interrupt)
    with pytest.raises(RuntimeError):
        upgrade.describe(tmp_path, cfg, plan_args(upgrade_action="apply", plan=state["plan_id"]))
    assert strategy.read(tmp_path, ledger_namespace(cfg))["generation"] == 1
    failed = upgrade.describe(tmp_path, cfg, plan_args(upgrade_action="status"))
    assert failed["phase"] == upgrade.PHASE_FAILED
    assert failed["failure"]["type"] == "RuntimeError"
    monkeypatch.setattr(upgrade._Context, "mark", original)
    resumed = upgrade.describe(
        tmp_path, cfg, plan_args(upgrade_action="resume", plan=state["plan_id"])
    )
    assert resumed["phase"] == upgrade.PHASE_READY
    assert resumed["steps"]["activate"]["detected"] is True
    assert strategy.read(tmp_path, ledger_namespace(cfg))["generation"] == 1


@pytest.mark.parametrize("backup_state", ["present", "moved", "damaged"])
def test_resume_reuses_the_backup_when_no_writer_touched_the_store(
    tmp_path, monkeypatch, backup_state
):
    """A stopped-writer interruption keeps the original recovery point."""
    cfg = embedded_cfg(tmp_path)
    store = legacy_store(tmp_path, cfg)
    store.close()
    _, state = run_plan(tmp_path, cfg)
    original = upgrade._Context.mark

    def interrupt(self, step, status, **detail):
        """Stop after the backup has been verified and recorded."""
        if step == "verify" and status == "running":
            raise RuntimeError("simulated crash after backup")
        return original(self, step, status, **detail)

    monkeypatch.setattr(upgrade._Context, "mark", interrupt)
    with pytest.raises(RuntimeError):
        upgrade.describe(tmp_path, cfg, plan_args(upgrade_action="apply", plan=state["plan_id"]))
    failed = strategy.read_state(tmp_path, ledger_namespace(cfg), upgrade.STATE_SUFFIX)
    backup = failed["steps"]["backup"]["path"]
    assert (tmp_path / backup).is_dir()
    if backup_state != "present":
        recovered = (tmp_path / backup).with_name(f"recovered-{state['instance_id'][:12]}")
        if backup_state == "damaged":
            import shutil

            shutil.copytree(tmp_path / backup, recovered)
            shutil.rmtree(tmp_path / backup / "qdrant")
        else:
            (tmp_path / backup).rename(recovered)
        backup = str(recovered.relative_to(tmp_path))
    monkeypatch.setattr(upgrade._Context, "mark", original)
    resumed = upgrade.describe(
        tmp_path, cfg, plan_args(upgrade_action="resume", plan=state["plan_id"])
    )
    assert resumed["phase"] == upgrade.PHASE_READY
    assert resumed["steps"]["backup"]["path"] == backup
    if backup_state != "present":
        assert resumed["steps"]["backup"]["recovered"] is True
    assert len(list((tmp_path / "qdrant-memory" / "backups").iterdir())) == (
        2 if backup_state == "damaged" else 1
    )


@pytest.mark.parametrize("action", ["apply", "resume"])
@pytest.mark.parametrize("interrupted_step", ["indexes", "activate"])
@pytest.mark.parametrize(
    "missing",
    [
        "directory",
        "qdrant",
        "metadata",
        "ledger",
        "collection",
        "instance",
        "corrupt_ledger",
        "watermark",
    ],
)
def test_resume_refuses_a_missing_recorded_backup(
    tmp_path, monkeypatch, action, interrupted_step, missing
):
    """A lost recovery point blocks every subsequent target action."""
    import shutil

    cfg = embedded_cfg(tmp_path)
    if missing in ("ledger", "corrupt_ledger", "watermark"):
        from qdrant_memory.ledger import Ledger

        Ledger(tmp_path, ledger_namespace(cfg)).close()
    store = legacy_store(tmp_path, cfg)
    seed(store)
    store.close()
    _, state = run_plan(tmp_path, cfg)
    original = upgrade._Context.mark

    def interrupt(self, step, status, **detail):
        """Stop before the target step while retaining the completed backup record."""
        if step == interrupted_step:
            raise RuntimeError("simulated crash after backup")
        return original(self, step, status, **detail)

    monkeypatch.setattr(upgrade._Context, "mark", interrupt)
    with pytest.raises(RuntimeError, match="simulated crash"):
        upgrade.describe(tmp_path, cfg, plan_args(upgrade_action="apply", plan=state["plan_id"]))
    namespace = ledger_namespace(cfg)
    failed = strategy.read_state(tmp_path, namespace, upgrade.STATE_SUFFIX)
    backup_record = failed["steps"]["backup"]
    assert backup_record["status"] == "done"
    backup = tmp_path / backup_record["path"]
    if missing == "metadata":
        (backup / "qdrant" / "meta.json").unlink()
    elif missing == "ledger":
        (backup / "state.db").unlink()
    elif missing == "corrupt_ledger":
        (backup / "state.db").write_bytes(b"corrupt SQLite backup")
    elif missing == "watermark":
        import sqlite3

        with sqlite3.connect(backup / "state.db") as db:
            db.execute(
                "INSERT OR REPLACE INTO ledger_meta(collection,key,value) VALUES(?,?,?)",
                (namespace, "reset_epoch", "1"),
            )
    elif missing == "instance":
        marker_path = backup / upgrade.BACKUP_MARKER
        marker = json.loads(marker_path.read_text())
        marker["instance_id"] = "another-plan-instance"
        marker_path.write_text(json.dumps(marker))
    elif missing == "collection":
        client = QdrantClient(path=str(backup / "qdrant"))
        try:
            client.delete_collection(cfg["qdrant"]["collection"])
        finally:
            client.close()
    else:
        shutil.rmtree(backup if missing == "directory" else backup / "qdrant")
    backup_contents = {
        path.relative_to(backup): path.read_bytes() for path in backup.rglob("*") if path.is_file()
    }
    monkeypatch.setattr(upgrade._Context, "mark", original)

    def unexpected_action(ctx):
        """Fail if any action runs without the original recovery point."""
        pytest.fail("target action ran after the recorded backup was lost")

    monkeypatch.setattr(upgrade, "ACTIONS", dict.fromkeys(upgrade.STEPS, unexpected_action))
    with pytest.raises(upgrade.UpgradeStateError, match="upgrade plan --replace"):
        upgrade.describe(tmp_path, cfg, plan_args(upgrade_action=action, plan=state["plan_id"]))
    after = strategy.read_state(tmp_path, namespace, upgrade.STATE_SUFFIX)
    assert after["phase"] == upgrade.PHASE_FAILED
    assert after["steps"] == failed["steps"]
    assert backup_contents == {
        path.relative_to(backup): path.read_bytes() for path in backup.rglob("*") if path.is_file()
    }
    assert strategy.read(tmp_path, namespace) is None
    assert list((tmp_path / "qdrant-memory" / "backups").iterdir()) == (
        [] if missing == "directory" else [backup]
    )
    if missing == "qdrant":
        assert not (backup / "qdrant").exists()
    elif missing == "metadata":
        assert not (backup / "qdrant" / "meta.json").exists()


def test_resume_refuses_a_plan_after_a_real_local_write(tmp_path, monkeypatch):
    """An in-place Runtime write crosses the saved plan boundary."""
    from qdrant_memory.ledger import Ledger
    from qdrant_memory.models import Scope, point_id
    from qdrant_memory.runtime import Runtime

    cfg = embedded_cfg(tmp_path)
    store = legacy_store(tmp_path, cfg)
    seed(store)
    before = store.count()
    store.close()
    _, state = run_plan(tmp_path, cfg)
    original = upgrade._Context.mark

    def interrupt(self, step, status, **detail):
        """Stop after the backup so the recovery point exists."""
        if step == "verify" and status == "running":
            raise RuntimeError("simulated crash after backup")
        return original(self, step, status, **detail)

    monkeypatch.setattr(upgrade._Context, "mark", interrupt)
    with pytest.raises(RuntimeError):
        upgrade.describe(tmp_path, cfg, plan_args(upgrade_action="apply", plan=state["plan_id"]))
    failed = strategy.read_state(tmp_path, ledger_namespace(cfg), upgrade.STATE_SUFFIX)
    backup = failed["steps"]["backup"]["path"]
    before = failed["plan"]["checkpoint"]["points"]

    scope = Scope("alice", None)
    identifier = point_id(scope, "fixture", "alicecats preferred")
    store = QdrantStore(QdrantClient(path=cfg["qdrant"]["path"]), cfg, Embedder())
    ledger = Ledger(tmp_path, ledger_namespace(cfg))
    rt = Runtime(cfg, store, ledger)
    try:
        old = store.get(identifier, scope)
        value = dict(old.payload)
        value["importance"] = 0.99
        key = rt.operation(identifier, "UPSERT", value)
        rt.commit([key])
        assert store.count() == before
    finally:
        ledger.close()
        store.close()
    monkeypatch.setattr(upgrade._Context, "mark", original)

    status = upgrade.describe(tmp_path, cfg, plan_args(upgrade_action="status"))
    assert status["plan_stale"] is True
    assert status["plan_stale_reason"] == "write_since_plan"
    assert status["next"] == "qdrant-memory upgrade plan --replace"
    with pytest.raises(upgrade.UpgradePlanStaleError, match="Local writes"):
        upgrade.describe(tmp_path, cfg, plan_args(upgrade_action="resume", plan=state["plan_id"]))
    kept = strategy.read_state(tmp_path, ledger_namespace(cfg), upgrade.STATE_SUFFIX)
    assert kept["plan_id"] == state["plan_id"]
    assert kept["steps"]["backup"]["path"] == backup
    assert (tmp_path / backup).is_dir()
    reused = upgrade.describe(tmp_path, cfg, plan_args(upgrade_action="plan"))
    assert reused["plan_id"] == state["plan_id"]
    assert reused["plan_stale"] is True
    assert "plan_stale" in {item["code"] for item in reused["blockers"]}
    assert (
        strategy.read_state(tmp_path, ledger_namespace(cfg), upgrade.STATE_SUFFIX)["plan_id"]
        == state["plan_id"]
    )

    replanned = upgrade.describe(tmp_path, cfg, plan_args(upgrade_action="plan", replace=True))
    assert replanned["persisted"] is True
    assert replanned["plan_id"] != state["plan_id"]
    with pytest.raises(upgrade.UpgradePlanStaleError, match="does not match the saved plan"):
        upgrade.describe(tmp_path, cfg, plan_args(upgrade_action="resume", plan=state["plan_id"]))
    fresh = strategy.read_state(tmp_path, ledger_namespace(cfg), upgrade.STATE_SUFFIX)
    assert (
        fresh["ledger"]["write_watermark"]["committed"]
        > state["ledger"]["write_watermark"]["committed"]
    )
    archives = list(strategy.directory(tmp_path).glob("*.upgrade-replaced-*.json"))
    assert archives
    archived = json.loads(archives[0].read_text())
    assert archived["plan_id"] == state["plan_id"]
    assert archived["steps"]["backup"]["path"] == backup


def test_failed_operation_is_unresolved_work_not_a_watermark_advance(tmp_path):
    """A failed write attempt does not advance the watermark, but it blocks apply."""
    from qdrant_memory.ledger import Ledger
    from qdrant_memory.models import Scope, point_id
    from qdrant_memory.runtime import Runtime

    cfg = embedded_cfg(tmp_path)
    store = legacy_store(tmp_path, cfg)
    seed(store)
    store.close()
    _, state = run_plan(tmp_path, cfg)
    scope = Scope("alice", None)
    identifier = point_id(scope, "fixture", "alicecats preferred")
    store = QdrantStore(QdrantClient(path=cfg["qdrant"]["path"]), cfg, Embedder())
    ledger = Ledger(tmp_path, ledger_namespace(cfg))
    rt = Runtime(cfg, store, ledger)
    try:
        old = store.get(identifier, scope)
        key = rt.operation(identifier, "UPSERT", dict(old.payload))
        ledger.failure("operations", key, RuntimeError("simulated write failure"), terminal=True)
    finally:
        ledger.close()
        store.close()
    snapshot = upgrade._ledger_snapshot(tmp_path, ledger_namespace(cfg))
    assert snapshot["write_watermark"] == state["ledger"]["write_watermark"]
    with pytest.raises(upgrade.UpgradeBlockedError) as blocked:
        upgrade.describe(tmp_path, cfg, plan_args(upgrade_action="apply", plan=state["plan_id"]))
    assert "failed_operations" in {item["code"] for item in blocked.value.blockers}


def test_reused_plan_merges_current_failed_operations_without_duplicates(tmp_path):
    """A failed write found while reusing a plan replaces the older saved entry."""
    from qdrant_memory.ledger import Ledger
    from qdrant_memory.models import Scope, point_id
    from qdrant_memory.runtime import Runtime

    cfg = embedded_cfg(tmp_path)
    store = legacy_store(tmp_path, cfg)
    seed(store)
    store.close()
    _, state = run_plan(tmp_path, cfg)
    scope = Scope("alice", None)
    identifier = point_id(scope, "fixture", "alicecats preferred")
    store = QdrantStore(QdrantClient(path=cfg["qdrant"]["path"]), cfg, Embedder())
    ledger = Ledger(tmp_path, ledger_namespace(cfg))
    rt = Runtime(cfg, store, ledger)
    try:
        old = store.get(identifier, scope)
        key = rt.operation(identifier, "UPSERT", dict(old.payload))
        ledger.failure("operations", key, RuntimeError("simulated write failure"), terminal=True)
    finally:
        ledger.close()
        store.close()
    state["phase"] = upgrade.PHASE_FAILED
    state["blockers"] = [
        {"code": "failed_operations", "detail": "stale saved detail"},
        {"code": "policy_corrupt", "detail": "keep this saved blocker"},
    ]
    strategy.write_state(tmp_path, ledger_namespace(cfg), upgrade.STATE_SUFFIX, state)
    result = upgrade.describe(tmp_path, cfg, plan_args(upgrade_action="plan"))
    assert result["reused_existing_plan"] is True
    assert result["ok"] is False
    codes = [item["code"] for item in result["blockers"]]
    assert codes.count("failed_operations") == 1
    assert "policy_corrupt" in codes
    detail = next(
        item["detail"] for item in result["blockers"] if item["code"] == "failed_operations"
    )
    assert "may have reached the destination" in detail


def test_completed_destination_reset_invalidates_the_saved_boundary(tmp_path):
    """A cleared Ledger starts a new epoch even before new writes land."""
    from qdrant_memory.ledger import Ledger

    cfg = embedded_cfg(tmp_path)
    store = legacy_store(tmp_path, cfg)
    seed(store)
    store.close()
    _, state = run_plan(tmp_path, cfg)
    ledger = Ledger(tmp_path, ledger_namespace(cfg))
    try:
        ledger.clear_destination()
    finally:
        ledger.close()
    status = upgrade.describe(tmp_path, cfg, plan_args(upgrade_action="status"))
    assert status["plan_stale"] is True
    assert status["plan_stale_reason"] == "write_since_plan"
    assert (
        status["write_watermark"]["reset_epoch"] > state["ledger"]["write_watermark"]["reset_epoch"]
    )


def test_destination_reset_epoch_is_scoped_to_the_namespace(tmp_path):
    """Resetting another destination must not expire this plan."""
    from qdrant_memory.ledger import Ledger

    cfg = embedded_cfg(tmp_path)
    store = legacy_store(tmp_path, cfg)
    seed(store)
    store.close()
    _, state = run_plan(tmp_path, cfg)
    namespace = ledger_namespace(cfg)
    other = Ledger(tmp_path, "another-destination")
    try:
        other.clear_destination()
    finally:
        other.close()
    status = upgrade.describe(tmp_path, cfg, plan_args(upgrade_action="status"))
    assert status["plan_stale"] is False
    assert status["write_watermark"]["reset_epoch"] == 0

    own = Ledger(tmp_path, namespace)
    try:
        own.clear_destination()
    finally:
        own.close()
    status = upgrade.describe(tmp_path, cfg, plan_args(upgrade_action="status"))
    assert status["plan_stale"] is True
    assert status["plan_stale_reason"] == "write_since_plan"
    assert status["write_watermark"]["reset_epoch"] == 1
    assert state["ledger"]["write_watermark"]["reset_epoch"] == 0


def test_old_plan_without_a_watermark_requires_replacement(tmp_path):
    """A plan saved before the watermark cannot prove the stop-write window."""
    cfg = embedded_cfg(tmp_path)
    store = legacy_store(tmp_path, cfg)
    store.close()
    _, state = run_plan(tmp_path, cfg)
    state.pop("ledger", None)
    strategy.write_state(tmp_path, ledger_namespace(cfg), upgrade.STATE_SUFFIX, state)
    status = upgrade.describe(tmp_path, cfg, plan_args(upgrade_action="status"))
    assert status["plan_stale"] is True
    assert status["plan_stale_reason"] == "no_watermark"
    with pytest.raises(upgrade.UpgradePlanStaleError, match="no local write watermark"):
        upgrade.describe(tmp_path, cfg, plan_args(upgrade_action="apply", plan=state["plan_id"]))


def test_remote_apply_requires_isolation_and_a_recovery_point(tmp_path, monkeypatch):
    """A remote destination cannot be adopted without explicit operator evidence."""
    cfg = config(
        qdrant={
            "mode": "server",
            "url": "http://127.0.0.1:6333",
            "collection": "fixture_upgrade",
        }
    )
    state = {
        "schema": 1,
        "plan_id": "a" * 64,
        "phase": upgrade.PHASE_PLANNED,
        "plan": {
            "namespace": ledger_namespace(cfg),
            "collection": "fixture_upgrade",
            "backend": "server",
            "config_digest": strategy.config_digest(cfg),
        },
        "blockers": [],
        "steps": {},
    }
    strategy.write_state(tmp_path, ledger_namespace(cfg), upgrade.STATE_SUFFIX, state)
    saved = strategy.read_state(tmp_path, ledger_namespace(cfg), upgrade.STATE_SUFFIX)
    with pytest.raises(upgrade.UpgradeRefusedError) as denied:
        upgrade._require_authorization(saved, cfg, plan_args())
    assert "confirm-writers-isolated" in str(denied.value)
    with pytest.raises(upgrade.UpgradeRefusedError) as no_backup:
        upgrade._require_authorization(saved, cfg, plan_args(confirm_writers_isolated=True))
    assert "accept-operator-backup" in str(no_backup.value)


def test_index_policy_and_managed_types_match_the_store():
    """One policy definition drives init, doctor, plan and apply."""
    policy = strategy.index_policy()
    from qdrant_memory.qdrant_store import INDEXES

    assert policy["version"] == strategy.INDEX_POLICY_VERSION
    assert set(policy["indexes"]) == set(INDEXES)
    assert {key: value["type"] for key, value in policy["indexes"].items()} == INDEXES


def test_frozen_defaults_preserve_the_dense_policy():
    """A future default change must declare its own legacy interpretation."""
    assert strategy.frozen_defaults_match() is True
    assert strategy.LEGACY_DEFAULTS["search"]["mode"] == "dense"
    assert strategy.LEGACY_DEFAULTS["search"]["rerank"] is False
    assert strategy.LEGACY_DEFAULTS["embedding"]["distance"] == "Cosine"


def test_explain_config_separates_profile_values_from_defaults(tmp_path):
    """A value equal to the old default is not treated as an explicit choice."""
    (tmp_path / "qdrant-memory.json").write_text(json.dumps({"search": {"top_k": 8}}))
    cfg = config(qdrant={"mode": "embedded", "path": str(tmp_path / "qdrant")})
    explained = strategy.explain_config(tmp_path, cfg)
    assert explained["sources"]["search.top_k"]["source"] == "profile"
    assert explained["sources"]["search.candidate_k"]["source"] == "default"
    assert explained["sources"]["search.top_k"]["legacy_default"] == 8


def test_effective_summary_reports_pending_until_target_matches(tmp_path):
    """A saved record that differs from the configured target stays pending."""
    (tmp_path / "qdrant-memory.json").write_text(
        json.dumps({"qdrant": {"mode": "embedded", "path": str(tmp_path / "qdrant")}})
    )
    cfg = config(qdrant={"mode": "embedded", "path": str(tmp_path / "qdrant")})
    namespace = ledger_namespace(cfg)
    assert strategy.effective_summary(tmp_path, namespace, cfg)["status"] == "missing"
    strategy.write(
        tmp_path,
        namespace,
        {"schema": 1, "generation": 1, "policy": {"index_policy_version": 99}},
    )
    assert strategy.effective_summary(tmp_path, namespace, cfg)["status"] == "pending"
    strategy.write(
        tmp_path,
        namespace,
        {"schema": 1, "generation": 2, "policy": strategy.desired_policy(cfg)},
    )
    assert strategy.effective_summary(tmp_path, namespace, cfg)["status"] == "accepted"


def test_corrupt_policy_record_blocks_adoption(tmp_path):
    """An unreadable record is a blocker, never a fresh install."""
    cfg = embedded_cfg(tmp_path)
    store = legacy_store(tmp_path, cfg)
    store.close()
    namespace = ledger_namespace(cfg)
    path = strategy.record_path(tmp_path, namespace)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json")
    result, _ = run_plan(tmp_path, cfg, dry_run=True)
    assert "policy_corrupt" in {item["code"] for item in result["blockers"]}


def test_plan_output_carries_no_endpoint_or_secret(tmp_path):
    """Plans are safe to archive: no credentials, endpoint or memory text."""
    cfg = config(
        qdrant={
            "mode": "server",
            "url": "https://example.invalid:6333",
            "api_key": "super-secret-key",
            "collection": "fixture_unreachable",
        }
    )
    result, _ = run_plan(tmp_path, cfg, dry_run=True)
    text = json.dumps(result)
    assert "super-secret-key" not in text
    assert "example.invalid" not in text


def _write_owner_receipt(home, namespace, *, event_status=None, event_json='{"user":"SECRET"}'):
    """Persist one real admitted receipt on disk, with an optional terminal event."""
    from qdrant_memory.ledger import Ledger

    ledger = Ledger(home, namespace)
    try:
        with ledger.lock, ledger.db:
            ledger.db.execute(
                """CREATE TABLE IF NOT EXISTS owner_events_v1 (
                    collection TEXT NOT NULL, request_key TEXT NOT NULL,
                    input_hash TEXT NOT NULL, event_key TEXT NOT NULL, event_json TEXT NOT NULL,
                    PRIMARY KEY(collection, request_key))"""
            )
            ledger.db.execute(
                "INSERT OR REPLACE INTO owner_events_v1 VALUES(?,?,?,?,?)",
                (namespace, "receipt-1", "input", "event-1", event_json),
            )
            if event_status:
                ledger.db.execute(
                    "INSERT OR REPLACE INTO events(event_id,payload_json,status,collection)"
                    " VALUES(?,?,?,?)",
                    ("event-1", "{}", event_status, namespace),
                )
    finally:
        ledger.close()


def _scrub_owner_receipt(home, namespace):
    """Represent the Owner recovery path clearing its admitted receipt."""
    from qdrant_memory.ledger import Ledger

    ledger = Ledger(home, namespace)
    try:
        with ledger.lock, ledger.db:
            ledger.db.execute(
                "UPDATE owner_events_v1 SET event_json='{}' WHERE collection=?",
                (namespace,),
            )
    finally:
        ledger.close()


def _write_shared_request(home, namespace, *, done):
    """Persist one incomplete shared request on disk."""
    from qdrant_memory.ledger import Ledger

    ledger = Ledger(home, namespace)
    try:
        with ledger.lock, ledger.db:
            ledger.db.execute(
                """CREATE TABLE IF NOT EXISTS shared_requests_v1 (
                    collection TEXT, request_key TEXT, input_hash TEXT, plan TEXT,
                    response TEXT, done INTEGER, principal TEXT,
                    PRIMARY KEY(collection, request_key))"""
            )
            ledger.db.execute(
                "INSERT OR REPLACE INTO shared_requests_v1 VALUES(?,?,?,?,?,?,?)",
                (namespace, "request-1", "hash", "null", "{}", done, "principal"),
            )
    finally:
        ledger.close()


def _interrupt_after_backup(monkeypatch):
    """Fail before the backup completion record is saved, after the copy exists."""
    original = upgrade._Context.mark

    def interrupt(self, step, status, **detail):
        """Raise at the exact crash window this regression covers."""
        if step == "backup" and status == "done":
            raise RuntimeError("simulated crash before backup record")
        return original(self, step, status, **detail)

    monkeypatch.setattr(upgrade._Context, "mark", interrupt)
    return original


def test_plan_preserves_an_interrupted_backup_and_resume_reuses_it(tmp_path, monkeypatch):
    """A replan keeps the phase, step record and recovery point of an interrupted pass."""
    cfg = embedded_cfg(tmp_path)
    store = legacy_store(tmp_path, cfg)
    seed(store)
    store.close()
    _, state = run_plan(tmp_path, cfg)
    original_mark = _interrupt_after_backup(monkeypatch)
    with pytest.raises(RuntimeError):
        upgrade.describe(tmp_path, cfg, plan_args(upgrade_action="apply", plan=state["plan_id"]))
    failed = strategy.read_state(tmp_path, ledger_namespace(cfg), upgrade.STATE_SUFFIX)
    assert failed["phase"] == upgrade.PHASE_FAILED
    assert failed["steps"]["backup"]["status"] == "running"
    assert "path" not in failed["steps"]["backup"]
    backups = list((tmp_path / "qdrant-memory" / "backups").iterdir())
    assert len(backups) == 1

    replanned = upgrade.describe(tmp_path, cfg, plan_args(upgrade_action="plan"))
    assert replanned["ok"] is True
    assert replanned["reused_existing_plan"] is True
    assert replanned["persisted"] is False
    assert replanned["plan_id"] == state["plan_id"]
    assert replanned["phase"] == upgrade.PHASE_FAILED
    assert strategy.read_state(tmp_path, ledger_namespace(cfg), upgrade.STATE_SUFFIX) == failed

    monkeypatch.setattr(upgrade._Context, "mark", original_mark)
    resumed = upgrade.describe(
        tmp_path, cfg, plan_args(upgrade_action="resume", plan=state["plan_id"])
    )
    assert resumed["phase"] == upgrade.PHASE_READY
    assert resumed["steps"]["backup"]["detected"] is True
    assert resumed["steps"]["backup"]["recovered"] is True
    assert (tmp_path / resumed["steps"]["backup"]["path"]).is_dir()
    assert strategy.read(tmp_path, ledger_namespace(cfg))["generation"] == 1
    again = upgrade.describe(
        tmp_path, cfg, plan_args(upgrade_action="resume", plan=state["plan_id"])
    )
    assert again["already_applied"] is True
    assert strategy.read(tmp_path, ledger_namespace(cfg))["generation"] == 1
    assert len(list((tmp_path / "qdrant-memory" / "backups").iterdir())) == 1


def test_plan_replace_archives_the_interrupted_plan_and_backup_reference(tmp_path, monkeypatch):
    """An explicit replacement keeps the old plan and its recovery point on disk."""
    cfg = embedded_cfg(tmp_path)
    store = legacy_store(tmp_path, cfg)
    seed(store)
    store.close()
    _, state = run_plan(tmp_path, cfg)
    original_mark = _interrupt_after_backup(monkeypatch)
    with pytest.raises(RuntimeError):
        upgrade.describe(tmp_path, cfg, plan_args(upgrade_action="apply", plan=state["plan_id"]))
    monkeypatch.setattr(upgrade._Context, "mark", original_mark)
    failed = strategy.read_state(tmp_path, ledger_namespace(cfg), upgrade.STATE_SUFFIX)

    moved = embedded_cfg(tmp_path, search={"top_k": 9})
    replaced = upgrade.describe(tmp_path, moved, plan_args(upgrade_action="plan", replace=True))
    assert replaced["ok"] is True
    assert replaced["plan_id"] != state["plan_id"]
    assert replaced["previous_plan"]["plan_id"] == state["plan_id"]
    assert replaced["previous_plan"]["backup"] == failed["steps"]["backup"]
    archive = strategy.state_path(
        tmp_path, ledger_namespace(cfg), replaced["previous_plan"]["archive"]
    )
    assert archive.is_file()
    archived = strategy.read_state(
        tmp_path, ledger_namespace(cfg), replaced["previous_plan"]["archive"]
    )
    assert archived["steps"]["backup"] == failed["steps"]["backup"]
    assert archived["archived_at"]


def test_apply_does_not_overwrite_a_plan_replaced_after_the_lease(tmp_path, monkeypatch):
    """A plan that changes while the lock is acquired is refused, not overwritten."""
    cfg = embedded_cfg(tmp_path)
    store = legacy_store(tmp_path, cfg)
    store.close()
    _, state = run_plan(tmp_path, cfg)
    namespace = ledger_namespace(cfg)
    replacement = dict(state)
    replacement["instance_id"] = "d" * 32
    replacement["phase"] = upgrade.PHASE_APPLYING
    replacement["updated_at"] = "later"
    original = upgrade.WriterLease.for_config

    class Lease:
        """Publish a newer plan between the preflight read and the lock."""

        @classmethod
        def for_config(cls, home, cfg_value, *, destination_policy=None):
            """Acquire the real lease, then replace the active record."""
            lease = original(home, cfg_value, destination_policy=destination_policy)
            strategy.write_state(home, namespace, upgrade.STATE_SUFFIX, replacement)
            return lease

        def close(self):
            """Release nothing."""

    monkeypatch.setattr(upgrade, "WriterLease", Lease)
    with pytest.raises(upgrade.UpgradePlanStaleError):
        upgrade.describe(tmp_path, cfg, plan_args(upgrade_action="apply", plan=state["plan_id"]))
    assert strategy.read_state(tmp_path, namespace, upgrade.STATE_SUFFIX) == replacement
    assert len(list((tmp_path / "qdrant-memory" / "backups").glob("*"))) == 0


def test_plan_blocks_on_an_admitted_owner_receipt(tmp_path):
    """A receipt with no queued event blocks planning and the saved plan is inert."""
    cfg = embedded_cfg(tmp_path)
    store = legacy_store(tmp_path, cfg)
    seed(store)
    store.close()
    _write_owner_receipt(tmp_path, ledger_namespace(cfg))
    result, state = run_plan(tmp_path, cfg)
    assert result["ok"] is False
    assert "shared_owner_receipt" in {item["code"] for item in result["blockers"]}
    assert state["steps"] == {name: {"status": "pending"} for name in upgrade.STEPS}
    with pytest.raises(upgrade.UpgradeBlockedError) as blocked:
        upgrade.describe(tmp_path, cfg, plan_args(upgrade_action="apply", plan=state["plan_id"]))
    assert "shared_owner_receipt" in {item["code"] for item in blocked.value.blockers}
    assert strategy.read_state(tmp_path, ledger_namespace(cfg), upgrade.STATE_SUFFIX) == state


def test_owner_receipt_appearing_after_plan_blocks_apply_until_recovered(tmp_path):
    """The in-lease preflight catches a receipt that was not present at plan time."""
    cfg = embedded_cfg(tmp_path)
    store = legacy_store(tmp_path, cfg)
    seed(store)
    store.close()
    _, state = run_plan(tmp_path, cfg)
    _write_owner_receipt(tmp_path, ledger_namespace(cfg))
    with pytest.raises(upgrade.UpgradeBlockedError) as blocked:
        upgrade.describe(tmp_path, cfg, plan_args(upgrade_action="apply", plan=state["plan_id"]))
    assert {item["code"] for item in blocked.value.blockers} == {"shared_owner_receipt"}
    assert strategy.read(tmp_path, ledger_namespace(cfg)) is None
    assert strategy.read_state(tmp_path, ledger_namespace(cfg), upgrade.STATE_SUFFIX) == state

    _scrub_owner_receipt(tmp_path, ledger_namespace(cfg))
    applied = upgrade.describe(
        tmp_path, cfg, plan_args(upgrade_action="apply", plan=state["plan_id"])
    )
    assert applied["phase"] == upgrade.PHASE_READY


def test_resume_retries_the_interlock_after_owner_recovery(tmp_path, monkeypatch):
    """A receipt that arrives before resume blocks it, and recovery reopens the same plan."""
    cfg = embedded_cfg(tmp_path)
    store = legacy_store(tmp_path, cfg)
    seed(store)
    store.close()
    _, state = run_plan(tmp_path, cfg)
    original_mark = _interrupt_after_backup(monkeypatch)
    with pytest.raises(RuntimeError):
        upgrade.describe(tmp_path, cfg, plan_args(upgrade_action="apply", plan=state["plan_id"]))
    monkeypatch.setattr(upgrade._Context, "mark", original_mark)
    _write_owner_receipt(tmp_path, ledger_namespace(cfg))
    with pytest.raises(upgrade.UpgradeBlockedError) as blocked:
        upgrade.describe(tmp_path, cfg, plan_args(upgrade_action="resume", plan=state["plan_id"]))
    assert "shared_owner_receipt" in {item["code"] for item in blocked.value.blockers}
    interrupted = strategy.read_state(tmp_path, ledger_namespace(cfg), upgrade.STATE_SUFFIX)
    assert interrupted["phase"] == upgrade.PHASE_FAILED
    assert interrupted["steps"]["backup"]["status"] == "running"

    _scrub_owner_receipt(tmp_path, ledger_namespace(cfg))
    resumed = upgrade.describe(
        tmp_path, cfg, plan_args(upgrade_action="resume", plan=state["plan_id"])
    )
    assert resumed["phase"] == upgrade.PHASE_READY
    assert resumed["steps"]["backup"]["recovered"] is True
    assert strategy.read(tmp_path, ledger_namespace(cfg))["generation"] == 1


def test_terminal_owner_receipt_does_not_block_an_upgrade(tmp_path):
    """A crash leftover after a terminal event is not pending replay work."""
    cfg = embedded_cfg(tmp_path)
    store = legacy_store(tmp_path, cfg)
    store.close()
    _write_owner_receipt(tmp_path, ledger_namespace(cfg), event_status="COMMITTED")
    result, state = run_plan(tmp_path, cfg)
    assert result["ok"] is True
    assert "shared_owner_receipt" not in {item["code"] for item in result["blockers"]}
    applied = upgrade.describe(
        tmp_path, cfg, plan_args(upgrade_action="apply", plan=state["plan_id"])
    )
    assert applied["phase"] == upgrade.PHASE_READY


def test_incomplete_shared_request_blocks_planning(tmp_path):
    """The shared-request half of the reused interlock still reports its blocker."""
    cfg = embedded_cfg(tmp_path)
    store = legacy_store(tmp_path, cfg)
    store.close()
    _write_shared_request(tmp_path, ledger_namespace(cfg), done=0)
    result, _ = run_plan(tmp_path, cfg)
    assert result["ok"] is False
    assert "shared_pending" in {item["code"] for item in result["blockers"]}


def test_ready_apply_rechecks_an_owner_receipt(tmp_path):
    """A receipt admitted after ready still blocks a repeated apply."""
    cfg = embedded_cfg(tmp_path)
    store = legacy_store(tmp_path, cfg)
    store.close()
    _, state = run_plan(tmp_path, cfg)
    upgrade.describe(tmp_path, cfg, plan_args(upgrade_action="apply", plan=state["plan_id"]))
    _write_owner_receipt(tmp_path, ledger_namespace(cfg))
    with pytest.raises(upgrade.UpgradeBlockedError) as blocked:
        upgrade.describe(tmp_path, cfg, plan_args(upgrade_action="apply", plan=state["plan_id"]))
    assert "shared_owner_receipt" in {item["code"] for item in blocked.value.blockers}
    assert (
        strategy.read_state(tmp_path, ledger_namespace(cfg), upgrade.STATE_SUFFIX)["phase"]
        == upgrade.PHASE_READY
    )

    _scrub_owner_receipt(tmp_path, ledger_namespace(cfg))
    repeated = upgrade.describe(
        tmp_path, cfg, plan_args(upgrade_action="apply", plan=state["plan_id"])
    )
    assert repeated["already_applied"] is True


def test_replace_keeps_every_archive_when_the_plan_identifier_is_stable(tmp_path, monkeypatch):
    """Two replacements of one deterministic plan never overwrite an archive."""
    cfg = embedded_cfg(tmp_path)
    store = legacy_store(tmp_path, cfg)
    seed(store)
    store.close()
    _, state = run_plan(tmp_path, cfg)
    original_mark = _interrupt_after_backup(monkeypatch)
    with pytest.raises(RuntimeError):
        upgrade.describe(tmp_path, cfg, plan_args(upgrade_action="apply", plan=state["plan_id"]))
    monkeypatch.setattr(upgrade._Context, "mark", original_mark)

    first = upgrade.describe(tmp_path, cfg, plan_args(upgrade_action="plan", replace=True))
    second = upgrade.describe(tmp_path, cfg, plan_args(upgrade_action="plan", replace=True))
    assert first["plan_id"] == second["plan_id"] == state["plan_id"]
    assert first["previous_plan"]["archive"] != second["previous_plan"]["archive"]
    archives = sorted(
        strategy.directory(tmp_path).glob(f"*.{upgrade.STATE_SUFFIX}-replaced-*.json")
    )
    assert len(archives) == 2
    archived = [json.loads(path.read_text(encoding="utf-8")) for path in archives]
    assert any(item["steps"]["backup"].get("status") == "running" for item in archived)
    assert all(item.get("archived_at") for item in archived)


def test_partial_backup_without_a_completion_marker_is_not_adopted(tmp_path):
    """A crash during the copy leaves a directory that resume must not trust."""
    import shutil

    cfg = embedded_cfg(tmp_path)
    store = legacy_store(tmp_path, cfg)
    seed(store)
    store.close()
    _, state = run_plan(tmp_path, cfg)
    namespace = ledger_namespace(cfg)
    fake = tmp_path / "qdrant-memory" / "backups" / f"20260101T000000Z-{state['instance_id'][:12]}"
    fake.mkdir(parents=True)
    shutil.copytree(cfg["qdrant"]["path"], fake / "qdrant")
    state["phase"] = upgrade.PHASE_FAILED
    state["steps"]["backup"] = {"status": "running"}
    strategy.write_state(tmp_path, namespace, upgrade.STATE_SUFFIX, state)

    resumed = upgrade.describe(
        tmp_path, cfg, plan_args(upgrade_action="resume", plan=state["plan_id"])
    )
    assert resumed["phase"] == upgrade.PHASE_READY
    assert resumed["steps"]["backup"].get("recovered") is not True
    adopted = tmp_path / resumed["steps"]["backup"]["path"]
    assert (adopted / upgrade.BACKUP_MARKER).is_file()
    assert adopted != fake
    assert len(list((tmp_path / "qdrant-memory" / "backups").iterdir())) == 2


def test_failure_recording_does_not_mask_the_upgrade_error(tmp_path, monkeypatch):
    """A control-plane write failure must not replace the original apply error."""
    cfg = embedded_cfg(tmp_path)
    store = legacy_store(tmp_path, cfg)
    store.close()
    _, state = run_plan(tmp_path, cfg)
    _interrupt_after_backup(monkeypatch)
    original_write = strategy.write_state

    def failing(home, namespace, suffix, record):
        """Refuse only the failure record, leaving earlier writes intact."""
        if record.get("phase") == upgrade.PHASE_FAILED:
            raise ValueError("simulated publication failure")
        return original_write(home, namespace, suffix, record)

    monkeypatch.setattr(upgrade.strategy, "write_state", failing)
    with pytest.raises(RuntimeError, match="simulated crash"):
        upgrade.describe(tmp_path, cfg, plan_args(upgrade_action="apply", plan=state["plan_id"]))


def test_replacement_does_not_adopt_a_backup_from_an_earlier_instance(tmp_path, monkeypatch):
    """A new plan instance never treats the old maintenance point as its own."""
    cfg = embedded_cfg(tmp_path)
    store = legacy_store(tmp_path, cfg)
    seed(store)
    store.close()
    _, state = run_plan(tmp_path, cfg)
    original_mark = _interrupt_after_backup(monkeypatch)
    with pytest.raises(RuntimeError):
        upgrade.describe(tmp_path, cfg, plan_args(upgrade_action="apply", plan=state["plan_id"]))
    monkeypatch.setattr(upgrade._Context, "mark", original_mark)
    failed = strategy.read_state(tmp_path, ledger_namespace(cfg), upgrade.STATE_SUFFIX)
    old_instance = failed["instance_id"]
    replaced = upgrade.describe(tmp_path, cfg, plan_args(upgrade_action="plan", replace=True))
    assert replaced["plan_id"] == state["plan_id"]
    new_state = strategy.read_state(tmp_path, ledger_namespace(cfg), upgrade.STATE_SUFFIX)
    assert new_state["instance_id"] != old_instance

    resumed = upgrade.describe(
        tmp_path, cfg, plan_args(upgrade_action="resume", plan=state["plan_id"])
    )
    assert resumed["phase"] == upgrade.PHASE_READY
    assert resumed["steps"]["backup"].get("recovered") is not True
    new_backup = tmp_path / resumed["steps"]["backup"]["path"]
    assert (
        json.loads((new_backup / upgrade.BACKUP_MARKER).read_text())["instance_id"]
        == new_state["instance_id"]
    )
    assert len(list((tmp_path / "qdrant-memory" / "backups").iterdir())) == 2


def test_plain_replan_of_an_unexecuted_plan_does_not_archive(tmp_path):
    """A plan that never ran may be replaced without an archive or backup claim."""
    cfg = embedded_cfg(tmp_path)
    store = legacy_store(tmp_path, cfg)
    store.close()
    _, state = run_plan(tmp_path, cfg)
    repeated = upgrade.describe(tmp_path, cfg, plan_args(upgrade_action="plan"))
    assert repeated["plan_id"] == state["plan_id"]
    assert repeated["previous_plan"] is None
    assert not list(strategy.directory(tmp_path).glob(f"*.{upgrade.STATE_SUFFIX}-replaced-*.json"))


def test_legacy_backup_without_a_marker_is_adopted_on_first_resume(tmp_path, monkeypatch):
    """A 0.7.0 backup survives the first 0.7.1 resume instead of being duplicated."""
    cfg = embedded_cfg(tmp_path)
    store = legacy_store(tmp_path, cfg)
    seed(store)
    store.close()
    _, state = run_plan(tmp_path, cfg)
    original_mark = _interrupt_after_backup(monkeypatch)
    with pytest.raises(RuntimeError):
        upgrade.describe(tmp_path, cfg, plan_args(upgrade_action="apply", plan=state["plan_id"]))
    monkeypatch.setattr(upgrade._Context, "mark", original_mark)
    backups = list((tmp_path / "qdrant-memory" / "backups").iterdir())
    assert len(backups) == 1
    legacy = backups[0].with_name(f"20260101T000000Z-{state['plan_id'][:12]}")
    backups[0].rename(legacy)
    (legacy / upgrade.BACKUP_MARKER).unlink()
    failed = strategy.read_state(tmp_path, ledger_namespace(cfg), upgrade.STATE_SUFFIX)
    failed.pop("instance_id", None)
    failed["phase"] = upgrade.PHASE_FAILED
    failed["steps"]["backup"] = {"status": "running"}
    strategy.write_state(tmp_path, ledger_namespace(cfg), upgrade.STATE_SUFFIX, failed)

    resumed = upgrade.describe(
        tmp_path, cfg, plan_args(upgrade_action="resume", plan=state["plan_id"])
    )
    assert resumed["phase"] == upgrade.PHASE_READY
    assert resumed["steps"]["backup"]["recovered"] is True
    assert (tmp_path / resumed["steps"]["backup"]["path"]) == legacy
    assert len(list((tmp_path / "qdrant-memory" / "backups").iterdir())) == 1


def test_reused_plan_reports_current_shared_blockers(tmp_path, monkeypatch):
    """A normal plan of an interrupted record refreshes only shared blockers."""
    cfg = embedded_cfg(tmp_path)
    store = legacy_store(tmp_path, cfg)
    seed(store)
    store.close()
    _, state = run_plan(tmp_path, cfg)
    original_mark = _interrupt_after_backup(monkeypatch)
    with pytest.raises(RuntimeError):
        upgrade.describe(tmp_path, cfg, plan_args(upgrade_action="apply", plan=state["plan_id"]))
    monkeypatch.setattr(upgrade._Context, "mark", original_mark)
    _write_owner_receipt(tmp_path, ledger_namespace(cfg))
    reused = upgrade.describe(tmp_path, cfg, plan_args(upgrade_action="plan"))
    assert reused["ok"] is False
    assert "shared_owner_receipt" in {item["code"] for item in reused["blockers"]}
    assert reused["plan_id"] == state["plan_id"]


def test_resume_rejects_a_done_activation_without_the_policy_record(tmp_path):
    """A done activate step is re-verified before the plan can become ready."""
    cfg = embedded_cfg(tmp_path)
    store = legacy_store(tmp_path, cfg)
    store.close()
    _, state = run_plan(tmp_path, cfg)
    upgrade.describe(tmp_path, cfg, plan_args(upgrade_action="apply", plan=state["plan_id"]))
    ready = strategy.read_state(tmp_path, ledger_namespace(cfg), upgrade.STATE_SUFFIX)
    strategy.record_path(tmp_path, ledger_namespace(cfg)).unlink()
    ready["phase"] = upgrade.PHASE_APPLYING
    strategy.write_state(tmp_path, ledger_namespace(cfg), upgrade.STATE_SUFFIX, ready)
    with pytest.raises(upgrade.UpgradeStateError):
        upgrade.describe(tmp_path, cfg, plan_args(upgrade_action="resume", plan=state["plan_id"]))
