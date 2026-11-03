"""Shared detector fixtures: Stubber-backed clients and a ready-made SweepContext."""

import logging
import os
import time
from collections.abc import Callable
from datetime import UTC, datetime

import boto3
import pytest
from botocore.client import BaseClient
from botocore.stub import Stubber

from detector.sweep import SweepContext
from shared.settings import load_settings

NOW = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)


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
            "was_proposed_live": lambda _resource_id: False,
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
