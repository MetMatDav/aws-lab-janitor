"""Stateful property test of ``shared/approvals.py`` ``ApprovalStore``.

The store runs against ``ConditionalDdb``, an in-memory DynamoDB client holding
one item. It evaluates the exact condition and update expressions the store
sends (anything else fails the test with ``AssertionError``) and raises a
``ConditionalCheckFailedException`` ``ClientError`` the way the service does.
Hypothesis then interleaves create, claim, revert, finalize, expire,
mark_reminded and refresh_token with clocks before, at and after ``expiresAt``.
"""

import copy
from datetime import UTC, datetime, timedelta

import pytest
from botocore.exceptions import ClientError
from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, initialize, invariant, rule

from shared.approvals import (
    APPROVED,
    DECIDING,
    EXPIRED,
    PENDING,
    REJECTED,
    AlreadyDecidedError,
    ApprovalExistsError,
    ApprovalRecord,
    ApprovalStore,
    ExpiredError,
    expiry_for,
)

TABLE = "test-audit"
APPROVAL_ID = "11111111-aaaa-bbbb-cccc-222222222222"
CREATED = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
EXPIRES = expiry_for(CREATED)
TERMINAL = frozenset({APPROVED, REJECTED, EXPIRED})
STATUS_NAMES = {"#s": "status"}
CONDITIONAL_FAILED = "ConditionalCheckFailedException"


# --- The fake client ------------------------------------------------------------------


def _s(value: dict) -> str:
    """The string inside a wire-format ``{"S": ...}`` value."""
    assert set(value) == {"S"}, value
    return value["S"]


def _status_is(item, values, name):
    assert name in values, f"{name} is not in ExpressionAttributeValues"
    return item is not None and "status" in item and item["status"] == values[name]


def _later(item, attribute, values, name):
    assert name in values, f"{name} is not in ExpressionAttributeValues"
    # DynamoDB compares strings by their UTF-8 bytes.
    return (
        item is not None
        and attribute in item
        and _s(item[attribute]).encode() > _s(values[name]).encode()
    )


# Every ConditionExpression the store may send, evaluated on (item, values).
CONDITIONS = {
    "#s = :pending": lambda item, v: _status_is(item, v, ":pending"),
    "#s = :deciding": lambda item, v: _status_is(item, v, ":deciding"),
    "#s = :pending AND expiresAt > :now": lambda item, v: (
        _status_is(item, v, ":pending") and _later(item, "expiresAt", v, ":now")
    ),
    "#s = :pending AND expiresAt > :now AND attribute_not_exists(remindedAt)": lambda item, v: (
        _status_is(item, v, ":pending")
        and _later(item, "expiresAt", v, ":now")
        and "remindedAt" not in item
    ),
    "#s IN (:pending, :deciding)": lambda item, v: (
        _status_is(item, v, ":pending") or _status_is(item, v, ":deciding")
    ),
}


def _set(*pairs):
    def apply(item, values):
        for attribute, name in pairs:
            item[attribute] = values[name]

    return apply


def _set_pending_remove_approver(item, values):
    item["status"] = values[":pending"]
    item.pop("approvedBy", None)


# Every UpdateExpression the store may send, applied in place on the item.
UPDATES = {
    "SET taskToken = :t": _set(("taskToken", ":t")),
    "SET remindedAt = :now": _set(("remindedAt", ":now")),
    "SET #s = :deciding, approvedBy = :by": _set(("status", ":deciding"), ("approvedBy", ":by")),
    "SET #s = :pending REMOVE approvedBy": _set_pending_remove_approver,
    "SET #s = :status, decidedAt = :at": _set(("status", ":status"), ("decidedAt", ":at")),
    "SET #s = :expired, decidedAt = :at": _set(("status", ":expired"), ("decidedAt", ":at")),
}


class ConditionalDdb:
    """A low-level DynamoDB client with one table holding at most one item."""

    def __init__(self) -> None:
        self.item: dict | None = None

    def _key_of(self, item: dict) -> dict:
        return {"pk": item["pk"], "sk": item["sk"]}

    def _check_key(self, key: dict) -> None:
        assert key == {"pk": {"S": f"APPROVAL#{APPROVAL_ID}"}, "sk": {"S": "META"}}, key
        if self.item is not None:
            assert key == self._key_of(self.item)

    @staticmethod
    def _failed(operation: str, old: dict | None) -> ClientError:
        response = {
            "Error": {"Code": CONDITIONAL_FAILED, "Message": "The conditional request failed"},
            "ResponseMetadata": {"HTTPStatusCode": 400},
        }
        if old is not None:
            response["Item"] = copy.deepcopy(old)
        return ClientError(response, operation)

    def put_item(self, *, TableName, Item, ConditionExpression):
        assert TableName == TABLE
        assert ConditionExpression == "attribute_not_exists(pk) AND attribute_not_exists(sk)"
        self._check_key(self._key_of(Item))
        if self.item is not None:
            raise self._failed("PutItem", None)
        self.item = copy.deepcopy(Item)
        return {}

    def get_item(self, *, TableName, Key, ConsistentRead):
        assert TableName == TABLE
        assert ConsistentRead is True
        self._check_key(Key)
        return {} if self.item is None else {"Item": copy.deepcopy(self.item)}

    def update_item(  # noqa: PLR0913 - the DynamoDB API's keyword arguments
        self,
        *,
        TableName,
        Key,
        UpdateExpression,
        ConditionExpression,
        ExpressionAttributeNames,
        ExpressionAttributeValues,
        ReturnValues="NONE",
        ReturnValuesOnConditionCheckFailure="NONE",
    ):
        assert TableName == TABLE
        self._check_key(Key)
        assert ExpressionAttributeNames == STATUS_NAMES, ExpressionAttributeNames
        if ConditionExpression not in CONDITIONS:
            raise AssertionError(f"unknown ConditionExpression {ConditionExpression!r}")
        if UpdateExpression not in UPDATES:
            raise AssertionError(f"unknown UpdateExpression {UpdateExpression!r}")
        assert ReturnValues in {"NONE", "ALL_NEW"}
        assert ReturnValuesOnConditionCheckFailure in {"NONE", "ALL_OLD"}
        if not CONDITIONS[ConditionExpression](self.item, ExpressionAttributeValues):
            old = self.item if ReturnValuesOnConditionCheckFailure == "ALL_OLD" else None
            raise self._failed("UpdateItem", old)
        assert self.item is not None
        UPDATES[UpdateExpression](self.item, ExpressionAttributeValues)
        return {"Attributes": copy.deepcopy(self.item)} if ReturnValues == "ALL_NEW" else {}


def test_fake_refuses_an_unknown_condition_expression():
    """Guards the fake itself: fails if an unknown expression could be treated as true.
    The store is involved because its create() is what seeds the item."""
    ddb = ConditionalDdb()
    ApprovalStore(ddb, TABLE).create(record(), "token-0")
    with pytest.raises(AssertionError, match="unknown ConditionExpression"):
        ddb.update_item(
            TableName=TABLE,
            Key={"pk": {"S": f"APPROVAL#{APPROVAL_ID}"}, "sk": {"S": "META"}},
            UpdateExpression="SET taskToken = :t",
            ConditionExpression="#s <> :pending",
            ExpressionAttributeNames=STATUS_NAMES,
            ExpressionAttributeValues={":t": {"S": "x"}, ":pending": {"S": PENDING}},
        )


# --- The state machine ----------------------------------------------------------------


def record() -> ApprovalRecord:
    return ApprovalRecord(
        approval_id=APPROVAL_ID,
        run_id="20261102T120000Z-abc1",
        plan_version_id="plan-v1",
        plan_sha256="ab" * 32,
        plan_summary={"candidateCount": 1, "estimatedMonthlyUsd": 0.8},
        status=PENDING,
        created_at=CREATED.isoformat(),
        expires_at=EXPIRES.isoformat(),
        approved_by=None,
        decided_at=None,
    )


# Clocks from creation to a day after expiry, dense around expiresAt, with and
# without microseconds (datetime.now(UTC).isoformat() writes both forms).
TICKS = [
    timedelta(0),
    timedelta(microseconds=1),
    timedelta(microseconds=-1),
    timedelta(seconds=1),
    timedelta(seconds=-1),
]
clocks = (
    st.sampled_from([EXPIRES + tick for tick in TICKS])
    | st.integers(0, int((EXPIRES - CREATED).total_seconds()) + 86400).map(
        lambda seconds: CREATED + timedelta(seconds=seconds)
    )
    | st.datetimes(
        min_value=CREATED.replace(tzinfo=None),
        max_value=(EXPIRES + timedelta(days=1)).replace(tzinfo=None),
        timezones=st.just(UTC),
    )
)
tokens = st.text(alphabet="abcdef0123456789", min_size=1, max_size=8)


class ApprovalStoreMachine(RuleBasedStateMachine):
    """Drive the real ApprovalStore and check the record after every step."""

    def __init__(self) -> None:
        super().__init__()
        self.ddb = ConditionalDdb()
        self.store = ApprovalStore(self.ddb, TABLE)
        self.token = ""
        self.final_status: str | None = None
        self.claims_since_pending = 0
        self.reminded_at: str | None = None

    def stored(self) -> dict:
        item = self.ddb.item
        assert item is not None
        return item

    def status(self) -> str:
        return _s(self.stored()["status"])

    @initialize(token=tokens)
    def create(self, token):
        self.store.create(record(), token)
        self.token = token

    @rule(token=tokens)
    def create_again(self, token):
        before = copy.deepcopy(self.stored())
        with pytest.raises(ApprovalExistsError):
            self.store.create(record(), token)
        assert self.stored() == before

    @rule(now=clocks)
    def claim(self, now):
        before = self.status()
        approver = {"mode": "link", "at": now.isoformat()}
        try:
            result = self.store.claim(APPROVAL_ID, now.isoformat(), approver)
        except AlreadyDecidedError as error:
            assert before != PENDING
            assert error.status == before
        except ExpiredError:
            assert before == PENDING
            assert now >= EXPIRES
        else:
            assert result == DECIDING
            assert before == PENDING
            assert now < EXPIRES, "a claim at or after expiresAt succeeded"
            self.claims_since_pending += 1
            assert self.status() == DECIDING

    @rule()
    def revert(self):
        before = self.status()
        self.store.revert(APPROVAL_ID)
        if before == DECIDING:
            assert self.status() == PENDING
            self.claims_since_pending = 0
        else:
            assert self.status() == before

    @rule(status=st.sampled_from([APPROVED, REJECTED]), at=clocks)
    def finalize(self, status, at):
        before = self.status()
        try:
            self.store.finalize(APPROVAL_ID, status, at.isoformat())
        except AlreadyDecidedError:
            assert before != DECIDING
        else:
            assert before == DECIDING
            assert self.status() == status

    @rule(at=clocks)
    def expire(self, at):
        before = self.status()
        self.store.expire(APPROVAL_ID, at.isoformat())
        if before in {PENDING, DECIDING}:
            assert self.status() == EXPIRED
        else:
            assert self.status() == before

    @rule(now=clocks)
    def mark_reminded(self, now):
        before_status = self.status()
        before_reminded = self.stored().get("remindedAt")
        result = self.store.mark_reminded(APPROVAL_ID, now.isoformat())
        if result is not None:
            assert before_status == PENDING
            assert now < EXPIRES
            assert before_reminded is None
            assert result.status == PENDING
            self.reminded_at = now.isoformat()
        elif before_status == PENDING and now < EXPIRES:
            # Refused while PENDING and unexpired: only because a reminder already went out.
            assert before_reminded is not None

    @rule(token=tokens)
    def refresh_token(self, token):
        before = self.status()
        try:
            self.store.refresh_token(APPROVAL_ID, token)
        except AlreadyDecidedError:
            assert before != PENDING
        else:
            assert before == PENDING
            self.token = token

    @rule()
    def get(self):
        found = self.store.get(APPROVAL_ID)
        assert found is not None
        current, token = found
        assert current.status == self.status()
        assert token == self.token

    @invariant()
    def final_status_never_changes(self):
        status = self.status()
        if self.final_status is not None:
            assert status == self.final_status, "a decided approval changed status"
        elif status in TERMINAL:
            self.final_status = status

    @invariant()
    def at_most_one_claim_between_reverts(self):
        assert self.claims_since_pending <= 1

    @invariant()
    def reminder_is_stamped_at_most_once(self):
        stored = self.stored().get("remindedAt")
        expected = None if self.reminded_at is None else {"S": self.reminded_at}
        assert stored == expected

    @invariant()
    def token_changes_only_through_refresh(self):
        assert _s(self.stored()["taskToken"]) == self.token

    @invariant()
    def approver_only_while_or_after_deciding(self):
        if self.status() == PENDING:
            assert "approvedBy" not in self.stored()


ApprovalStoreMachine.TestCase.settings = settings(
    ApprovalStoreMachine.TestCase.settings, stateful_step_count=30
)
# Fails if any ApprovalStore condition is loosened or dropped (e.g. claim without
# "expiresAt > :now", expire without the status check, mark_reminded without
# attribute_not_exists(remindedAt)), or if a new expression appears unmodelled.
TestApprovalStoreMachine = ApprovalStoreMachine.TestCase
