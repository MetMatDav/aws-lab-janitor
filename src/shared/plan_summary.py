"""A bounded summary of a plan, small enough to store in an approval record."""

import math

from shared.text import one_line

SUMMARY_CANDIDATE_LIMIT = 50


def summarize_plan(plan: dict, limit: int = SUMMARY_CANDIDATE_LIMIT) -> dict:
    """Totals for the whole plan plus its first ``limit`` candidates in plan order.

    The size is bounded by ``limit``, so the summary always fits a DynamoDB item.
    """
    candidates = plan["candidates"]
    known_costs = [
        c["estimatedMonthlyUsd"] for c in candidates if c["estimatedMonthlyUsd"] is not None
    ]
    return {
        "runId": plan["runId"],
        "generatedAt": plan["generatedAt"],
        "candidateCount": len(candidates),
        "estimatedMonthlyUsd": round(math.fsum(known_costs), 2),
        "errorCount": len(plan["errors"]),
        "regions": sorted({c["region"] for c in candidates}),
        "candidates": [
            {
                "region": c["region"],
                "kind": c["kind"],
                "resourceId": c["resourceId"],
                "reason": c["reason"],
                "estimatedMonthlyUsd": c["estimatedMonthlyUsd"],
            }
            for c in candidates[:limit]
        ],
        "truncated": len(candidates) > limit,
    }


def render_summary(summary: dict) -> str:
    """One line per candidate, plus a final line counting the candidates left out."""
    lines = []
    for item in summary["candidates"]:
        line = (
            f"[{one_line(item['kind'])}] {one_line(item['resourceId'])} "
            f"({one_line(item['region'])}) {one_line(item['reason'])}"
        )
        # .get: a summary read back from DynamoDB has no key for an unknown (None) cost.
        cost = item.get("estimatedMonthlyUsd")
        if cost is not None:
            line += f" ~${cost:.2f}/mo"
        lines.append(line)
    omitted = summary["candidateCount"] - len(summary["candidates"])
    if summary["truncated"]:
        lines.append(f"... and {omitted} more")
    return "\n".join(lines)
