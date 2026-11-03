"""Approval records: one human decision on one plan, stored in the audit table.

The record carries the Step Functions task token, so a decision can resume the
waiting execution. The status moves PENDING -> DECIDING -> APPROVED/REJECTED
through conditional updates, so two concurrent decisions cannot both win.
Task tokens are never logged.
"""

import hashlib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from botocore.exceptions import ClientError

from shared.dynamo import deserialize, serialize

logger = logging.getLogger("shared.approvals")

APPROVAL_PREFIX = "APPROVAL#"
META_SK = "META"
PENDING = "PENDING"
DECIDING = "DECIDING"
APPROVED = "APPROVED"
REJECTED = "REJECTED"
EXPIRED = "EXPIRED"
APPROVAL_TTL_DAYS = 30
# Shorter than the 72 h task timeout, so the record expires before the execution does.
APPROVAL_WINDOW = timedelta(hours=71, minutes=30)

_NEW_RECORD_CONDITION = "attribute_not_exists(pk) AND attribute_not_exists(sk)"
_CONDITIONAL_FAILURE = "ConditionalCheckFailedException"


class ApprovalError(Exception):
    """Base class of approval store failures."""


class ApprovalExistsError(ApprovalError):
    """An approval with this id was already created."""


class AlreadyDecidedError(ApprovalError):
    """The approval is no longer waiting for a decision."""


class ExpiredError(ApprovalError):
    """The approval window closed before a decision was made."""


def short_hash(approval_id: str) -> str:
    """Stable short fingerprint for logs: the approval id grants access, so it is never logged."""
    return hashlib.sha256(approval_id.encode()).hexdigest()[:16]


def expiry_for(created_at: datetime) -> datetime:
    """When an approval created at ``created_at`` stops accepting decisions."""
    return created_at + APPROVAL_WINDOW


@dataclass(frozen=True)
class ApprovalRecord:
    approval_id: str
    run_id: str
    plan_version_id: str
    plan_sha256: str
    plan_summary: dict
    status: str
    created_at: str
    expires_at: str
    approved_by: dict | None
    decided_at: str | None

    @classmethod
    def from_item(cls, item: dict) -> "ApprovalRecord":
        """Build a record from a deserialized (plain Python) DynamoDB item."""
        return cls(
            approval_id=item["approvalId"],
            run_id=item["runId"],
            plan_version_id=item["planVersionId"],
            plan_sha256=item["planSha256"],
            plan_summary=item["planSummary"],
            status=item["status"],
            created_at=item["createdAt"],
            expires_at=item["expiresAt"],
            approved_by=item.get("approvedBy"),
            decided_at=item.get("decidedAt"),
        )


def _is_conditional_failure(error: ClientError) -> bool:
    return error.response.get("Error", {}).get("Code") == _CONDITIONAL_FAILURE


class ApprovalStore:
    """Create approval records and move them through their status transitions."""

    def __init__(self, ddb_client, table_name: str) -> None:
        self._ddb = ddb_client
        self._table = table_name

    def create(self, record: ApprovalRecord, task_token: str) -> None:
        """Write a new PENDING record; raise ``ApprovalExistsError`` if the id is taken."""
        expires_at = datetime.fromisoformat(record.expires_at)
        expires_ttl = expires_at + timedelta(days=APPROVAL_TTL_DAYS)
        item = {
            "pk": f"{APPROVAL_PREFIX}{record.approval_id}",
            "sk": META_SK,
            "approvalId": record.approval_id,
            "runId": record.run_id,
            "planVersionId": record.plan_version_id,
            "planSha256": record.plan_sha256,
            "planSummary": record.plan_summary,
            "taskToken": task_token,
            "status": record.status,
            "createdAt": record.created_at,
            "expiresAt": record.expires_at,
            "ttl": int(expires_ttl.astimezone(UTC).timestamp()),
        }
        try:
            self._ddb.put_item(
                TableName=self._table,
                Item=serialize(item),
                ConditionExpression=_NEW_RECORD_CONDITION,
            )
        except ClientError as error:
            if not _is_conditional_failure(error):
                raise
            raise ApprovalExistsError(record.approval_id) from error

    def get(self, approval_id: str) -> tuple[ApprovalRecord, str] | None:
        """The record and its task token, or ``None`` if there is no such approval."""
        response = self._ddb.get_item(
            TableName=self._table, Key=self._key(approval_id), ConsistentRead=True
        )
        raw = response.get("Item")
        if raw is None:
            return None
        item = deserialize(raw)
        return ApprovalRecord.from_item(item), item["taskToken"]

    def refresh_token(self, approval_id: str, task_token: str) -> None:
        """Replace the task token of a PENDING approval (a retried task attempt has a new one).

        Raises ``AlreadyDecidedError`` if the approval already left PENDING.
        """
        try:
            self._ddb.update_item(
                TableName=self._table,
                Key=self._key(approval_id),
                UpdateExpression="SET taskToken = :t",
                ConditionExpression="#s = :pending",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues=serialize({":t": task_token, ":pending": PENDING}),
            )
        except ClientError as error:
            if not _is_conditional_failure(error):
                raise
            raise AlreadyDecidedError(approval_id) from error

    def claim(self, approval_id: str, now: str, approver: dict) -> str:
        """Move a PENDING, unexpired approval to DECIDING for ``approver``.

        Raises ``AlreadyDecidedError`` if it left PENDING, ``ExpiredError`` if
        it is still PENDING but its window closed.
        """
        try:
            self._ddb.update_item(
                TableName=self._table,
                Key=self._key(approval_id),
                UpdateExpression="SET #s = :deciding, approvedBy = :by",
                ConditionExpression="#s = :pending AND expiresAt > :now",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues=serialize(
                    {":deciding": DECIDING, ":pending": PENDING, ":now": now, ":by": approver}
                ),
                ReturnValuesOnConditionCheckFailure="ALL_OLD",
            )
        except ClientError as error:
            if not _is_conditional_failure(error):
                raise
            old = error.response.get("Item")
            if old is not None and deserialize(old).get("status") != PENDING:
                raise AlreadyDecidedError(approval_id) from error
            raise ExpiredError(approval_id) from error
        return DECIDING

    def revert(self, approval_id: str) -> None:
        """Put a DECIDING approval back to PENDING after a failed resume."""
        try:
            self._ddb.update_item(
                TableName=self._table,
                Key=self._key(approval_id),
                UpdateExpression="SET #s = :pending REMOVE approvedBy",
                ConditionExpression="#s = :deciding",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues=serialize({":pending": PENDING, ":deciding": DECIDING}),
            )
        except ClientError as error:
            if not _is_conditional_failure(error):
                raise
            logger.warning(
                "approval was not DECIDING, nothing to revert",
                extra={"approvalIdHash": short_hash(approval_id)},
            )

    def finalize(self, approval_id: str, status: str, decided_at: str) -> None:
        """Record the final ``status`` of a DECIDING approval."""
        try:
            self._ddb.update_item(
                TableName=self._table,
                Key=self._key(approval_id),
                UpdateExpression="SET #s = :status, decidedAt = :at",
                ConditionExpression="#s = :deciding",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues=serialize(
                    {":status": status, ":at": decided_at, ":deciding": DECIDING}
                ),
            )
        except ClientError as error:
            if not _is_conditional_failure(error):
                raise
            raise AlreadyDecidedError(approval_id) from error

    def expire(self, approval_id: str, decided_at: str) -> None:
        """Mark a PENDING or DECIDING approval EXPIRED; an already decided one is left alone."""
        try:
            self._ddb.update_item(
                TableName=self._table,
                Key=self._key(approval_id),
                UpdateExpression="SET #s = :expired, decidedAt = :at",
                ConditionExpression="#s IN (:pending, :deciding)",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues=serialize(
                    {
                        ":expired": EXPIRED,
                        ":at": decided_at,
                        ":pending": PENDING,
                        ":deciding": DECIDING,
                    }
                ),
            )
        except ClientError as error:
            if not _is_conditional_failure(error):
                raise
            logger.info(
                "approval already decided, not expiring",
                extra={"approvalIdHash": short_hash(approval_id)},
            )

    @staticmethod
    def _key(approval_id: str) -> dict:
        return serialize({"pk": f"{APPROVAL_PREFIX}{approval_id}", "sk": META_SK})
