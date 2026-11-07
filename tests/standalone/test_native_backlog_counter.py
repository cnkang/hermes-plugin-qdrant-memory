"""Native capture backlog drops use their own counter."""

from types import SimpleNamespace

from qdrant_memory.native_owner import NativeOwner


def test_backlog_capture_uses_a_separate_counter():
    """An authorized rule dropped by a full backlog is not a policy denial."""

    class Ledger:
        """Record counter names without touching a database."""

        def __init__(self):
            self.counters = []

        def increment(self, name):
            self.counters.append(name)

    owner = SimpleNamespace(runtime=SimpleNamespace(ledger=Ledger()))
    native = object.__new__(NativeOwner)
    native.__dict__.update(owner=owner, denied_captures=0)
    native._defer_capture(2)
    assert owner.runtime.ledger.counters == ["native_shared_capture_backlog_full"]
    assert native.denied_captures == 0
