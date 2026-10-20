"""Optional real-model mutation scenarios using the existing Owner evaluation host."""

import json
import secrets
from concurrent.futures import ThreadPoolExecutor

import anyio
import httpx

from qdrant_memory.quality_metrics import (
    MUTATION_CHECKS,
    durable_snapshot,
    matches,
    score_mutations,
)


def is_denied(value):
    """Accept explicit authorization responses; transport/service failures never qualify."""
    if isinstance(value, dict):
        return value.get("error", {}).get("type") == "PermissionError"
    seen, pending = set(), [value]
    while pending:
        error = pending.pop()
        if id(error) in seen:
            continue
        seen.add(id(error))
        if isinstance(error, httpx.HTTPStatusError) and error.response.status_code in (401, 403):
            return True
        if isinstance(error, BaseExceptionGroup):
            pending.extend(error.exceptions)
        if isinstance(error, BaseException) and error.__cause__ is not None:
            pending.append(error.__cause__)
    return False


def evaluate_mutations(core, plan, home, recorder, tokens, adapter):
    """Drive multilingual Host turns, scoped tools, concurrent CAS and persistent restart."""
    checks = dict.fromkeys(MUTATION_CHECKS, False)
    cases = {row["id"]: row for row in plan["cases"]}
    facts = {row["id"]: row["expected"][0] for row in plan["cases"]}
    policy_path = home / "policy.json"
    provider, manager = core.start_host(home, policy_path, recorder)

    def tool(url, cert, token, name, arguments):
        """Use an independent official HTTPS MCP client for each operation."""

        async def call():
            return await core.call_remote(url, token, name, arguments, ca_file=cert)

        return anyio.run(call)

    def denied(url, cert, token, name, arguments):
        """Require a real denial rather than turning arbitrary outages into success."""
        try:
            return is_denied(tool(url, cert, token, name, arguments))
        except Exception as error:
            return is_denied(error)

    def find(hits, key):
        """Select a returned annotated fact without directly reading the database."""
        return next((hit for hit in hits if matches(hit["text"], facts[key])), None)

    def turn(key, number):
        """Invoke actual Hermes MemoryManager callbacks across two sessions."""
        row = cases[key]
        provider.on_session_switch(row["session"])
        provider.on_turn_start(number, row["user"], session_id=row["session"])
        manager.sync_all(row["user"], "ack", session_id=row["session"])
        if not provider.wait_idle(120) or provider._native_owner.owner.blocked:
            raise RuntimeError("Mutation lifecycle failed or recovery gate blocked")

    delete_args, deletion, deleted_id = None, None, None
    winner, winning_args, winning_token = None, None, None
    after, obsolete = [], 0
    try:
        with core.tls_owner(provider, home) as (url, cert):
            turn("change-old", 1)
            initial = core.remote_query(url, cert, tokens["SECOND"], "Project Willow 缓存有效期")
            old = find(initial, "change-old")
            checks["same_agent_continuation"] = old is not None
            turn("change-new", 2)
            after = core.remote_query(url, cert, tokens["SECOND"], "Project Willow 最新缓存有效期")
            new = find(after, "change-new")
            obsolete = sum(
                matches(hit["text"], facts["change-old"])
                and not matches(hit["text"], facts["change-new"])
                for hit in after
            )
            checks["supersession"] = bool(
                old
                and new
                and old["id"] == new["id"]
                and new["version"] > old["version"]
                and obsolete == 0
            )

            for route, token in (("pi", tokens["PI"]), ("project", tokens["PROJECT"])):
                core.pi_capture(
                    adapter,
                    home / ("mutation-" + route),
                    [row for row in cases.values() if row["route"] == route],
                    url,
                    cert,
                    token,
                    tokens["READER"],
                )
            private = core.remote_query(url, cert, tokens["PI"], "Project Acacia 用户文档语言")
            language = find(private, "language-zh")
            checks["multilingual"] = language is not None
            shared = core.remote_query(
                url, cert, tokens["READER"], "Project Sequoia deployment region"
            )
            decision = find(shared, "handoff-decision")
            checks["cross_agent_handoff"] = decision is not None

            if language:
                deleted_id = language["id"]
                delete_args = {
                    "id": deleted_id,
                    "expected_version": language["version"],
                    "request_id": "mutation-delete-private",
                }
                deletion = tool(url, cert, tokens["PI"], "qdrant_memory_delete", delete_args)
                checks["delete_replay"] = (
                    deletion.get("action") == "DELETE"
                    and tool(url, cert, tokens["PI"], "qdrant_memory_delete", delete_args)
                    == deletion
                )
                checks["deleted_absent"] = all(
                    hit["id"] != deleted_id
                    for hit in core.remote_query(
                        url, cert, tokens["PI"], "Project Acacia 用户文档语言"
                    )
                )

            if decision:
                readonly_args = {
                    "id": decision["id"],
                    "expected_version": decision["version"],
                    "request_id": "mutation-readonly-deny",
                }
                checks["readonly_denied"] = denied(
                    url, cert, tokens["READER"], "qdrant_memory_delete", readonly_args
                )
                document = json.loads(policy_path.read_text())
                second = secrets.token_hex(32)
                grant = next(
                    row
                    for row in document["clients"]
                    if row["token_sha256"] == core.digest(tokens["PROJECT"].encode())
                )
                document["clients"].append(
                    {
                        **grant,
                        "token_sha256": core.digest(second.encode()),
                        "principal": "hermes-project",
                        "logical_agent_id": "hermes",
                        "device_id": "hermes-project",
                    }
                )
                policy_path.write_text(json.dumps(document))
                arguments = [
                    {
                        "id": decision["id"],
                        "expected_version": decision["version"],
                        "request_id": "mutation-cas-" + str(index),
                        "text": "Project Sequoia's approved deployment region is " + city + ".",
                    }
                    for index, city in enumerate(("Kyoto", "Osaka"))
                ]
                credentials = (tokens["PROJECT"], second)
                with ThreadPoolExecutor(max_workers=2) as pool:
                    jobs = [
                        pool.submit(tool, url, cert, token, "qdrant_memory_update", args)
                        for token, args in zip(credentials, arguments)
                    ]
                    results = [job.result() for job in jobs]
                winners = [
                    index for index, value in enumerate(results) if value.get("action") == "UPDATE"
                ]
                conflicts = sum(
                    value.get("error", {}).get("type") == "SharedConflictError" for value in results
                )
                checks["cas_one_winner"] = len(winners) == conflicts == 1
                if len(winners) == 1:
                    index = winners[0]
                    winner, winning_args, winning_token = (
                        results[index],
                        arguments[index],
                        credentials[index],
                    )

            document = json.loads(policy_path.read_text())
            for grant in document["clients"]:
                if grant["token_sha256"] == core.digest(tokens["READER"].encode()):
                    grant["revoked"] = True
            policy_path.write_text(json.dumps(document))
            checks["revocation_denied"] = denied(
                url, cert, tokens["READER"], "qdrant_memory_search", {"query": "Sequoia"}
            )
            native_snapshot = durable_snapshot(
                core.remote_query(url, cert, tokens["SECOND"], "Project Willow 最新缓存有效期")
            )
    finally:
        manager.shutdown_all()

    calls = len(recorder.latencies)
    provider, manager = core.start_host(home, policy_path, recorder)
    try:
        with core.tls_owner(provider, home) as (url, cert):
            stable = native_snapshot == durable_snapshot(
                core.remote_query(url, cert, tokens["SECOND"], "Project Willow 最新缓存有效期")
            )
            stable &= denied(
                url, cert, tokens["READER"], "qdrant_memory_search", {"query": "Sequoia"}
            )
            if delete_args:
                stable &= (
                    tool(url, cert, tokens["PI"], "qdrant_memory_delete", delete_args) == deletion
                )
                stable &= all(
                    hit["id"] != deleted_id
                    for hit in core.remote_query(url, cert, tokens["PI"], "Acacia")
                )
            else:
                stable = False
            if winning_args:
                stable &= (
                    tool(url, cert, winning_token, "qdrant_memory_update", winning_args) == winner
                )
            else:
                stable = False
            checks["restart_durable"] = bool(stable)
    finally:
        manager.shutdown_all()
    checks["restart_no_model_calls"] = len(recorder.latencies) == calls
    return score_mutations(checks, obsolete_hits=obsolete, observed_hits=len(after))
