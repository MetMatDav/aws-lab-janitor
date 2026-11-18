"""The README's tables and diagram say what the code says."""

import re
from pathlib import Path

from executor.handler import DELETABLE_KINDS
from lab_janitor.config import DEFAULT_MIN_AGE_DAYS
from shared.findings import KINDS
from shared.pricing import FIXED_HOURLY_USD
from tests.docs import markdown
from tests.infra.helpers import state_machine_definition

# Which kinds each entry of the price table gives an estimate to.
PRICED_KINDS = {
    "nat-gateway": {"nat-gateway"},
    "public-ipv4": {"public-ipv4", "elastic-ip"},
    "alb": {"elbv2"},
    "nlb": {"elbv2"},
    "ebs-gp3-gib": {"ebs-volume"},
    "ebs-snapshot-gib": {"ebs-snapshot"},
    "vpc-endpoint-interface-az": {"vpc-endpoint"},
}
AGED_KINDS = ("ebs-snapshot", "ebs-volume", "elastic-ip")

README = Path("README.md").read_text()


def first_column(heading: str) -> set[str]:
    return {row[0].strip("` ") for row in markdown.tables(README, heading)}


def test_readme_tables_match_the_code():
    detects = first_column("What it detects")
    deletes = first_column("What it can delete, after approval")

    assert detects == set(KINDS)
    assert deletes == set(DELETABLE_KINDS)


def test_never_touches_is_every_kind_that_cannot_be_deleted():
    assert first_column("What it never touches") == set(KINDS) - set(DELETABLE_KINDS)


def test_readme_diagram_names_only_real_states(template):
    states = set(state_machine_definition(template)["States"])
    (diagram,) = [
        block for block in markdown.mermaid_blocks(README) if block.startswith("stateDiagram")
    ]
    named = set(re.findall(r"\b([A-Z][A-Za-z]+)\b", diagram))

    assert named <= states, sorted(named - states)
    # Every state of the three outcomes is drawn; only the failure pair may be in a note.
    assert states - named == set(), sorted(states - named)


def test_readme_has_the_sections_a_reader_needs():
    headings = [line[3:].strip() for line in README.splitlines() if line.startswith("## ")]

    assert headings == [
        "What it does",
        "How a deletion happens",
        "What it detects",
        "What it can delete, after approval",
        "What it never touches",
        "Try it in dry-run",
        "What it costs",
        "Evidence",
        "How it is tested",
        "Limits",
        "Why not aws-nuke, cloud-nuke or Cloud Custodian",
        "How it was built",
        "License",
    ]


def test_readme_gives_no_test_counts():
    # Counts drift; docs/testing.md carries them with a date.
    assert not re.search(r"\b\d[\d,]* (?:unit |passing )?tests\b", README)


def cost_column_errors(text: str) -> set[str]:
    """Kinds whose "Cost estimate" cell disagrees with the price table."""
    priced = set().union(*PRICED_KINDS.values())
    wrong = set()
    for kind, _what, cost in markdown.tables(text, "What it detects"):
        name = kind.strip("` ")
        if cost not in {"yes", "no"} or (cost == "yes") != (name in priced):
            wrong.add(name)
    return wrong


def minimum_age_errors(text: str) -> set[str]:
    """Kinds whose "Minimum age" cell disagrees with the default or the rule."""
    wrong = set()
    for kind, _when, age in markdown.tables(text, "What it can delete, after approval"):
        name = kind.strip("` ")
        expected = f"{DEFAULT_MIN_AGE_DAYS} days" if name in AGED_KINDS else "none"
        if not age.startswith(expected):
            wrong.add(name)
    return wrong


def drawn_edges(text: str) -> set[tuple[str, str]]:
    (diagram,) = [
        block for block in markdown.mermaid_blocks(text) if block.startswith("stateDiagram")
    ]
    return markdown.state_edges(diagram)


def machine_edges(definition: dict) -> set[tuple[str, str]]:
    """Every transition of the machine; ``*`` is the start and the end, as Mermaid draws them."""
    edges = {("*", definition["StartAt"])}
    for name, state in definition["States"].items():
        targets = [state.get("Next"), state.get("Default")]
        targets += [choice["Next"] for choice in state.get("Choices", [])]
        targets += [catch["Next"] for catch in state.get("Catch", [])]
        if state.get("End") or state["Type"] in {"Succeed", "Fail"}:
            targets.append("*")
        edges |= {(name, target) for target in targets if target}
    return edges


def test_the_price_map_covers_the_whole_price_table():
    assert set(PRICED_KINDS) == set(FIXED_HOURLY_USD)


def test_cost_column_matches_the_price_table():
    assert cost_column_errors(README) == set()


def test_a_wrong_cost_cell_is_caught():
    wrong = README.replace("| `ec2-instance` | EC2 instance | no |", "| `ec2-instance` | x | yes |")
    assert wrong != README
    assert cost_column_errors(wrong) == {"ec2-instance"}


def test_minimum_ages_match_the_default():
    assert minimum_age_errors(README) == set()


def test_a_wrong_minimum_age_is_caught():
    rows = [line for line in README.splitlines() if line.startswith("| `ebs-volume` | Tagged")]
    (row,) = rows
    wrong = README.replace(row, row.replace(f"{DEFAULT_MIN_AGE_DAYS} days", "none"))
    assert minimum_age_errors(wrong) == {"ebs-volume"}


def test_every_transition_drawn_exists_in_the_machine(template):
    assert drawn_edges(README) <= machine_edges(state_machine_definition(template))


def test_a_false_transition_or_an_invented_state_is_caught(template):
    real = machine_edges(state_machine_definition(template))
    skipped = README.replace("HasCandidates --> NewApprovalId", "HasCandidates --> Apply")
    invented = README.replace("Applied --> [*]", "Applied --> autoApprove")

    assert drawn_edges(skipped) - real == {("HasCandidates", "Apply")}
    assert drawn_edges(invented) - real == {("Applied", "autoApprove")}
