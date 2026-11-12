"""Human-readable weekly report rendered from a plan document."""

from itertools import groupby

from shared.text import one_line

TITLE = "LAB JANITOR WEEKLY REPORT"
ACCOUNT_CLEAN = "ACCOUNT CLEAN: no billable resources detected"
TRUNCATION_SUFFIX = "\n... truncated, {omitted} lines omitted. Full report: {pointer}"


def count_noun(count: int, noun: str) -> str:
    """``1 deletion``, ``2 deletions``: the count with a regular English plural."""
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def _resource_tokens(item: dict) -> list[str]:
    """``id (region)`` followed by the age token when the age is known."""
    tokens = [one_line(item["resourceId"]), f"({one_line(item['region'])})"]
    if item["ageDays"] is not None:
        tokens.append(f"{item['ageDays']}d")
    return tokens


def _cost_tokens(item: dict) -> list[str]:
    """The ``~$X/mo`` token, or nothing when the cost is unknown."""
    cost = item["estimatedMonthlyUsd"]
    return [] if cost is None else [f"~${cost:.2f}/mo"]


def _join(tokens: list[str]) -> str:
    return " ".join(token for token in tokens if token)


def _header(plan: dict) -> list[str]:
    findings = len(plan["candidates"]) + len(plan["inventory"])
    return [
        TITLE,
        f"Run: {plan['runId']}",
        f"Mode: {'DRY RUN' if plan['dryRun'] else 'LIVE'}",
        f"Regions swept: {len(plan['regions'])}",
        f"Findings: {findings}",
        f"Candidates for deletion: {len(plan['candidates'])}",
        f"Estimated fixed monthly cost of everything detected: ~${plan['estimatedMonthlyUsd']:.2f}",
    ]


def _actionable(plan: dict) -> list[str]:
    lines = [
        _join(
            [
                f"[{item['kind']}]",
                *_resource_tokens(item),
                one_line(item["reason"]),
                *_cost_tokens(item),
            ]
        )
        for item in plan["candidates"]
    ]
    return ["ACTIONABLE", *(lines or ["none"])]


def _inventory(plan: dict) -> list[str]:
    lines: list[str] = []
    by_kind = sorted(plan["inventory"], key=lambda item: item["kind"])
    for kind, group in groupby(by_kind, key=lambda item: item["kind"]):
        items = list(group)
        lines.append(f"[{kind}] {len(items)} resources")
        lines.extend(
            "  " + _join([*_resource_tokens(i), *_cost_tokens(i), one_line(i["detail"])])
            for i in items
        )
    return ["INVENTORY", *(lines or ["none"])]


def _errors(plan: dict) -> list[str]:
    """One line per error; same region, service family and classification collapse to one."""
    groups: dict[tuple[str, str, str], list[dict]] = {}
    for error in plan["errors"]:
        family = error["service"].split(":")[0]
        groups.setdefault((error["region"], family, error["classification"]), []).append(error)
    lines = []
    for (region, family, classification), items in groups.items():
        first = items[0]
        message = one_line(first["error"])
        if len(items) == 1:
            lines.append(f"{region} {first['service']} {classification}: {message}")
        else:
            lines.append(
                f"{region} {family} {classification}: "
                f"{len(items)} occurrences, first message: {message}"
            )
    return ["ERRORS", *lines] if lines else []


def _quiet_regions(plan: dict) -> list[str]:
    """Swept regions with no finding and no error (an error means the region was not fully seen)."""
    seen = {item["region"] for key in ("candidates", "inventory", "errors") for item in plan[key]}
    quiet = [region for region in plan["regions"] if region not in seen]
    return ["REGIONS WITH NOTHING FOUND", ", ".join(quiet) or "none"]


def render_report(plan: dict, bucket: str) -> str:
    """Render the weekly report text for ``plan``; ``bucket`` is where the plan is stored."""
    sections = [_header(plan)]
    if not plan["candidates"] and not plan["inventory"] and not plan["errors"]:
        sections.append([ACCOUNT_CLEAN])
    sections += [_actionable(plan), _inventory(plan), _errors(plan), _quiet_regions(plan)]
    sections.append([f"Full plan: s3://{bucket}/plans/{plan['runId']}/plan.json"])
    return "\n\n".join("\n".join(lines) for lines in sections if lines)


def truncate_report(text: str, limit_bytes: int, pointer: str) -> str:
    """Return ``text`` if it fits in ``limit_bytes`` (UTF-8), else its longest whole-line prefix
    that fits together with a suffix saying how many lines were omitted and where to read them.

    Raises ValueError when not even the suffix alone fits.
    """
    if len(text.encode()) <= limit_bytes:
        return text
    lines = text.split("\n")
    # prefix_bytes[k] is the UTF-8 size of "\n".join(lines[:k]).
    prefix_bytes = [0]
    for index, line in enumerate(lines):
        prefix_bytes.append(prefix_bytes[-1] + len(line.encode()) + (1 if index else 0))
    for kept in range(len(lines) - 1, -1, -1):
        suffix = TRUNCATION_SUFFIX.format(omitted=len(lines) - kept, pointer=pointer)
        if prefix_bytes[kept] + len(suffix.encode()) <= limit_bytes:
            return "\n".join(lines[:kept]) + suffix
    raise ValueError(f"limit of {limit_bytes} bytes cannot hold the truncation notice")
