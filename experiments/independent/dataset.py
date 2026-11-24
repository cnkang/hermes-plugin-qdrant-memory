"""Expand the hand-authored, per-question dataset; no slice-wide relevance IDs."""

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def load_dataset():
    """Expand authored groups into scoped memories and per-question annotations."""
    data = json.loads((ROOT / "dataset.json").read_text())
    memories, queries = [], []
    for group in data:
        gid = group["id"]
        for key, value in group["memories"].items():
            text, scope, deleted = (
                value if isinstance(value, list) else [value, ["fictional", "hermes"], False]
            )
            memories.append(
                dict(
                    id=f"{gid}/{key}",
                    text=text,
                    scope=scope,
                    deleted=deleted,
                    fact_group_id=gid,
                    split=group["split"],
                )
            )
        for i, q in enumerate(group["questions"], 1):
            text, tags, correct, related, conflict, reason, *scope = q
            queries.append(
                dict(
                    query_id=f"{gid}/q{i}",
                    fact_group_id=gid,
                    split=group["split"],
                    query=text,
                    tags=tags,
                    scope=scope[0] if scope else ["fictional", "hermes"],
                    correct_ids=[f"{gid}/{x}" for x in correct],
                    related_ids=[f"{gid}/{x}" for x in related],
                    conflict_ids=[f"{gid}/{x}" for x in conflict],
                    rationale=reason,
                )
            )
    validate(memories, queries)
    return memories, queries


def validate(memories, queries):
    """Reject invalid evidence, scopes and split leakage."""
    by_id = {m["id"]: m for m in memories}
    assert len(by_id) == len(memories), "duplicate memory IDs"
    assert len({q["query_id"] for q in queries}) == len(queries), "duplicate query IDs"
    assert len({q["query"] for q in queries}) == len(queries), "duplicate question text"
    splits = {}
    for q in queries:
        assert q["split"] in {"dev", "holdout"}
        splits.setdefault(q["fact_group_id"], set()).add(q["split"])
        assert q["rationale"] and q["tags"] and len(q["scope"]) == 2
        sets = [set(q[k]) for k in ("correct_ids", "related_ids", "conflict_ids")]
        assert not any(sets[i] & sets[j] for i in range(3) for j in range(i)), q["query_id"]
        for key in ("correct_ids", "related_ids", "conflict_ids"):
            assert len(q[key]) == len(set(q[key]))
            assert all(x in by_id for x in q[key]), q["query_id"]
        assert bool(q["correct_ids"]) != ("no_answer" in q["tags"]), q["query_id"]
        for mid in q["correct_ids"] + q["related_ids"]:
            assert by_id[mid]["scope"] == q["scope"] and not by_id[mid]["deleted"], q["query_id"]
        if "stale_fact" in q["tags"]:
            assert q["correct_ids"] and q["conflict_ids"] and "historical" not in q["tags"]
    assert all(len(s) == 1 for s in splits.values()), "fact-group split leakage"


def digest(path):
    """Hash exact artifact bytes for the frozen manifest."""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def inventory(queries):
    """Count queries and independent fact groups per tag."""
    result = {}
    for split in ("dev", "holdout", "all"):
        rows = [q for q in queries if split == "all" or q["split"] == split]
        result[split] = {
            tag: {
                "queries": sum(tag in q["tags"] for q in rows),
                "fact_groups": len({q["fact_group_id"] for q in rows if tag in q["tags"]}),
            }
            for tag in sorted({t for q in rows for t in q["tags"]})
        }
        result[split]["total"] = dict(
            queries=len(rows), fact_groups=len({q["fact_group_id"] for q in rows})
        )
    return result


if __name__ == "__main__":
    _, questions = load_dataset()
    print(json.dumps(inventory(questions), ensure_ascii=False, indent=2))
