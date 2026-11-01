"""Exercise failed-provider hooks and recovery across a real process boundary."""

import json
import subprocess
import sys
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace

import pytest
import utils

from qdrant_memory.ownership import WriterBusyError
from qdrant_memory.provider import QdrantMemoryProvider

from .helpers import LLM, Embedder, config


def _read_child_json(proc, timeout=15):
    """Read one child status line and retain stderr when the child exits early."""
    lines = []
    done = Event()

    def read_line():
        lines.append(proc.stdout.readline())
        done.set()

    Thread(target=read_line, daemon=True).start()
    assert done.wait(timeout), "child provider did not report lifecycle status"
    if not lines[0]:
        try:
            exit_code = proc.wait(timeout=5)
        except subprocess.TimeoutExpired as exc:
            raise AssertionError("child closed stdout but did not exit") from exc
        stderr = proc.stderr.read().strip()
        detail = f"; stderr: {stderr}" if stderr else ""
        raise AssertionError(f"child provider exited with code {exit_code}{detail}")
    return json.loads(lines[0])


def test_read_child_json_includes_stderr_when_child_exits_without_stdout():
    """A failed child keeps its traceback visible in the parent assertion."""
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sys; print('child traceback marker', file=sys.stderr); sys.exit(17)",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    with pytest.raises(AssertionError, match="child provider exited with code 17") as exc_info:
        _read_child_json(proc)

    assert "child traceback marker" in str(exc_info.value)


def test_cross_process_provider_failure_callbacks_and_recovery(tmp_path, monkeypatch):
    """A child process stays unavailable until the existing owner exits."""
    owner = QdrantMemoryProvider(
        plugin_context=SimpleNamespace(llm=LLM()),
        embedder=Embedder(),
        overrides=config(),
    )
    owner.initialize("owner-session", hermes_home=str(tmp_path))
    entered, release = Event(), Event()
    upsert = owner.store.upsert

    def blocked_upsert(records):
        entered.set()
        assert release.wait(15)
        return upsert(records)

    monkeypatch.setattr(owner.store, "upsert", blocked_upsert)
    owner.sync_turn("cats preferred", "ack", session_id="owner-session")
    assert entered.wait(5)

    child = r"""
import json, sys
from types import SimpleNamespace
sys.path[:0] = sys.argv[2:]
from qdrant_memory.ownership import WriterBusyError
from qdrant_memory.provider import ProviderUnavailableError, QdrantMemoryProvider
from tests.helpers import Embedder, LLM, config
p = QdrantMemoryProvider(plugin_context=SimpleNamespace(llm=LLM()), embedder=Embedder(), overrides=config())
try:
    p.initialize("contender-session", hermes_home=sys.argv[1])
except WriterBusyError:
    pass
else:
    raise AssertionError("the owner process must retain its exclusive lease")
p.on_turn_start(1, "", session_id="contender-session")
try:
    p.sync_turn("lost turn", "ack", session_id="contender-session")
except ProviderUnavailableError as exc:
    sync_error = exc.code
else:
    raise AssertionError("an unavailable provider must not acknowledge an unwritten turn")
try:
    p.on_session_end([{"role": "user", "content": "cats"}])
except ProviderUnavailableError as exc:
    session_error = exc.code
else:
    raise AssertionError("session-end persistence must fail explicitly")
try:
    p.on_pre_compress([], require_checkpoint=True)
except ProviderUnavailableError as exc:
    checkpoint_error = exc.code
else:
    raise AssertionError("a required checkpoint must fail closed")
tool_error = json.loads(p.handle_tool_call("qdrant_memory_add", {"text": "cats"}))["error"]
assert p.prefetch("cats", session_id="contender-session") == ""
print(json.dumps({"state": p.lifecycle_state, "sync": sync_error, "session": session_error,
                  "checkpoint": checkpoint_error, "tool": tool_error}), flush=True)
if sys.stdin.readline().strip() != "retry":
    raise AssertionError("missing explicit recovery request")
p.initialize("contender-session", hermes_home=sys.argv[1])
assert p.wait_idle()
owner_hits = json.loads(p.handle_tool_call("qdrant_memory_search", {"query": "cats"}))
assert [m["text"] for m in owner_hits["memories"]] == ["cats preferred"]
p.on_turn_start(1, "", session_id="contender-session")
p.sync_turn("dogs preferred", "ack", session_id="contender-session")
assert p.wait_idle()
p.runtime.recover()
p.runtime.recover()
dogs = json.loads(p.handle_tool_call("qdrant_memory_search", {"query": "dogs"}))
dog_texts = [m["text"] for m in dogs["memories"]]
result = {"state": p.lifecycle_state, "owner_memory": len(owner_hits["memories"]),
          "dog_found": "dogs preferred" in dog_texts,
          "count": p.store.count(), "events": p.ledger.stats()["events"]}
assert p.store.count() == 2
print(json.dumps(result), flush=True)
if sys.stdin.readline().strip() != "close":
    raise AssertionError("missing provider shutdown request")
p.shutdown()
"""
    project_root = str(Path(__file__).resolve().parents[1])
    utils_root = str(Path(utils.__file__).resolve().parent)
    proc = subprocess.Popen(
        [sys.executable, "-c", child, str(tmp_path), project_root, utils_root],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )

    try:
        failed = _read_child_json(proc)
        assert failed["state"] == "FAILED"
        assert failed["sync"] == failed["session"] == failed["checkpoint"] == "writer_busy"
        assert failed["tool"]["code"] == "writer_busy"
        assert failed["tool"]["retryable"] is True

        release.set()
        assert owner.wait_idle()
        assert owner.ledger.stats()["events"]["COMMITTED"] == 1
        assert owner.store.count() == 1
        owner.shutdown()
        proc.stdin.write("retry\n")
        proc.stdin.flush()

        recovered = _read_child_json(proc)
        assert recovered == {
            "state": "READY",
            "owner_memory": 1,
            "dog_found": True,
            "count": 2,
            "events": {"COMMITTED": 2},
        }
        contender = QdrantMemoryProvider(embedder=Embedder(), overrides=config())
        try:
            with pytest.raises(WriterBusyError):
                contender.initialize("third-process", hermes_home=str(tmp_path))
            assert contender.lifecycle_state == "FAILED"
        finally:
            contender.shutdown()
        proc.stdin.write("close\n")
        proc.stdin.flush()
        assert proc.wait(timeout=15) == 0
    finally:
        release.set()
        if owner.lifecycle_state == "READY":
            owner.shutdown()
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
