from decimal import Decimal

from shared.dynamo import deserialize, serialize


def test_serialize_converts_floats_and_drops_none_map_values():
    item = {"pk": "a", "n": 1, "f": 0.08, "m": {"x": None, "y": [1.5, "z"]}}

    assert serialize(item) == {
        "pk": {"S": "a"},
        "n": {"N": "1"},
        "f": {"N": "0.08"},
        "m": {"M": {"y": {"L": [{"N": "1.5"}, {"S": "z"}]}}},
    }


def test_deserialize_returns_plain_python_values():
    item = {"pk": {"S": "a"}, "n": {"N": "1"}, "m": {"M": {"ok": {"BOOL": True}}}}

    assert deserialize(item) == {"pk": "a", "n": Decimal(1), "m": {"ok": True}}
