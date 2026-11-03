from pathlib import Path

from aws_cdk import assertions
from aws_cdk.assertions import Match

from lab_janitor.markers import lambda_env
from tests.infra.helpers import DETECTOR_HANDLER


def _detector(template: assertions.Template) -> dict:
    functions = template.find_resources(
        "AWS::Lambda::Function", {"Properties": {"Handler": DETECTOR_HANDLER}}
    )
    (function,) = functions.values()
    return function


def test_detector_function_settings(template: assertions.Template):
    template.has_resource_properties(
        "AWS::Lambda::Function",
        {
            "Runtime": "python3.13",
            "Architectures": ["arm64"],
            "ReservedConcurrentExecutions": 1,
            "Handler": DETECTOR_HANDLER,
            "MemorySize": 512,
            "Timeout": 300,
            "LoggingConfig": Match.object_like({"LogFormat": "JSON"}),
        },
    )


def test_detector_environment(template: assertions.Template):
    variables = _detector(template)["Properties"]["Environment"]["Variables"]
    assert {k: variables[k] for k in lambda_env()} == lambda_env()
    assert variables["PLANS_BUCKET"] == {"Ref": "AuditPlansBucketDEC41664"}
    assert variables["AUDIT_TABLE"] == {"Ref": "AuditTable18A3095C"}
    assert variables["MIN_AGE_DAYS"] == "7"


def test_log_group_retention_90_days(template: assertions.Template):
    groups = template.find_resources("AWS::Logs::LogGroup")
    log_group_id = _detector(template)["Properties"]["LoggingConfig"]["LogGroup"]["Ref"]
    group = groups[log_group_id]
    assert group["Properties"]["RetentionInDays"] == 90
    assert group["DeletionPolicy"] == "Delete"
    assert group["UpdateReplacePolicy"] == "Delete"


def test_asset_excludes_pycache(cli_assembly: Path):
    staged = [p for p in cli_assembly.glob("asset.*") if (p / "detector" / "handler.py").exists()]
    assert len(staged) == 1
    (asset,) = staged
    assert not list(asset.rglob("*.pyc"))
    assert not list(asset.rglob("__pycache__"))
