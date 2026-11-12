"""Text that came from the account, made safe for one line of a plain-text email.

Resource names, tag values, error messages and the approver's User-Agent are
chosen by whoever can create a resource or open a link. Printed raw they can
add lines to an email (a forged "Approved by" line), reorder what the reader
sees (bidirectional overrides) or hide text (zero-width and control characters).
"""

import unicodedata

# Cc: control characters, including ESC and NUL. Cf: format characters, including
# the bidirectional overrides and the zero-width space.
_INVISIBLE = frozenset({"Cc", "Cf"})


def one_line(text: object) -> str:
    """``text`` on a single line, without control or format characters.

    Every run of whitespace, including the Unicode line separators, becomes one
    space first, so removing a character never glues two lines into one word.
    """
    collapsed = " ".join(str(text).split())
    visible = "".join(ch for ch in collapsed if unicodedata.category(ch) not in _INVISIBLE)
    return " ".join(visible.split())
