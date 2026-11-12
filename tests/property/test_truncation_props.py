"""Property tests for ``truncate_report`` (shared/report.py) and ``one_line`` (shared/text.py)."""

import unicodedata

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

from shared.report import TRUNCATION_SUFFIX, truncate_report
from shared.text import one_line

# Lines never contain "\n" (that is the separator); multi-byte characters are common.
line_chars = st.characters(blacklist_characters="\n", blacklist_categories=("Cs",))
lines = st.lists(st.text(alphabet=line_chars, max_size=12), min_size=1, max_size=15)
pointers = st.text(alphabet=line_chars, max_size=20)


def kept_lines(result: str, text_lines: list[str], pointer: str) -> int | None:
    """How many whole leading lines ``result`` keeps, or None if it is not that shape."""
    if result == "\n".join(text_lines):
        return len(text_lines)
    for kept in range(len(text_lines) - 1, -1, -1):
        notice = TRUNCATION_SUFFIX.format(omitted=len(text_lines) - kept, pointer=pointer)
        if result == "\n".join(text_lines[:kept]) + notice:
            return kept
    return None


def fits(text_lines: list[str], kept: int, pointer: str, limit: int) -> bool:
    notice = TRUNCATION_SUFFIX.format(omitted=len(text_lines) - kept, pointer=pointer)
    return len(("\n".join(text_lines[:kept]) + notice).encode()) <= limit


@given(lines, pointers, st.integers(0, 400))
@example([""] * 10, "p", 0)
# A hundred empty lines: keeping one leaves a two-digit count, so the notice with one kept
# line fits in 48 bytes while the notice alone (three digits) does not.
@example([""] * 100, "p", 48)
def test_truncated_report_fits_and_keeps_whole_leading_lines(text_lines, pointer, limit):
    """Fails if truncate_report counts characters instead of UTF-8 bytes, cuts a line in the
    middle, keeps fewer lines than fit, or raises when some prefix and notice would fit."""
    text = "\n".join(text_lines)
    if len(text.encode()) <= limit:
        assert truncate_report(text, limit, pointer) == text
        return
    any_fit = any(fits(text_lines, k, pointer, limit) for k in range(len(text_lines)))
    if not any_fit:
        with pytest.raises(ValueError, match="cannot hold the truncation notice"):
            truncate_report(text, limit, pointer)
        # Documented case: not even the notice alone fits.
        assert not fits(text_lines, 0, pointer, limit)
        return
    result = truncate_report(text, limit, pointer)
    encoded = result.encode("utf-8")
    assert len(encoded) <= limit
    assert encoded.decode("utf-8") == result
    kept = kept_lines(result, text_lines, pointer)
    assert kept is not None and kept < len(text_lines)
    # The longest whole-line prefix: one more line would not fit.
    assert not any(fits(text_lines, k, pointer, limit) for k in range(kept + 1, len(text_lines)))


@given(lines, pointers, st.integers(0, 400), st.integers(0, 400))
def test_larger_limit_never_keeps_fewer_lines(text_lines, pointer, first, second):
    """Fails if a larger byte limit could make truncate_report keep a shorter prefix
    (e.g. a search that stops at the first fitting prefix from the top)."""
    small, large = sorted((first, second))
    text = "\n".join(text_lines)
    try:
        small_result = truncate_report(text, small, pointer)
    except ValueError:
        return
    large_result = truncate_report(text, large, pointer)
    small_kept = kept_lines(small_result, text_lines, pointer)
    large_kept = kept_lines(large_result, text_lines, pointer)
    assert small_kept is not None and large_kept is not None
    assert small_kept <= large_kept


LINE_BREAKS = "\n\r\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029"
INVISIBLE = "\u200b\u200e\u202e\u2066\ufeff\x00\x1b\u00ad"


@given(
    st.text()
    | st.text(alphabet=st.sampled_from(f"ab {LINE_BREAKS}{INVISIBLE}\t\u00a0\u3000"))
    | st.integers()
)
@example("a\u2028b")
@example("a\u200b\nb")
@example(" \u202e x \u200b ")
@example("a\x85b\x1c")
def test_one_line_is_one_clean_line_and_idempotent(text):
    """Fails if one_line keeps any line break (Unicode separators included), a Cc or Cf
    character, an edge or doubled space, or changes its own output."""
    result = one_line(text)
    assert len(result.splitlines()) <= 1
    assert not any(unicodedata.category(ch) in {"Cc", "Cf"} for ch in result)
    assert result == result.strip(" ")
    assert "  " not in result
    assert one_line(result) == result
