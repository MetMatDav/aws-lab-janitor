"""Shared boto3 client factory used by every detector."""

import threading

import boto3
from botocore.client import BaseClient
from botocore.config import Config

_CONFIG = Config(
    connect_timeout=5,
    read_timeout=20,
    retries={"mode": "standard", "max_attempts": 3},
)


class ClientFactory:
    """Creates and caches boto3 clients per ``(service, region)``.

    One shared session, bounded timeouts and retries, and a lock around client
    creation so detectors running in threads never build the same client twice.
    """

    def __init__(self, session: boto3.Session | None = None) -> None:
        self._session = session or boto3.Session()
        self._lock = threading.Lock()
        self._clients: dict[tuple[str, str], BaseClient] = {}

    def get(self, service: str, region: str) -> BaseClient:
        """Return the cached client for ``service`` in ``region``, creating it if needed."""
        key = (service, region)
        with self._lock:
            client = self._clients.get(key)
            if client is None:
                client = self._session.client(service, region_name=region, config=_CONFIG)
                self._clients[key] = client
            return client

    def enabled_regions(self) -> list[str]:
        """Sorted names of the regions enabled for the account (queried from us-east-1)."""
        response = self.get("ec2", "us-east-1").describe_regions(AllRegions=False)
        return sorted(region["RegionName"] for region in response["Regions"])
