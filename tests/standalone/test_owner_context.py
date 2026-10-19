"""Native Owner RPCs preserve their trusted host profile context across threads."""

from contextvars import ContextVar, copy_context

from qdrant_memory.shared_service import SharedOwner

from ..helpers import runtime
from .test_device_grants import grant


def test_owner_uses_captured_host_context_not_rpc_caller(tmp_path, monkeypatch):
    """A host-provided embedder cannot read a foreign/default profile secret scope."""
    profile = ContextVar("synthetic_host_profile", default="foreign")
    rt = runtime(tmp_path)
    token = "a" * 40
    marker = profile.set("owner")
    owner = SharedOwner(rt, [grant(token, "mac")], execution_context=copy_context())
    profile.reset(marker)
    original = rt.store.embedder.embed_query

    def embed(text):
        assert profile.get() == "owner"
        return original(text)

    monkeypatch.setattr(rt.store.embedder, "embed_query", embed)
    try:
        saved = owner.call(token, "qdrant_memory_add", {"text": "cats", "request_id": "ctx"})
        assert saved["action"] == "ADD"
        assert owner.call(token, "qdrant_memory_search", {"query": "cats"})["memories"]
        assert profile.get() == "foreign"
    finally:
        rt.ledger.close()
        rt.store.close()
