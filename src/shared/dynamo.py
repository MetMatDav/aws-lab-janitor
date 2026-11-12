"""DynamoDB item (de)serialization for the low-level client."""

import math
from decimal import Decimal

from boto3.dynamodb.types import TypeDeserializer, TypeSerializer

_serializer = TypeSerializer()
_deserializer = TypeDeserializer()


def _clean(value):
    """Make ``value`` serializable: floats become ``Decimal``, ``None`` map values are dropped.

    A float that is not finite is refused here: boto3 rejects ``Infinity`` and
    ``NaN`` but lets ``-Infinity`` through as a number.
    """
    if isinstance(value, float):
        if not math.isfinite(value):
            raise TypeError("Infinity and NaN not supported")
        return Decimal(str(value))
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items() if v is not None}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    return value


def serialize(item: dict) -> dict:
    """Plain Python item to DynamoDB attribute values."""
    return {k: _serializer.serialize(v) for k, v in _clean(item).items()}


def deserialize(item: dict) -> dict:
    """DynamoDB attribute values to a plain Python item (numbers come back as ``Decimal``)."""
    return {k: _deserializer.deserialize(v) for k, v in item.items()}
