"""Shared detector fixtures: Stubber-backed clients and a ready-made SweepContext."""

import logging
import os
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import boto3
import pytest
from botocore.client import BaseClient
from botocore.stub import Stubber

from detector.sweep import SweepContext
from shared.findings import ErrorFinding, Finding
from shared.settings import load_settings

NOW = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
REGION = "us-east-1"


def days_ago(days: int) -> datetime:
    """Timestamp ``days`` whole days before ``NOW``."""
    return NOW - timedelta(days=days)


def by_id(findings: list[Finding], kind: str) -> dict[str, Finding]:
    """Findings of ``kind`` keyed by resource id."""
    return {f.resource_id: f for f in findings if f.kind == kind}


def run_detector(
    detector: Callable[..., None], ctx: SweepContext, region: str = REGION
) -> tuple[list[Finding], list[ErrorFinding]]:
    """Run ``detector`` for ``region`` and return what it appended."""
    findings: list[Finding] = []
    errors: list[ErrorFinding] = []
    detector(region, ctx, findings, errors)
    return findings, errors


class FakeClients:
    """ClientFactory-shaped stand-in that returns pre-registered (stubbed) clients."""

    def __init__(self) -> None:
        self._clients: dict[tuple[str, str], BaseClient] = {}

    def add(self, service: str, region: str, client: BaseClient) -> None:
        self._clients[(service, region)] = client

    def get(self, service: str, region: str) -> BaseClient:
        return self._clients[(service, region)]


@pytest.fixture
def fake_clients() -> FakeClients:
    return FakeClients()


@pytest.fixture
def stub_factory(fake_clients) -> Callable[..., Stubber]:
    """Create a real boto3 client, register it in ``fake_clients`` and return its active Stubber."""
    stubbers: list[Stubber] = []

    def make(service: str, region: str = "us-east-1") -> Stubber:
        client = boto3.client(service, region_name=region)
        stubber = Stubber(client)
        stubber.activate()
        fake_clients.add(service, region, client)
        stubbers.append(stubber)
        return stubber

    yield make
    for stubber in stubbers:
        stubber.deactivate()


@pytest.fixture
def ec2_stub(stub_factory) -> Stubber:
    return stub_factory("ec2")


@pytest.fixture
def ctx_factory(fake_clients) -> Callable[..., SweepContext]:
    """Build a SweepContext over ``fake_clients``; keyword arguments override the defaults."""

    def make(**overrides) -> SweepContext:
        values = {
            "settings": load_settings({}, os.environ),
            "clients": fake_clients,
            "now": NOW,
            "account": "123456789012",
            "was_eligible_before": lambda _resource_id: False,
            "deadline": time.time() + 600,
            "log": logging.LoggerAdapter(logging.getLogger("test"), {"runId": "test"}),
        }
        values.update(overrides)
        return SweepContext(**values)

    return make


@pytest.fixture
def ctx(ctx_factory) -> SweepContext:
    return ctx_factory()


@pytest.fixture
def ctx_past_deadline(ctx_factory) -> SweepContext:
    return ctx_factory(deadline=time.time() - 1)


def _stubbed_client(service: str):
    client = boto3.client(service, region_name=REGION)
    stubber = Stubber(client)
    stubber.activate()
    yield client, stubber
    stubber.deactivate()


@pytest.fixture
def s3_stub():
    """A stubbed S3 client and its Stubber."""
    yield from _stubbed_client("s3")


@pytest.fixture
def sns_stub():
    """A stubbed SNS client and its Stubber."""
    yield from _stubbed_client("sns")


@pytest.fixture
def ddb_stub():
    """A stubbed DynamoDB client and its Stubber."""
    yield from _stubbed_client("dynamodb")
