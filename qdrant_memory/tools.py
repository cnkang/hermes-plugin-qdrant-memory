"""Small memory tool surface; caller scope cannot be overridden by tool arguments."""

from .models import payload

MAX_ARGUMENT_BYTES = 65536


def validate_arguments(name, args, catalogue=None):
    """Bound all tool strings before normalization, hashing, embedding or storage."""
    definition = next((item for item in (catalogue or schemas()) if item["name"] == name), None)
    if definition is None:
        raise ValueError("Unknown memory tool")
    fields = definition["parameters"]
    if not isinstance(args, dict) or set(args) - set(fields["properties"]):
        raise ValueError("Unexpected memory tool arguments")
    if set(fields["required"]) - set(args):
        raise ValueError("Missing memory tool arguments")
    for value in args.values():
        if not isinstance(value, str):
            raise ValueError("Memory tool arguments must be strings")
        # The character bound avoids allocating an encoded copy of huge inputs.
        if len(value) > MAX_ARGUMENT_BYTES or len(value.encode()) > MAX_ARGUMENT_BYTES:
            raise ValueError("Memory tool argument exceeds byte limit")


def schemas():
    """Return stable schemas without caller-supplied scope overrides."""
    definitions = {
        "search": (
            "Recall scoped durable memories.",
            {"query": {"type": "string"}, "category": {"type": "string"}},
            ["query"],
        ),
        "add": (
            "Save an explicit durable fact or preference.",
            {"text": {"type": "string"}},
            ["text"],
        ),
        "update": (
            "Replace a scoped memory by its exact ID.",
            {"id": {"type": "string"}, "text": {"type": "string"}},
            ["id", "text"],
        ),
        "delete": ("Delete a scoped memory by its exact ID.", {"id": {"type": "string"}}, ["id"]),
    }
    return [
        {
            "name": "qdrant_memory_" + name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
        }
        for name, (description, properties, required) in definitions.items()
    ]


def dispatch(runtime, name, args, scope, session_id):
    """Execute a scoped tool under the shared lock with exact-ID authorization."""
    validate_arguments(name, args)
    with runtime.lock:
        if name == "qdrant_memory_search":
            filters = {"category": args["category"]} if args.get("category") else None
            return {
                "memories": [
                    {"id": str(h.id), "score": h.score, **h.payload}
                    for h in runtime.store.search(args["query"], scope, filters)
                ]
            }
        if name == "qdrant_memory_add":
            return runtime.add(args["text"], scope, session_id=session_id)
        if name not in {"qdrant_memory_update", "qdrant_memory_delete"}:
            raise ValueError("Unknown memory tool")
        # An ID is not authorization: retrieve and check scope before preparing a mutation.
        old = runtime.store.get(args["id"], scope)
        if old is None:
            raise ValueError("Memory ID not found in caller scope")
        if name == "qdrant_memory_delete":
            key = runtime.operation(
                str(old.id),
                "DELETE",
                {"content_hash": old.payload.get("content_hash", "")},
                source_id=runtime.ledger.delete_fence_source(scope),
            )
        else:
            value = payload(
                args["text"],
                scope,
                old.payload["source"],
                session_id,
                category=old.payload["category"],
                importance=old.payload["importance"],
                metadata=old.payload.get("metadata"),
                cloud_origin=old.payload.get("cloud_origin"),
                created_at=old.payload["created_at"],
            )
            key = runtime.operation(str(old.id), "UPSERT", value)
        runtime.commit([key])
        return {"id": str(old.id), "action": "DELETE" if name.endswith("delete") else "UPDATE"}
