"""Preregistered decision, separate from execution success."""


def decide(result):
    """Apply preregistered evidence and safety requirements."""
    if result.get("safety_failure") or result.get("lifecycle", {}).get("status") == "FAIL":
        return dict(verdict="NO-GO", reasons=["scope/deletion/lifecycle safety failure"])
    if result.get("execution_status") != "SUCCESS":
        return dict(
            verdict="INCONCLUSIVE", reasons=[result.get("error", result["execution_status"])]
        )
    if result["split"] != "holdout":
        return dict(verdict="INCONCLUSIVE", reasons=["development data is exploratory"])
    quality = result["quality"]
    checks, insufficient = [], []

    def bound(name, record, limit, direction, groups=10):
        if not record or record["fact_groups"] < groups:
            insufficient.append(name + ": insufficient independent fact groups")
            return
        lo, hi = record["ci95"]
        if direction == "lower":
            state = "PASS" if lo >= limit else "FAIL" if hi < limit else "UNCERTAIN"
        else:
            state = "PASS" if hi <= limit else "FAIL" if lo > limit else "UNCERTAIN"
        checks.append(dict(name=name, state=state, **record))

    mixed = quality.get("mixed", {})
    if mixed.get("answered_queries", 0) < 30:
        insufficient.append("mixed: fewer than 30 answered questions")
    record = mixed.get("paired", {}).get("recall")
    bound("mixed Recall gain strictly positive", record, 1e-12, "lower", 20)
    if record and record["delta"] < 0.05:
        checks.append(
            dict(
                name="mixed point gain >=0.05",
                state="FAIL" if record["ci95"][1] < 0.05 else "UNCERTAIN",
            )
        )
    for tag in ("chinese", "english", "code", "identifiers"):
        for key in ("recall", "ndcg"):
            bound(
                f"{tag} {key} noninferiority",
                quality.get(tag, {}).get("paired", {}).get(key),
                -0.02,
                "lower",
            )
    stale = quality.get("stale_fact", {})
    if stale.get("queries", 0) < 30:
        insufficient.append("stale_fact: fewer than 30 questions")
    bound(
        "stale-before-current regression",
        stale.get("stale_before", {}).get("paired"),
        0.02,
        "upper",
        20,
    )
    bound(
        "current fact presence", stale.get("current_present", {}).get("paired"), -0.02, "lower", 20
    )
    bound(
        "historical accuracy",
        quality.get("historical", {}).get("historical_accuracy", {}).get("paired"),
        -0.02,
        "lower",
    )
    bound(
        "no-answer interference",
        quality.get("no_answer", {}).get("interference", {}).get("paired"),
        0.02,
        "upper",
    )
    latency = result.get("latency", {}).get("e2e", {})
    rounds = [k for k in latency if k != "pooled"]
    if len(rounds) < 3 or any(latency[k]["dense"]["n"] < 100 for k in rounds):
        insufficient.append("warm E2E: require 3 rounds with >=100 pairs each")
    for key, stats in latency.items():
        lo, hi = stats["p95_ratio_ci95"]
        checks.append(
            dict(
                name="warm E2E p95 " + key,
                state="PASS"
                if hi <= 1.20 and stats["delta_percent"] <= 20
                else "FAIL"
                if lo > 1.20
                else "UNCERTAIN",
                ci95=[lo, hi],
            )
        )
    if result.get("lifecycle", {}).get("status") != "PASS":
        insufficient.append("same-volume lifecycle evidence missing")
    if any(c["state"] == "FAIL" for c in checks):
        verdict = "NO-GO"
    elif insufficient or any(c["state"] == "UNCERTAIN" for c in checks):
        verdict = "INCONCLUSIVE"
    else:
        verdict = "GO"
    return dict(verdict=verdict, checks=checks, reasons=insufficient)
