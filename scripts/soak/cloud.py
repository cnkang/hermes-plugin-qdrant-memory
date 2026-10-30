"""Independent cloud cleanup audit for one soak run.

The cloud window writes into a fixed test collection, so the only acceptable
cleanup is the removal of the records that carry this run's owner identity. This
module re-reads the collection and fails when any such record remains, when the
collection disappeared, or when the run never recorded a cleanup result.
"""

import argparse
import os
from pathlib import Path

from .common import read_json


class CleanupError(RuntimeError):
    """The cloud collection does not match the run's recorded cleanup."""


def verify_cleanup(report, *, url, api_key, collection=None):
    """Re-read the collection and prove the run's records are gone."""
    from qdrant_client import QdrantClient, models

    if not isinstance(report, dict):
        raise CleanupError("report.json is missing or unreadable")
    cleanup = report.get("cloud_cleanup") or {}
    owner_id = cleanup.get("owner_id")
    name = collection or cleanup.get("collection")
    if not owner_id or not name:
        raise CleanupError("the report holds no cloud cleanup record")
    if cleanup.get("deleted") is not True:
        raise CleanupError("the harness did not confirm its own cleanup")

    previous = os.environ.get("SSL_CERT_FILE")
    os.environ.pop("SSL_CERT_FILE", None)
    try:
        client = QdrantClient(url=url, api_key=api_key, timeout=30)
        try:
            if not client.collection_exists(name):
                raise CleanupError("the fixed test collection is missing")
            selector = models.Filter(
                must=[models.FieldCondition(key="user_id", match=models.MatchValue(value=owner_id))]
            )
            remaining = client.count(name, count_filter=selector, exact=True).count
            total = client.count(name, exact=True).count
        finally:
            client.close()
    finally:
        if previous is not None:
            os.environ["SSL_CERT_FILE"] = previous
    if remaining:
        raise CleanupError(f"{remaining} record(s) of this run remain in {name}")
    return {
        "collection": name,
        "owner_id": owner_id,
        "records_before": cleanup.get("records_before"),
        "remaining": remaining,
        "collection_points": total,
        "collection_retained": True,
        "status": "PASS",
    }


def main(argv=None):
    """Audit ``--report`` against the live collection and print the result."""
    parser = argparse.ArgumentParser(prog="soak-cloud-audit")
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--url", default=os.environ.get("SOAK_QDRANT_URL", ""))
    parser.add_argument("--api-key", default=os.environ.get("SOAK_QDRANT_API_KEY", ""))
    parser.add_argument("--collection", default=os.environ.get("SOAK_QDRANT_COLLECTION", ""))
    args = parser.parse_args(argv)
    path = args.report / "report.json" if args.report.is_dir() else args.report
    try:
        if not args.url or not args.api_key:
            raise CleanupError("the cloud endpoint or credential is not configured")
        result = verify_cleanup(
            read_json(path), url=args.url, api_key=args.api_key, collection=args.collection or None
        )
    except CleanupError as error:
        print(f"CLEANUP_FAILED: {error}")
        return 1
    print(
        "Cleanup verified: collection {collection} retains {collection_points} point(s)"
        " and none of them belong to run scope {owner_id}.".format(**result)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
