"""Every relative link in the README and under docs/ leads somewhere."""

from pathlib import Path

import pytest

from tests.docs import markdown

DOCUMENTS = [Path("README.md"), *sorted(Path("docs").rglob("*.md"))]


@pytest.mark.parametrize("document", DOCUMENTS, ids=str)
def test_relative_links_and_anchors_resolve(document):
    for target, anchor in markdown.links(document):
        destination = (document.parent / target).resolve() if target else document.resolve()
        assert destination.exists(), f"{document}: {target}"
        if anchor and destination.suffix == ".md":
            assert anchor in markdown.anchors(destination), f"{document}: {target}#{anchor}"


def test_links_skip_urls_and_code(tmp_path):
    page = tmp_path / "page.md"
    page.write_text(
        "# A Title, with `code` & more\n\n"
        "[site](https://example.com/x) [mail](mailto:a@example.com) "
        "[here](#a-title-with-code--more)\n"
        "[file](other.md#part) and `[not a link](nowhere.md)`\n\n"
        "```\n[inside a fence](nowhere.md)\n```\n"
    )

    assert markdown.links(page) == [("", "a-title-with-code--more"), ("other.md", "part")]
    assert markdown.anchors(page) == {"a-title-with-code--more"}


def test_anchors_number_repeated_headings(tmp_path):
    page = tmp_path / "page.md"
    page.write_text("## Using\n\ntext\n\n## Using\n\n### Rehearsal record: 2026-11-11\n")

    assert markdown.anchors(page) == {"using", "using-1", "rehearsal-record-2026-11-11"}


def test_tables_and_mermaid_blocks():
    text = (
        "## What it detects\n\nintro\n\n| Kind | Marked by |\n| --- | --- |\n"
        "| `ebs-volume` | tag |\n| `ami` | tag |\n\n## Next\n\n| x |\n| - |\n| y |\n\n"
        "```mermaid\nstateDiagram-v2\n  Detect --> Apply\n```\n"
    )

    assert markdown.tables(text, "What it detects") == [["`ebs-volume`", "tag"], ["`ami`", "tag"]]
    assert markdown.tables(text, "Missing") == []
    assert markdown.mermaid_blocks(text) == ["stateDiagram-v2\n  Detect --> Apply"]
