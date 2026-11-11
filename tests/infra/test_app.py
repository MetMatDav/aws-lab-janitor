import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from aws_cdk import cx_api

ROOT = Path(__file__).resolve().parents[2]


def _run_app(**env: str) -> subprocess.CompletedProcess:
    clean = {k: v for k, v in os.environ.items() if not k.startswith("CDK_")}
    return subprocess.run(
        [sys.executable, "app.py"],
        cwd=ROOT,
        env={**clean, **env, "CDK_CONTEXT_JSON": '{"reportEmail": "test@example.com"}'},
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )


@pytest.mark.parametrize(
    ("env", "missing"),
    [
        ({"CDK_DEFAULT_REGION": "us-east-1"}, "CDK_DEFAULT_ACCOUNT"),
        ({"CDK_DEFAULT_ACCOUNT": "123456789012"}, "CDK_DEFAULT_REGION"),
    ],
)
def test_app_requires_account_and_region(env: dict[str, str], missing: str):
    result = _run_app(**env)
    assert result.returncode != 0
    assert missing in result.stderr
    present = next(iter(env))
    assert f"{present} is not set" not in result.stderr


def test_stack_artifact_carries_stack_tags(cli_assembly: Path):
    artifact = cx_api.CloudAssembly(str(cli_assembly)).get_stack_by_name("LabJanitorStack")
    assert artifact.tags == {"Project": "aws-lab-janitor", "ManagedBy": "cdk"}


def test_resources_keep_the_project_tags(cli_template: dict):
    queue = next(r for r in cli_template["Resources"].values() if r["Type"] == "AWS::SQS::Queue")
    assert {"Key": "Project", "Value": "aws-lab-janitor"} in queue["Properties"]["Tags"]
    assert {"Key": "ManagedBy", "Value": "cdk"} in queue["Properties"]["Tags"]


def test_cli_outputs_are_stack_level_and_carry_no_email(cli_template: dict):
    # The CLI synthesis uses its own email domains, so a leak would show here
    # even if the unit-test emails changed.
    outputs = cli_template["Outputs"]
    assert set(outputs) == {
        "StateMachineArn",
        "PlansBucketName",
        "AuditTableName",
        "ApprovalApiUrl",
        "TrailLogBucketName",
    }
    assert "@" not in json.dumps(outputs)
