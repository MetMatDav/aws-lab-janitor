"""Just enough Markdown parsing to hold the documents to the code."""

import re
from pathlib import Path

_FENCE = re.compile(r"^(```|~~~)")
_INLINE_CODE = re.compile(r"`[^`\n]*`")
_LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
_NOT_IN_A_SLUG = re.compile(r"[^\w\- ]")
_EDGE = re.compile(r"^\s*(\[\*\]|\w+)\s*-->\s*(\[\*\]|\w+)\s*(?::.*)?$")


def _prose_lines(text: str) -> list[str]:
    """The lines of ``text`` outside fenced code blocks."""
    lines, fenced = [], False
    for line in text.splitlines():
        if _FENCE.match(line.strip()):
            fenced = not fenced
        elif not fenced:
            lines.append(line)
    return lines


def links(path: Path) -> list[tuple[str, str | None]]:
    """Each relative link of the document as ``(target, anchor)``; ``""`` is the document itself."""
    found = []
    for line in _prose_lines(path.read_text()):
        for destination in _LINK.findall(_INLINE_CODE.sub("", line)):
            if _SCHEME.match(destination):
                continue
            target, _, anchor = destination.partition("#")
            found.append((target, anchor or None))
    return found


def _slug(heading: str) -> str:
    """The anchor GitHub gives a heading."""
    return _NOT_IN_A_SLUG.sub("", heading.lower()).replace(" ", "-")


def anchors(path: Path) -> set[str]:
    """Every heading anchor of the document; a repeated heading gets ``-1``, ``-2``..."""
    seen: dict[str, int] = {}
    found = set()
    for line in _prose_lines(path.read_text()):
        match = _HEADING.match(line)
        if not match:
            continue
        slug = _slug(match.group(2))
        count = seen.get(slug, 0)
        seen[slug] = count + 1
        found.add(slug if count == 0 else f"{slug}-{count}")
    return found


def tables(text: str, heading: str) -> list[list[str]]:
    """The body rows of the first table under ``heading``, each as its cells."""
    rows: list[list[str]] = []
    inside = False
    for line in _prose_lines(text):
        match = _HEADING.match(line)
        if match:
            if inside:
                break
            inside = match.group(2) == heading
        elif inside and line.lstrip().startswith("|"):
            rows.append([cell.strip() for cell in line.strip().strip("|").split("|")])
        elif inside and rows:
            break
    return rows[2:]


def mermaid_blocks(text: str) -> list[str]:
    """The source of every ```mermaid block."""
    return [block.strip("\n") for block in re.findall(r"```mermaid\n(.*?)```", text, re.DOTALL)]


def state_edges(diagram: str) -> set[tuple[str, str]]:
    """Every ``A --> B`` of a Mermaid state diagram; the start and end marker is ``*``."""
    edges = set()
    for line in diagram.splitlines():
        match = _EDGE.match(line)
        if match:
            edges.add(tuple("*" if end == "[*]" else end for end in match.groups()))
    return edges
