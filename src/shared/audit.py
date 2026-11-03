"""Append-only audit trail in DynamoDB.

Every record is written with a condition that it does not exist yet, so a
retried invocation can never overwrite history; a duplicate write is logged
and ignored.
"""

import logging
from datetime import UTC, datetime
from decimal import Decimal

from boto3.dynamodb.types import TypeSerializer
from botocore.exceptions import ClientError

logger = logging.getLogger("shared.audit")

NEW_RECORD_CONDITION = "attribute_not_exists(pk) AND attribute_not_exists(sk)"
RESOURCE_INDEX = "by-resource"
SUMMARY_SK = "RUN#SUMMARY"
SUMMARY_ACTOR = "lambda:detector"

_serializer = TypeSerializer()


def _clean(value):
    """Make ``value`` serializable: floats become ``Decimal``, ``None`` map values are dropped."""
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items() if v is not None}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    return value


def _serialize(item: dict) -> dict:
    return {k: _serializer.serialize(v) for k, v in _clean(item).items()}


def _iso(at: datetime) -> str:
    return at.astimezone(UTC).isoformat()


class AuditClient:
    """Write run events and summaries; answer whether a resource was proposed live."""

    def __init__(self, ddb_client, table_name: str) -> None:
        self._ddb = ddb_client
        self._table = table_name

    # Every argument is a column of the audit record.
    def put_run_event(  # noqa: PLR0913, PLR0917
        self,
        run_id: str,
        region: str,
        resource_id: str,
        event: str,
        actor: str,
        dry_run: bool,
        detail: dict,
        at: datetime,
    ) -> None:
        """Record ``event`` for one resource in run ``run_id``."""
        self._put_new(
            {
                "pk": f"RUN#{run_id}",
                "sk": f"RESOURCE#{region}#{resource_id}#{event}",
                "event": event,
                "at": _iso(at),
                "actor": actor,
                "dryRun": dry_run,
                "resourceId": resource_id,
                "region": region,
                "detail": detail,
            }
        )

    def put_run_summary(self, run_id: str, summary: dict, at: datetime) -> None:
        """Record the summary of run ``run_id``."""
        self._put_new(
            {
                "pk": f"RUN#{run_id}",
                "sk": SUMMARY_SK,
                "event": "SUMMARY",
                "at": _iso(at),
                "actor": SUMMARY_ACTOR,
                "detail": summary,
            }
        )

    def was_proposed_live(self, resource_id: str) -> bool:
        """True if any earlier non-dry-run plan proposed ``resource_id`` for deletion."""
        params = {
            "TableName": self._table,
            "IndexName": RESOURCE_INDEX,
            "KeyConditionExpression": "resourceId = :id",
            "FilterExpression": "#e = :e AND dryRun = :f",
            "ExpressionAttributeNames": {"#e": "event"},
            "ExpressionAttributeValues": {
                ":id": {"S": resource_id},
                ":e": {"S": "PROPOSED"},
                ":f": {"BOOL": False},
            },
        }
        while True:
            page = self._ddb.query(**params)
            if page.get("Count", 0) > 0 or page.get("Items"):
                return True
            last_key = page.get("LastEvaluatedKey")
            if not last_key:
                return False
            params["ExclusiveStartKey"] = last_key

    def _put_new(self, item: dict) -> None:
        try:
            self._ddb.put_item(
                TableName=self._table,
                Item=_serialize(item),
                ConditionExpression=NEW_RECORD_CONDITION,
            )
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                raise
            logger.warning(
                "audit record already exists, skipping",
                extra={"pk": item["pk"], "sk": item["sk"]},
            )
