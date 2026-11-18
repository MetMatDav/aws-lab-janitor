"""The decision records: numbered, complete, indexed, and true to the tests they cite."""

import re
from pathlib import Path

import pytest

ADRS = sorted(Path("docs/adr").glob("[0-9][0-9][0-9][0-9]-*.md"))
SECTIONS = ["Status", "Context", "Decision", "Consequences", "Where to look"]
TEST_FILES = [path for path in Path("tests").rglob("test_*.py")]


def test_there_are_fifteen_numbered_without_gaps():
    assert [path.name[:4] for path in ADRS] == [f"{n:04d}" for n in range(1, 16)]


@pytest.mark.parametrize("adr", ADRS, ids=lambda path: path.name[:4])
def test_adr_has_the_five_sections_in_order(adr):
    headings = [line[3:].strip() for line in adr.read_text().splitlines() if line.startswith("## ")]
    assert headings == SECTIONS


def test_index_lists_every_adr_once():
    index = Path("docs/adr/README.md").read_text()
    for adr in ADRS:
        assert index.count(f"({adr.name})") == 1


def test_every_adr_cited_in_code_exists():
    cited = set()
    for path in [*Path("lab_janitor").rglob("*.py"), *Path("src").rglob("*.py")]:
        cited |= set(re.findall(r"ADR-(\d{4})", path.read_text()))
    assert cited
    assert cited <= {path.name[:4] for path in ADRS}


@pytest.mark.parametrize("adr", ADRS, ids=lambda path: path.name[:4])
def test_every_test_an_adr_names_exists(adr):
    defined = set()
    for path in TEST_FILES:
        defined |= set(re.findall(r"^\s*(?:def|class) (\w+)", path.read_text(), re.MULTILINE))
    named = set(re.findall(r"`(test_\w+|Test\w+|\w+Machine)`", adr.read_text()))
    assert named, "an ADR names at least one test that would fail if it were undone"
    assert named <= defined, sorted(named - defined)
