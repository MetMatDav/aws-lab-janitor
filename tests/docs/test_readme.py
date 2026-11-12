"""The README's tables and diagram say what the code says."""

import re
from pathlib import Path

from executor.handler import DELETABLE_KINDS
from shared.findings import KINDS
from tests.docs import markdown
from tests.infra.helpers import state_machine_definition

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
