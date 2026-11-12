"""Fixtures for the adversarial suite: the Stubber-backed clients of the unit tests."""

import pytest

from approval import handler as approval
from shared.approvals import ApprovalStore
from tests.adversarial.approval_helpers import ROLE_ARN, TABLE
from tests.unit.conftest import (  # noqa: F401 - re-exported as fixtures
    ddb_stub,
    fake_clients,
    s3_stub,
    sns_stub,
    stub_factory,
)


class ApprovalApi:
    """Stubbed DynamoDB and Step Functions behind the approval handler, with call counters."""

    def __init__(self, ddb, sfn) -> None:
        self.ddb = ddb
        self.sfn = sfn
        self.reads: list[dict] = []
        self.writes: list[dict] = []
        self.callbacks: list[tuple[str, dict]] = []
        self._count(ddb.client, "get_item", self.reads.append)
        self._count(ddb.client, "update_item", self.writes.append)
        self._count(ddb.client, "put_item", self.writes.append)
        for name in ("send_task_success", "send_task_failure"):
            self._count(sfn.client, name, lambda params, n=name: self.callbacks.append((n, params)))

    @staticmethod
    def _count(client, name: str, note) -> None:
        original = getattr(client, name)

        def counted(**params):
            note(params)
            return original(**params)

        setattr(client, name, counted)


@pytest.fixture
def api(monkeypatch, stub_factory) -> ApprovalApi:  # noqa: F811 - the fixture, by name
    """The approval handler in ``both`` mode over stubbed clients; every call is counted."""
    harness = ApprovalApi(stub_factory("dynamodb"), stub_factory("stepfunctions"))
    monkeypatch.setenv("APPROVAL_AUTH", "both")
    monkeypatch.setenv("APPROVER_ROLE_ARN", ROLE_ARN)
    monkeypatch.setattr(
        approval,
        "build_dependencies",
        lambda: (ApprovalStore(harness.ddb.client, TABLE), harness.sfn.client),
    )
    yield harness
    harness.ddb.assert_no_pending_responses()
    harness.sfn.assert_no_pending_responses()
