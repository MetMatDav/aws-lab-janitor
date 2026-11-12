"""Three small properties: the confirmation code, ISO timestamp ordering, integer context values."""

import re
from datetime import UTC, datetime

from hypothesis import example, given
from hypothesis import strategies as st

from approval import handler
from lab_janitor import config

HEX = "0123456789abcdef"
run_ids = st.builds(
    lambda when, suffix: f"{when.strftime('%Y%m%dT%H%M%SZ')}-{suffix}",
    st.datetimes(),
    st.text(alphabet=HEX, min_size=4, max_size=4),
)


@st.composite
def confirmations(draw, run_id):
    """Arbitrary text, or the run id's code with near-miss changes around it."""
    code = run_id[-4:]
    near = st.builds(
        lambda pad_left, body, pad_right: pad_left + body + pad_right,
        st.text(alphabet=" \t\r\n\u00a0\u3000\u200b", max_size=2),
        st.sampled_from([code, code.upper(), code[:3], code + "0"]),
        st.text(alphabet=" \t\r\n\u00a0\u2028\ufeff", max_size=2),
    )
    return draw(st.text(max_size=8) | near)


@given(run_ids.flatmap(lambda run_id: st.tuples(st.just(run_id), confirmations(run_id))))
@example(("20261102T120000Z-12ff", "12\ufb00"))
def test_confirmation_matches_only_the_ascii_code(case):
    """Fails if the comparison is loosened to casefold(), a bare strip() or NFKC, which
    accept look-alikes such as the "ff" ligature or a no-break space."""
    run_id, confirm = case
    typed = confirm.strip(" \t\r\n")
    matches = handler._confirmation_matches(confirm, run_id)
    if matches:
        assert typed.lower() == run_id[-4:]
    # And the converse on the ASCII side: the exact code, in any case, is accepted.
    if typed.isascii() and typed.lower() == run_id[-4:]:
        assert matches


@given(
    st.datetimes(timezones=st.just(UTC)),
    st.datetimes(timezones=st.just(UTC)),
    st.booleans(),
    st.booleans(),
)
def test_iso_timestamps_order_like_their_instants(left, right, drop_left, drop_right):
    """Fails if the timestamps the store writes ever compare as strings differently from
    the instants they denote (e.g. a format that varies its width), which would break
    the ``expiresAt > :now`` condition."""
    if drop_left:
        left = left.replace(microsecond=0)
    if drop_right:
        right = right.replace(microsecond=0)
    a, b = left.isoformat(), right.isoformat()
    # The format the code produces: datetime.now(UTC).isoformat().
    assert a == datetime.fromisoformat(a).astimezone(UTC).isoformat()
    assert (a < b) == (left < right)
    assert (a == b) == (left == right)
    # DynamoDB compares strings by UTF-8 bytes.
    assert (a.encode() < b.encode()) == (left < right)


ASCII_INTEGER = re.compile(r"-?[0-9]+")
# The module also caps the length at nine digits (int() refuses huge strings with
# another message), so the converse is only claimed up to that length.
MAX_DIGITS = 9
LOW, HIGH = 0, 3650


@given(
    st.text(max_size=12)
    | st.from_regex(r"\A-?[0-9]{1,6}\Z")
    | st.integers(-10, 4000).map(str)
    | st.text(alphabet="0123456789-+_ \n\u0661\uff11", max_size=6)
)
@example("\u0661")
@example("7\n")
@example("1_0")
@example(" 7")
@example("+7")
@example("-0")
def test_int_in_range_accepts_only_ascii_digits_in_range(value):
    """Fails if _int_in_range trusts int() alone (which accepts Arabic-Indic digits, '1_0',
    spaces and '+'), or stops checking the range."""
    valid = ASCII_INTEGER.fullmatch(value) is not None and LOW <= int(value) <= HIGH
    try:
        result = config._int_in_range("minAgeDays", value, LOW, HIGH)
    except ValueError as error:
        assert "minAgeDays must be an integer" in str(error)
        assert not valid or len(value.lstrip("-")) > MAX_DIGITS
    else:
        assert valid
        assert type(result) is int
        assert result == int(value)
