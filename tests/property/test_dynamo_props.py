"""Property tests for ``shared/dynamo.py`` ``serialize`` / ``deserialize``.

Contract (read from the module): ``serialize`` takes a plain item made of str,
bool, int, float, None, lists and nested dicts. Floats are stored as
``Decimal(str(f))``; ``None`` values inside a map (the item itself included)
are dropped, while ``None`` inside a list is kept as NULL. ``deserialize``
returns every number as ``Decimal``. DynamoDB numbers hold up to 38 significant
digits with magnitudes between 1E-130 and about 1E+126, so the supported float
domain is 0 and finite floats well inside that range.
"""

import math
from decimal import Decimal

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

from shared.dynamo import deserialize, serialize

INT_LIMIT = 10**38 - 1
finite_floats = (
    st.just(0.0)
    | st.just(-0.0)
    | (
        st.floats(min_value=-1e120, max_value=1e120, allow_nan=False, allow_infinity=False).filter(
            lambda f: f == 0 or abs(f) >= 1e-120
        )
    )
)
scalars = (
    st.none()
    | st.booleans()
    | st.integers(-INT_LIMIT, INT_LIMIT)
    | finite_floats
    | st.text(max_size=12)
)
values = st.recursive(
    scalars,
    lambda children: (
        st.lists(children, max_size=4) | st.dictionaries(st.text(max_size=6), children, max_size=4)
    ),
    max_leaves=20,
)
items = st.dictionaries(st.text(min_size=1, max_size=8), values, max_size=6)


def expected(value):
    """What a value should look like after serialize then deserialize."""
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return value
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, list):
        return [expected(v) for v in value]
    return {k: expected(v) for k, v in value.items() if v is not None}


def same(left, right) -> bool:
    """Equality that also checks types, so ``True`` never equals ``Decimal(1)``."""
    if type(left) is not type(right):
        return False
    if isinstance(left, list):
        return len(left) == len(right) and all(map(same, left, right))
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(same(left[k], right[k]) for k in left)
    if isinstance(left, Decimal):
        return str(left) == str(right)
    return left == right


@given(items)
@example({"a": None, "b": [None, {"c": None}], "d": -0.0, "e": 0.1})
def test_round_trip_on_the_supported_domain(item):
    """Fails if serialize stops converting floats via str (e.g. Decimal(f) keeps the binary
    expansion), stops dropping None map values, or loses a type on the way back."""
    assert same(deserialize(serialize(item)), expected(item))


non_finite = st.sampled_from([math.nan, math.inf, -math.inf])


def nest(value, depth):
    """``value`` wrapped ``depth`` times alternately in a list and a map."""
    for level in range(depth):
        value = [value] if level % 2 else {"k": value}
    return value


@given(st.sampled_from([math.nan, math.inf]), st.integers(0, 3))
def test_nan_and_positive_infinity_raise(value, depth):
    """Fails if serialize starts accepting NaN or +infinity anywhere in an item."""
    with pytest.raises(TypeError):
        serialize({"v": nest(value, depth)})


@given(non_finite, st.integers(0, 3))
@example(-math.inf, 0)
def test_non_finite_floats_raise(value, depth):
    """Fails if ``_clean`` stops refusing non-finite floats: boto3's TypeSerializer only
    rejects the strings 'Infinity' and 'NaN', so negative infinity would be stored."""
    with pytest.raises((TypeError, ArithmeticError)):
        serialize({"v": nest(value, depth)})
