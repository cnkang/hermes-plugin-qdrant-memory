"""Qdrant named-vector storage with mandatory scope filters and pipeline identity."""

import time

from .config import embedding_config
from .embedding import validate_vectors
from .models import Scope, point_id

# Pipeline identity lives in a reserved point, excluded from memory counts and recall.
IDENTITY_ID = point_id(Scope("__schema__", None), "schema", "embedding")
INDEXES = {
    **dict.fromkeys(
        (
            "user_id",
            "agent_id",
            "session_id",
            "category",
            "source",
            "content_hash",
            "cloud_origin.id",
        ),
        "keyword",
    ),
    "created_at": "datetime",
    "updated_at": "datetime",
    "schema_version": "integer",
}


class CollectionNotInitializedError(ValueError):
    """Signal a missing target collection without exposing endpoint details."""


class CollectionCompatibilityError(ValueError):
    """Reject a collection whose schema or embedding identity cannot be trusted."""


def build_client(cfg):
    """Create the embedded, self-hosted or Cloud Qdrant client."""
    from qdrant_client import QdrantClient

    q = cfg["qdrant"]
    if q["mode"] == "embedded":
        from pathlib import Path

        Path(q["path"]).mkdir(parents=True, exist_ok=True, mode=0o700)
        return QdrantClient(path=q["path"], force_disable_check_same_thread=True)
    return QdrantClient(
        url=q["url"],
        api_key=q["api_key"],
        timeout=q["timeout_seconds"],
        prefer_grpc=q["prefer_grpc"],
    )


def _close_embedded_collection_storage(client, collection_name):
    """Close local SQLite handles before Qdrant removes a collection directory.

    qdrant-client's local delete path ignores directory-removal errors. On Windows,
    an open collection database prevents removal and can make a later reset reload
    the old points into the recreated collection.
    """
    local_client = getattr(client, "_client", None)
    collections = getattr(local_client, "collections", None)
    collection = collections.get(collection_name) if isinstance(collections, dict) else None
    close = getattr(collection, "close", None)
    if not callable(close):
        raise CollectionCompatibilityError(
            "Embedded Qdrant cannot safely reset a collection without closing its local storage"
        )
    close()


def scope_filter(scope, extra=None):
    """Build mandatory user/agent filters plus payload constraints."""
    from qdrant_client import models as m

    conditions = [m.FieldCondition(key="user_id", match=m.MatchValue(value=scope.user_id))]
    if scope.agent_id is None:
        conditions.append(m.IsNullCondition(is_null=m.PayloadField(key="agent_id")))
    else:
        conditions.append(
            m.FieldCondition(key="agent_id", match=m.MatchValue(value=scope.agent_id))
        )
    for key, value in (extra or {}).items():
        conditions.append(m.FieldCondition(key=key, match=m.MatchValue(value=value)))
    return m.Filter(must=conditions)


class QdrantStore:
    """Store named dense vectors with strict pipeline identity and scope boundaries."""

    def __init__(self, client, cfg, embedder):
        """Bind a client, collection settings and embedding pipeline."""
        self.client, self.cfg, self.embedder = client, cfg, embedder
        self.collection = cfg["qdrant"]["collection"]

    def initialize(self, create=True, reset=False):
        """Probe embeddings and create or validate the named dense collection contract."""
        from qdrant_client import models as m

        vectors = self.embedder.embed_documents(["embedding dimension probe"])
        validate_vectors(vectors, 1, self.embedder.dimensions)
        distance = m.Distance(embedding_config(self.cfg)["distance"])
        if reset:
            if not create:
                raise ValueError("Reset requires collection creation")
            if self.client.collection_exists(self.collection):
                if self.cfg["qdrant"]["mode"] == "embedded":
                    _close_embedded_collection_storage(self.client, self.collection)
                self.client.delete_collection(self.collection)
                if self.cfg["qdrant"]["mode"] == "embedded":
                    # Reopen the local engine so its filesystem-backed collection
                    # catalog reflects the completed delete on every platform.
                    self.client.close()
                    self.client = build_client(self.cfg)
                if self.client.collection_exists(self.collection):
                    raise CollectionCompatibilityError(
                        "Qdrant did not confirm collection deletion during reset"
                    )
        if not self.client.collection_exists(self.collection):
            if not create:
                raise CollectionNotInitializedError(
                    "Collection does not exist; run hermes qdrant-memory init"
                )
            self.client.create_collection(
                self.collection,
                vectors_config={
                    "dense": m.VectorParams(size=self.embedder.dimensions, distance=distance)
                },
            )
        info = self.client.get_collection(self.collection)
        schema = info.config.params.vectors
        if not isinstance(schema, dict) or "dense" not in schema:
            raise CollectionCompatibilityError("Collection must have a named dense vector")
        if schema["dense"].size != self.embedder.dimensions or schema["dense"].distance != distance:
            raise CollectionCompatibilityError("Collection dimension or distance mismatch")
        self._validate_identity(vectors[0], create)
        if create and self.cfg["qdrant"]["mode"] != "embedded":
            for key, kind in INDEXES.items():
                self.client.create_payload_index(
                    self.collection, field_name=key, field_schema=kind, wait=True
                )

    def _validate_identity(self, vector, create):
        """Refuse unknown or incompatible pipeline identity before permitting writes."""
        from qdrant_client import models as m

        identity = self.client.retrieve(self.collection, ids=[IDENTITY_ID], with_payload=True)
        if identity:
            if identity[0].payload.get("embedding_fingerprint") != self.embedder.fingerprint:
                raise CollectionCompatibilityError(
                    "Collection embedding fingerprint mismatch; migrate to a new collection"
                )
        else:
            if not create:
                raise CollectionCompatibilityError(
                    "Collection has no trusted embedding fingerprint"
                )
            if self.client.count(self.collection, exact=True).count:
                raise CollectionCompatibilityError(
                    "Nonempty collection has no trusted embedding fingerprint"
                )
            self.client.upsert(
                self.collection,
                points=[
                    m.PointStruct(
                        id=IDENTITY_ID,
                        vector={"dense": vector},
                        payload={
                            "_qdrant_memory_schema": 1,
                            "embedding_fingerprint": self.embedder.fingerprint,
                        },
                    )
                ],
                wait=True,
            )

    def search(self, query, scope, filters=None, top_k=None):
        """Retrieve scoped embedding candidates and record latency metrics."""
        from qdrant_client import models as m

        search = self.cfg["search"]
        started = time.monotonic()
        vector = self.embedder.embed_query(query)
        embedded = time.monotonic()
        hits = self.client.query_points(
            self.collection,
            query=vector,
            using="dense",
            query_filter=scope_filter(scope, filters),
            search_params=m.SearchParams(hnsw_ef=search["hnsw_ef"], exact=search["exact"])
            if self.cfg["qdrant"]["mode"] != "embedded"
            else None,
            limit=search["candidate_k"],
            score_threshold=search["min_score"],
            with_payload=True,
            with_vectors=False,
        ).points
        if hasattr(self, "metrics"):
            self.metrics.measure("embedding_latency_ms", (embedded - started) * 1000)
            self.metrics.measure("qdrant_query_latency_ms", (time.monotonic() - embedded) * 1000)
            self.metrics.measure("search_latency_ms", (time.monotonic() - started) * 1000)
        return hits[: top_k or search["top_k"]]

    def lookup_hash(self, scope, value):
        """Find an exact normalized-content hash in the caller's scope."""
        rows, _ = self.client.scroll(
            self.collection,
            scroll_filter=scope_filter(scope, {"content_hash": value}),
            limit=1,
            with_payload=True,
            with_vectors=False,
        )
        return rows[0] if rows else None

    def get(self, identifier, scope):
        """Retrieve an exact ID only if its payload belongs to the caller."""
        rows = self.client.retrieve(
            self.collection, ids=[identifier], with_payload=True, with_vectors=False
        )
        if rows and all(rows[0].payload.get(k) == v for k, v in scope.as_dict().items()):
            return rows[0]
        return None

    def upsert(self, records):
        """Embed prepared payloads and wait for confirmed batch upsert."""
        from qdrant_client import models as m

        started = time.monotonic()
        vectors = self.embedder.embed_documents([p["text"] for _, p in records])
        if hasattr(self, "metrics"):
            self.metrics.measure("embedding_latency_ms", (time.monotonic() - started) * 1000)
        validate_vectors(vectors, len(records), self.embedder.dimensions)
        self.client.upsert(
            self.collection,
            points=[
                m.PointStruct(id=i, payload=p, vector={"dense": v})
                for (i, p), v in zip(records, vectors)
            ],
            wait=True,
        )

    def delete(self, identifiers):
        """Delete already-authorized point IDs and wait for completion."""
        from qdrant_client import models as m

        self.client.delete(
            self.collection, points_selector=m.PointIdsList(points=identifiers), wait=True
        )

    def count(self, scope=None):
        """Count exact memories, excluding the reserved schema identity point."""
        from qdrant_client import models as m

        filt = (
            scope_filter(scope)
            if scope
            else m.Filter(must_not=[m.HasIdCondition(has_id=[IDENTITY_ID])])
        )
        return self.client.count(self.collection, count_filter=filt, exact=True).count

    def scroll(self, scope=None, with_vectors=False):
        """Yield memory records in bounded pages, optionally including vectors."""
        from qdrant_client import models as m

        offset = None
        filt = (
            scope_filter(scope)
            if scope
            else m.Filter(must_not=[m.HasIdCondition(has_id=[IDENTITY_ID])])
        )
        while True:
            rows, offset = self.client.scroll(
                self.collection,
                scroll_filter=filt,
                limit=128,
                offset=offset,
                with_payload=True,
                with_vectors=with_vectors,
            )
            yield from rows
            if offset is None:
                break

    def close(self):
        """Close the client and release embedded persistence ownership."""
        self.client.close()
