"""Exercise failed-provider hooks and recovery across a real process boundary."""

import json
import subprocess
import sys
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace

import utils

from qdrant_memory.provider import QdrantMemoryProvider

from .helpers import LLM, Embedder, config


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
p.shutdown()
p.shutdown()
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
          "count": p.store.count(), "events": p.ledger.stats()["events"],
          "lease_exclusive": True}
assert p.store.count() == 2
p.shutdown()
print(json.dumps(result), flush=True)
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

    def read_json(timeout=15):
        lines = []
        done = Event()

        def read_line():
            lines.append(proc.stdout.readline())
            done.set()

        Thread(target=read_line, daemon=True).start()
        assert done.wait(timeout), "child provider did not report lifecycle status"
        assert lines[0], f"child provider exited with code {proc.poll()}"
        return json.loads(lines[0])

    try:
        failed = read_json()
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

        recovered = read_json()
        assert recovered == {
            "state": "READY",
            "owner_memory": 1,
            "dog_found": True,
            "count": 2,
            "events": {"COMMITTED": 2},
            "lease_exclusive": True,
        }
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
