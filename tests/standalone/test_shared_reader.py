"""Contract tests for the additive shared reader (no storage, no provider wiring)."""

import pytest

from qdrant_memory.shared_reader import (
    MAX_TEXT_BYTES,
    SharedCredential,
    SharedHit,
    SharedReader,
)

TOKEN = "t" * 48
URL = "http://127.0.0.1:8765"


def credential(space="project-alpha", visibility="project", token=TOKEN):
    """Build a loopback credential for one space."""
    return SharedCredential(url=URL, token=token, space_id=space, visibility=visibility)


def row(memory_id="m1", space="project-alpha", visibility="project", **extra):
    """Build a record shaped exactly like the Owner search payload."""
    payload = {
        "id": memory_id,
        "score": 0.5,
        "text": "fact",
        "shared_protocol": "qdrant-memory-shared-v1",
        "space_id": space,
        "visibility": visibility,
        "created_by": "user",
        "updated_by": "user",
        "version": "6f1f0c1e-6a1a-4d3a-9f38-2f0b6f9c1a2b",
    }
    payload.update(extra)
    return payload


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8765@evil.com/",
        "http://127.0.0.1:",
        "https://127.0.0.1:8765",
        "http://localhost:8765",
        "http://127.0.0.1:8765/path",
        "http://127.0.0.1:8765?x=1",
        "http://127.0.0.1:8765#f",
        "http://127.0.0.1:not-a-port",
        "http://127.0.0.1:0",
        "http://127.0.0.1:70000",
        "127.0.0.1:8765",
        "",
        None,
    ],
)
def test_credential_rejects_every_non_loopback_url(url):
    """Reject userinfo tricks, paths, queries and bad ports with a redacted error."""
    with pytest.raises(ValueError, match="loopback|port"):
        SharedCredential(url=url, token=TOKEN, space_id="project-alpha", visibility="project")


def test_credential_accepts_the_documented_loopback_forms():
    """Accept a literal loopback URL with and without the trailing slash."""
    first = SharedCredential(URL, TOKEN, "project-alpha", "project")
    second = SharedCredential(URL + "/", TOKEN, "project-alpha", "project")
    assert first.space_id == second.space_id == "project-alpha"


@pytest.mark.parametrize(
    "netloc", ["[sensitive-host-label]:8765", "sensitive-host-label\uff1a8765"]
)
def test_url_parser_failures_do_not_reflect_untrusted_input(netloc):
    """Malformed authority errors use the same redacted loopback contract."""
    from qdrant_memory.shared_protocol import validate_loopback_url

    with pytest.raises(
        ValueError, match="^Shared service URL must be literal IPv4 loopback with port$"
    ) as error:
        validate_loopback_url("http://" + netloc)
    assert "sensitive-host-label" not in str(error.value)


def test_credential_and_hit_repr_are_redacted():
    """Never expose the bearer token or memory text through repr."""
    hit = SharedHit(
        memory_id="m1",
        text="secret memory text",
        score=0.5,
        space_id="project-alpha",
        visibility="project",
    )
    assert TOKEN not in repr(credential())
    assert "redacted" in repr(credential())
    assert "secret memory text" not in repr(hit)
    assert "redacted" in repr(hit)


def test_merges_spaces_sorts_and_keeps_provenance():
    """Merge two granted spaces, call the right endpoint and keep provenance."""
    seen = []

    def fake(url, token, name, arguments):
        seen.append((url, token, name, arguments))
        rows = {
            "a" * 40: [row("a", score=0.4, created_by="alice")],
            "b" * 40: [row("b", space="project-beta", score=0.9)],
        }
        return {"memories": rows[token]}

    reader = SharedReader(
        [
            SharedCredential(URL, "a" * 40, "project-alpha", "project"),
            SharedCredential(URL, "b" * 40, "project-beta", "project"),
        ],
        call=fake,
    )
    hits = reader.search("journal mode")
    assert [hit.memory_id for hit in hits] == ["b", "a"]
    assert hits[1].created_by == "alice"
    assert hits[1].version == "6f1f0c1e-6a1a-4d3a-9f38-2f0b6f9c1a2b"
    assert seen == [
        (URL, "a" * 40, "qdrant_memory_search", {"query": "journal mode"}),
        (URL, "b" * 40, "qdrant_memory_search", {"query": "journal mode"}),
    ]


def test_drops_records_without_the_exact_marker_space_and_visibility():
    """Drop wrong markers, spaces, visibilities, sizes and malformed values."""

    def fake(url, token, name, arguments):
        return {
            "memories": [
                row("good"),
                row("wrong-marker", shared_protocol="other"),
                row("wrong-space", space="project-beta"),
                row("wrong-visibility", visibility="shared"),
                row("empty-text", text="   "),
                row("oversized", text="x" * (MAX_TEXT_BYTES + 1)),
                row("bool-score", score=True),
                row("nan-score", score=float("nan")),
                row("inf-score", score=float("inf")),
                row("huge-score", score=10**400),
                row("missing-score", score=None),
                "not-a-dict",
            ]
        }

    reader = SharedReader([credential()], call=fake)
    hits = reader.search("fact")
    assert [hit.memory_id for hit in hits] == ["good"]


def test_rejects_unauthorized_space_before_any_call():
    """Refuse a space outside the configured allowlist before calling the Owner."""
    called = []

    def fake(*args):
        called.append(args)
        return {"memories": []}

    reader = SharedReader([credential()], call=fake)
    with pytest.raises(ValueError, match="not authorized"):
        reader.search("fact", spaces=["project-beta"])
    with pytest.raises(ValueError, match="sequence of names"):
        reader.search("fact", spaces="project-alpha")
    with pytest.raises(ValueError, match="strings"):
        reader.search("fact", spaces=[1])  # type: ignore[list-item]
    with pytest.raises(ValueError, match="unique"):
        reader.search("fact", spaces=["project-alpha", "project-alpha"])
    assert called == []


def test_private_spaces_require_explicit_opt_in():
    """Require an explicit boolean opt-in before a private space is readable."""
    with pytest.raises(ValueError, match="Private spaces"):
        SharedReader([credential(visibility="private")])
    with pytest.raises(ValueError, match="boolean"):
        SharedReader([credential(visibility="private")], allow_private="false")  # type: ignore[arg-type]

    reader = SharedReader(
        [credential(visibility="private")], allow_private=True, call=lambda *a: {"memories": []}
    )
    assert reader.spaces == ("project-alpha",)


def test_rejects_duplicate_space_and_bad_limit():
    """Refuse duplicate space bindings, boolean limits and out-of-range limits."""
    with pytest.raises(ValueError, match="exactly one credential"):
        SharedReader([credential(), credential()])
    with pytest.raises(ValueError, match="limit"):
        SharedReader([credential()], limit=0)
    with pytest.raises(ValueError, match="limit"):
        SharedReader([credential()], limit=True)

    reader = SharedReader([credential()], call=lambda *a: {"memories": []})
    with pytest.raises(ValueError, match="limit"):
        reader.search("fact", limit=True)


def test_rejects_empty_query_and_caps_results():
    """Refuse a blank query and cap the merged result set."""

    def fake(url, token, name, arguments):
        return {"memories": [row(f"m{index}", score=index / 10) for index in range(10)]}

    reader = SharedReader([credential()], call=fake)
    with pytest.raises(ValueError, match="nonempty"):
        reader.search("  ")
    assert len(reader.search("fact", limit=3)) == 3
    assert len(reader.search("fact")) == 8


def test_keeps_the_best_scoring_duplicate():
    """Keep the highest score when a space returns the same record twice."""

    def fake(url, token, name, arguments):
        return {"memories": [row("same", score=0.2), row("same", score=0.8)]}

    reader = SharedReader([credential()], call=fake)
    hits = reader.search("fact")
    assert len(hits) == 1
    assert hits[0].score == 0.8


def test_same_id_in_two_spaces_stays_separate():
    """Do not collapse the same identifier when it belongs to two spaces."""

    def fake(url, token, name, arguments):
        if token == "a" * 40:
            return {"memories": [row("shared-id", score=0.3)]}
        return {"memories": [row("shared-id", space="project-beta", score=0.6)]}

    reader = SharedReader(
        [
            SharedCredential(URL, "a" * 40, "project-alpha", "project"),
            SharedCredential(URL, "b" * 40, "project-beta", "project"),
        ],
        call=fake,
    )
    hits = reader.search("fact")
    assert [hit.space_id for hit in hits] == ["project-beta", "project-alpha"]


def test_propagates_redacted_error_without_reflection():
    """Surface a redacted failure without echoing the remote message."""

    def fake(url, token, name, arguments):
        return {"error": {"type": "PermissionError", "retryable": False}}

    reader = SharedReader([credential()], call=fake)
    with pytest.raises(ValueError, match="Shared search failed"):
        reader.search("fact")


def test_missing_version_and_extra_payload_fields_are_tolerated():
    """Accept records without a version and ignore unknown payload keys."""

    def fake(url, token, name, arguments):
        return {"memories": [row("no-version", version=None, extra_field="ignored")]}

    reader = SharedReader([credential()], call=fake)
    hits = reader.search("fact")
    assert hits[0].version is None


def test_any_error_key_in_the_envelope_fails_the_search():
    """Treat a present error key, even with a null value, as a failed search."""

    def fake(url, token, name, arguments):
        return {"error": None, "memories": [row("partial")]}

    reader = SharedReader([credential()], call=fake)
    with pytest.raises(ValueError, match="Shared search failed"):
        reader.search("fact")


def test_text_cap_matches_the_owner_writer_limit():
    """Keep records up to 65 536 UTF-8 bytes and drop anything larger, including multibyte."""

    def fake(url, token, name, arguments):
        return {
            "memories": [
                row("at-limit", text="x" * 65_536),
                row("over-limit", text="x" * 65_537),
                row("multibyte-over", text="\u4f60" * 32_768),
                row("surrogate", text="\ud800bad"),
            ]
        }

    reader = SharedReader([credential()], call=fake)
    assert [hit.memory_id for hit in reader.search("fact")] == ["at-limit"]
    assert MAX_TEXT_BYTES == 65_536


def test_spaces_accepts_any_sequence_of_strings():
    """Accept a tuple and refuse a generator or a bare string."""

    def fake(url, token, name, arguments):
        return {"memories": []}

    reader = SharedReader([credential()], call=fake)
    assert reader.search("fact", spaces=("project-alpha",)) == []
    assert reader.search("fact", spaces=["project-alpha"]) == []
    with pytest.raises(ValueError, match="sequence of names"):
        reader.search("fact", spaces=iter(["project-alpha"]))


def test_client_rejects_an_oversized_response_body():
    """Bound the response body before JSON parsing."""
    from qdrant_memory.shared_client import MAX_RESPONSE_BYTES, _collect_bounded

    class Stub:
        def __init__(self, chunks):
            self._chunks = chunks

        def iter_raw(self):
            yield from self._chunks

    assert _collect_bounded(Stub([b"{}"])) == b"{}"
    with pytest.raises(ValueError, match="too large"):
        _collect_bounded(Stub([b"x" * (MAX_RESPONSE_BYTES + 1)]))
    with pytest.raises(ValueError, match="too large"):
        _collect_bounded(
            Stub([b"x" * (MAX_RESPONSE_BYTES // 2 + 1), b"y" * (MAX_RESPONSE_BYTES // 2)])
        )


def test_client_parses_bytes_and_guards_the_error_envelope(monkeypatch):
    """Parse the capped body and never trust the shape of a remote error envelope."""
    import qdrant_memory.shared_client as client

    class FakeResponse:
        status_code = 500

        def __init__(self, body):
            self._body = body

        def iter_raw(self):
            yield self._body

    class FakeStream:
        def __init__(self, body):
            self._response = FakeResponse(body)

        def __enter__(self):
            return self._response

        def __exit__(self, *exc):
            return False

    class FakeClient:
        def __init__(self, body):
            self._body = body

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def stream(self, *args, **kwargs):
            return FakeStream(self._body)

    monkeypatch.setattr(
        client.httpx, "Client", lambda **kwargs: FakeClient(b'{"error": "not-a-dict"}')
    )
    assert client.call(
        "http://127.0.0.1:8765", "t" * 40, "qdrant_memory_search", {"query": "q"}
    ) == {"error": {"type": "SharedClientError", "retryable": False}}

    for body in (b"not-json", b"[" * 5000 + b"]" * 5000):
        monkeypatch.setattr(client.httpx, "Client", lambda **kwargs: FakeClient(body))
        # Python versions with an iterative JSON decoder can parse the nested
        # array; it must still fail the object-envelope contract.
        with pytest.raises(
            ValueError, match="^Shared service response is not (valid JSON|a JSON object)$"
        ):
            client.call("http://127.0.0.1:8765", "t" * 40, "qdrant_memory_search", {"query": "q"})

    monkeypatch.setattr(client.httpx, "Client", lambda **kwargs: FakeClient(b"[1, 2]"))
    with pytest.raises(ValueError, match="not a JSON object"):
        client.call("http://127.0.0.1:8765", "t" * 40, "qdrant_memory_search", {"query": "q"})
