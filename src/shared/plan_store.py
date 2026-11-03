"""Versioned S3 storage for plans and their reports.

The bucket must be versioned: the approval flow pins an exact object version
of the plan, so every write has to return a ``VersionId``.
"""

import json
from dataclasses import dataclass

from shared.plan import plan_bytes, plan_sha256

PLAN_CONTENT_TYPE = "application/json"
REPORT_CONTENT_TYPE = "text/plain; charset=utf-8"


class PlanIntegrityError(Exception):
    """The plan in S3, the detector's hash and the approval record do not all agree."""


@dataclass(frozen=True)
class StoredPlan:
    """Where a run's plan and report were written, and the plan's sha256."""

    plan_key: str
    plan_version_id: str
    plan_sha256: str
    report_key: str
    report_version_id: str


def plan_key(run_id: str) -> str:
    """S3 key of the plan document for ``run_id``."""
    return f"plans/{run_id}/plan.json"


def report_key(run_id: str) -> str:
    """S3 key of the human-readable report for ``run_id``."""
    return f"plans/{run_id}/report.txt"


class PlanStore:
    """Write and read plans and reports by exact object version."""

    def __init__(self, s3_client, bucket: str) -> None:
        self._s3 = s3_client
        self._bucket = bucket

    def put(self, run_id: str, plan: dict, report: str) -> StoredPlan:
        """Upload the canonical plan bytes and the report; return their versions.

        Raises ``RuntimeError`` if the bucket does not return a version id.
        """
        data = plan_bytes(plan)
        p_key = plan_key(run_id)
        r_key = report_key(run_id)
        plan_version = self._put_object(p_key, data, PLAN_CONTENT_TYPE)
        report_version = self._put_object(r_key, report.encode("utf-8"), REPORT_CONTENT_TYPE)
        return StoredPlan(
            plan_key=p_key,
            plan_version_id=plan_version,
            plan_sha256=plan_sha256(data),
            report_key=r_key,
            report_version_id=report_version,
        )

    def get_plan(self, run_id: str, version_id: str) -> tuple[dict, str]:
        """Return the plan at ``version_id`` and the sha256 of the bytes actually read."""
        data = self._get_object(plan_key(run_id), version_id)
        return json.loads(data), plan_sha256(data)

    def get_report(self, run_id: str, version_id: str) -> str:
        """Return the report text at ``version_id``."""
        return self._get_object(report_key(run_id), version_id).decode("utf-8")

    def _put_object(self, key: str, body: bytes, content_type: str) -> str:
        response = self._s3.put_object(
            Bucket=self._bucket, Key=key, Body=body, ContentType=content_type
        )
        version_id = response.get("VersionId")
        if not version_id:
            raise RuntimeError("plans bucket is not versioned")
        return version_id

    def _get_object(self, key: str, version_id: str) -> bytes:
        response = self._s3.get_object(Bucket=self._bucket, Key=key, VersionId=version_id)
        return response["Body"].read()
