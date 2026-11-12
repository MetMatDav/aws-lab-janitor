import pytest

from shared.text import one_line


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("plain text", "plain text"),
        ("  two\n\nlines\r\nand\ttabs  ", "two lines and tabs"),
        (
            "line\u2028separator\u2029paragraph\x85nel\x0bvt\x0cff",
            "line separator paragraph nel vt ff",
        ),
        ("esc\x1b[31mred", "esc[31mred"),
        ("nul\x00byte", "nulbyte"),
        ("rtl\u202egnp.exe", "rtlgnp.exe"),
        ("zero\u200bwidth", "zerowidth"),
        ("a \x00 b", "a b"),
        ("café ñ 中文", "café ñ 中文"),
        ("", ""),
    ],
    ids=ascii,
)
def test_one_line_collapses_whitespace_and_drops_invisible_characters(text, expected):
    assert one_line(text) == expected


def test_one_line_accepts_non_strings():
    assert one_line(42) == "42"
