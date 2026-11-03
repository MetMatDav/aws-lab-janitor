"""Append-only audit trail in DynamoDB.

Every record is written with a condition that it does not exist yet, so a
retried invocation can never overwrite history; a duplicate write is logged
and ignored.
"""

import logging
from datetime import UTC, datetime

from botocore.exceptions import ClientError

from shared.dynamo import serialize

logger = logging.getLogger("shared.audit")

NEW_RECORD_CONDITION = "attribute_not_exists(pk) AND attribute_not_exists(sk)"
RESOURCE_INDEX = "by-resource"
SUMMARY_SK = "RUN#SUMMARY"
SUMMARY_ACTOR = "lambda:detector"


def _iso(at: datetime) -> str:
    return at.astimezone(UTC).isoformat()


class AuditClient:
    """Write run events and summaries; answer whether a resource was proposed or eligible live."""

    def __init__(self, ddb_client, table_name: str) -> None:
        self._ddb = ddb_client
        self._table = table_name

    # Every argument is a column of the audit record.
    def put_run_event(  # noqa: PLR0913, PLR0917
        self,
        run_id: str,
        region: str,
        resource_id: str,
        kind: str,
        event: str,
        actor: str,
        dry_run: bool,
        detail: dict,
        at: datetime,
    ) -> None:
        """Record ``event`` for one resource in run ``run_id``.

        The sort key includes ``kind`` because resource ids are only unique per
        kind: a global S3 bucket and a CloudWatch dashboard may share a name.
        """
        self._put_new(
            {
                "pk": f"RUN#{run_id}",
                "sk": f"RESOURCE#{region}#{kind}#{resource_id}#{event}",
                "event": event,
                "at": _iso(at),
                "actor": actor,
                "dryRun": dry_run,
                "resourceId": resource_id,
                "kind": kind,
                "region": region,
                "detail": detail,
            }
        )

    def put_run_summary(
        self, run_id: str, summary: dict, at: datetime, actor: str = SUMMARY_ACTOR
    ) -> None:
        """Record the summary of run ``run_id``."""
        self._put_new(
            {
                "pk": f"RUN#{run_id}",
                "sk": SUMMARY_SK,
                "event": "SUMMARY",
                "at": _iso(at),
                "actor": actor,
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
        return self._any_match(params)

    def was_eligible_live_before(self, resource_id: str, before: datetime) -> bool:
        """True if a non-dry-run plan marked ``resource_id`` ELIGIBLE earlier than ``before``."""
        params = {
            "TableName": self._table,
            "IndexName": RESOURCE_INDEX,
            "KeyConditionExpression": "resourceId = :id AND #at < :before",
            "FilterExpression": "#e = :e AND dryRun = :f",
            "ExpressionAttributeNames": {"#at": "at", "#e": "event"},
            "ExpressionAttributeValues": {
                ":id": {"S": resource_id},
                ":before": {"S": _iso(before)},
                ":e": {"S": "ELIGIBLE"},
                ":f": {"BOOL": False},
            },
        }
        return self._any_match(params)

    def _any_match(self, params: dict) -> bool:
        """Run the query ``params`` page by page; True as soon as a page has an item."""
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
                Item=serialize(item),
                ConditionExpression=NEW_RECORD_CONDITION,
            )
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                raise
            logger.warning(
                "audit record already exists, skipping",
                extra={"pk": item["pk"], "sk": item["sk"]},
            )
