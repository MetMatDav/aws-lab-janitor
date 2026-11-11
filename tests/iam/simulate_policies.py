"""Simulate the executor's IAM policy and write the result as evidence.

Synthesizes the stack for a placeholder account, takes every statement
granted to the executor function's role, and asks IAM
(``iam:SimulateCustomPolicy``, a read-only call) what that policy decides for
a fixed list of cases: lab versus non-lab resources, the keep tag, and
actions the janitor must never have. Run it from the repository root:

    python -m tests.iam.simulate_policies [--profile P] [--out FILE]

The template is synthesized here rather than read from ``cdk.out``: the CDK
CLI replaces ``CDK_DEFAULT_ACCOUNT`` with the caller's account, so a template
from the CLI carries a real account id that no case would match.

What this proves is the policy logic: given a resource with these tags, this
is the decision. It does not prove that a service sends a tag key when it
authorizes a call; that comes from the Service Authorization Reference, which
lists ``aws:ResourceTag`` for every resource type used here.

Needs AWS credentials, so it is not part of the test suite or of CI. The
account id in the report is a placeholder: resource ARNs are made up.
"""

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import aws_cdk as cdk
import boto3
from aws_cdk import assertions

from lab_janitor.stack import LabJanitorStack

ACCOUNT = "123456789012"
REGION = "us-east-1"
TABLE_ARN = f"arn:aws:dynamodb:{REGION}:{ACCOUNT}:table/audit"
PLANS_BUCKET_ARN = "arn:aws:s3:::plans"
EXECUTOR_HANDLER = "executor.handler.handler"
DEFAULT_OUT = "docs/evidence/iam-simulation.txt"
ROOT = Path(__file__).resolve().parents[2]
ACCOUNT_ID = re.compile(r"\b\d{12}\b")

PSEUDO = {"AWS::Partition": "aws", "AWS::AccountId": ACCOUNT, "AWS::Region": REGION}
ARN_BY_TYPE = {
    "AWS::DynamoDB::GlobalTable": TABLE_ARN,
    "AWS::DynamoDB::Table": TABLE_ARN,
    "AWS::S3::Bucket": PLANS_BUCKET_ARN,
}

LAB = {"Purpose": "lab"}
KEEP = {"janitor": "keep"}
SNAPSHOT = f"arn:aws:ec2:{REGION}::snapshot/snap-0123456789abcdef0"
VOLUME = f"arn:aws:ec2:{REGION}:{ACCOUNT}:volume/vol-0123456789abcdef0"
ADDRESS = f"arn:aws:ec2:{REGION}:{ACCOUNT}:elastic-ip/eipalloc-0123456789abcdef0"
INSTANCE = f"arn:aws:ec2:{REGION}:{ACCOUNT}:instance/i-0123456789abcdef0"
LAB_DASHBOARD = f"arn:aws:cloudwatch::{ACCOUNT}:dashboard/LAB-demo"
PROD_DASHBOARD = f"arn:aws:cloudwatch::{ACCOUNT}:dashboard/PROD-demo"
LAB_MONITOR = f"arn:aws:internetmonitor:{REGION}:{ACCOUNT}:monitor/LAB-demo"
PROD_MONITOR = f"arn:aws:internetmonitor:{REGION}:{ACCOUNT}:monitor/PROD-demo"

ALLOWED, IMPLICIT_DENY, EXPLICIT_DENY = "allowed", "implicitDeny", "explicitDeny"


@dataclass(frozen=True)
class Case:
    """One question for the simulator and the decision the design promises."""

    name: str
    action: str
    resource: str
    expected: str
    tags: dict[str, str] = field(default_factory=dict)
    leading_keys: tuple[str, ...] = ()


CASES: tuple[Case, ...] = (
    Case("dashboard with the lab prefix", "cloudwatch:DeleteDashboards", LAB_DASHBOARD, ALLOWED),
    Case(
        "dashboard without the lab prefix",
        "cloudwatch:DeleteDashboards",
        PROD_DASHBOARD,
        IMPLICIT_DENY,
    ),
    Case("kept lab dashboard", "cloudwatch:DeleteDashboards", LAB_DASHBOARD, EXPLICIT_DENY, KEEP),
    Case("monitor with the lab prefix", "internetmonitor:DeleteMonitor", LAB_MONITOR, ALLOWED),
    Case(
        "monitor without the lab prefix",
        "internetmonitor:DeleteMonitor",
        PROD_MONITOR,
        IMPLICIT_DENY,
    ),
    Case("kept lab monitor", "internetmonitor:DeleteMonitor", LAB_MONITOR, EXPLICIT_DENY, KEEP),
    Case(
        "disabling a kept lab monitor",
        "internetmonitor:UpdateMonitor",
        LAB_MONITOR,
        EXPLICIT_DENY,
        KEEP,
    ),
    Case("lab-tagged snapshot", "ec2:DeleteSnapshot", SNAPSHOT, ALLOWED, LAB),
    Case("untagged snapshot", "ec2:DeleteSnapshot", SNAPSHOT, IMPLICIT_DENY),
    Case("kept lab snapshot", "ec2:DeleteSnapshot", SNAPSHOT, EXPLICIT_DENY, {**LAB, **KEEP}),
    Case("lab-tagged volume", "ec2:DeleteVolume", VOLUME, ALLOWED, LAB),
    Case("untagged volume", "ec2:DeleteVolume", VOLUME, IMPLICIT_DENY),
    Case("lab-tagged address", "ec2:ReleaseAddress", ADDRESS, ALLOWED, LAB),
    Case("untagged address", "ec2:ReleaseAddress", ADDRESS, IMPLICIT_DENY),
    Case("kept lab address", "ec2:ReleaseAddress", ADDRESS, EXPLICIT_DENY, {**LAB, **KEEP}),
    Case("lab-tagged instance", "ec2:TerminateInstances", INSTANCE, IMPLICIT_DENY, LAB),
    Case("tagging a snapshot", "ec2:CreateTags", SNAPSHOT, IMPLICIT_DENY, LAB),
    Case("the plans bucket itself", "s3:DeleteBucket", PLANS_BUCKET_ARN, IMPLICIT_DENY),
    Case(
        "a stored plan",
        "s3:DeleteObject",
        f"{PLANS_BUCKET_ARN}/plans/run/plan.json",
        IMPLICIT_DENY,
    ),
    Case(
        "lab-tagged database",
        "rds:DeleteDBInstance",
        f"arn:aws:rds:{REGION}:{ACCOUNT}:db:demo",
        IMPLICIT_DENY,
        LAB,
    ),
    Case(
        "audit item under a run",
        "dynamodb:PutItem",
        TABLE_ARN,
        ALLOWED,
        leading_keys=("RUN#20261110T140012Z-7f3a",),
    ),
    Case(
        "audit item under an approval",
        "dynamodb:PutItem",
        TABLE_ARN,
        IMPLICIT_DENY,
        leading_keys=("APPROVAL#abc",),
    ),
)


def synthesized_template() -> dict:
    """The stack's template for the placeholder account, with the context of ``cdk.json``."""
    context = json.loads((ROOT / "cdk.json").read_text())["context"]
    app = cdk.App(context={**context, "reportEmail": "simulation@example.com"})
    stack = LabJanitorStack(
        app, "LabJanitorStack", env=cdk.Environment(account=ACCOUNT, region=REGION)
    )
    return assertions.Template.from_stack(stack).to_json()


def _attribute(logical_id: str, attribute: str, resources: dict) -> str:
    """A made-up value for ``Fn::GetAtt``: the fixed table or bucket ARN where it matters."""
    if attribute != "Arn":
        return f"{logical_id}.{attribute}".lower()
    resource_type = resources.get(logical_id, {}).get("Type", "")
    return ARN_BY_TYPE.get(resource_type, f"arn:aws:simulated:::{logical_id}")


def _resolve(value: object, resources: dict) -> object:
    """``value`` with CloudFormation intrinsics replaced by fixed, made-up names."""
    if isinstance(value, list):
        return [_resolve(item, resources) for item in value]
    if not isinstance(value, dict):
        return value
    if set(value) == {"Ref"}:
        return PSEUDO.get(value["Ref"], value["Ref"].lower())
    if set(value) == {"Fn::GetAtt"}:
        return _attribute(*value["Fn::GetAtt"], resources)
    if set(value) == {"Fn::Join"}:
        separator, parts = value["Fn::Join"]
        return separator.join(str(_resolve(part, resources)) for part in parts)
    return {key: _resolve(item, resources) for key, item in value.items()}


def role_policy(template: dict, handler: str) -> dict:
    """One policy document with every statement granted to the role of ``handler``'s function."""
    resources = template["Resources"]
    (role_id,) = {
        resource["Properties"]["Role"]["Fn::GetAtt"][0]
        for resource in resources.values()
        if resource["Type"] == "AWS::Lambda::Function"
        and resource["Properties"].get("Handler") == handler
    }
    statements = [
        statement
        for resource in resources.values()
        if resource["Type"] == "AWS::IAM::Policy"
        and {"Ref": role_id} in resource["Properties"].get("Roles", [])
        for statement in resource["Properties"]["PolicyDocument"]["Statement"]
    ]
    statements += [
        statement
        for policy in resources[role_id]["Properties"].get("Policies", [])
        for statement in policy["PolicyDocument"]["Statement"]
    ]
    return {"Version": "2012-10-17", "Statement": _resolve(statements, resources)}


def _context(case: Case) -> list[dict]:
    entries = [
        {
            "ContextKeyName": f"aws:ResourceTag/{key}",
            "ContextKeyValues": [value],
            "ContextKeyType": "string",
        }
        for key, value in case.tags.items()
    ]
    if case.leading_keys:
        entries.append(
            {
                "ContextKeyName": "dynamodb:LeadingKeys",
                "ContextKeyValues": list(case.leading_keys),
                "ContextKeyType": "stringList",
            }
        )
    return entries


def evaluate(iam, policy: dict, case: Case) -> str:
    """The simulator's decision for ``case`` under ``policy``."""
    response = iam.simulate_custom_policy(
        PolicyInputList=[json.dumps(policy)],
        ActionNames=[case.action],
        ResourceArns=[case.resource],
        ContextEntries=_context(case),
    )
    (result,) = response["EvaluationResults"]
    return result["EvalDecision"]


def render(results: list[tuple[Case, str]], today: str) -> str:
    """The evidence file: one PASS or FAIL line per case and a totals line."""
    passed = sum(1 for case, actual in results if actual == case.expected)
    lines = [
        "IAM policy simulation of the lab janitor executor role",
        f"Date: {today}  API: iam:SimulateCustomPolicy  Policy: executor role, synthesized",
        f"Account id {ACCOUNT} and all resource ids are placeholders.",
        "",
    ]
    for case, actual in results:
        verdict = "PASS" if actual == case.expected else "FAIL"
        tags = ",".join(f"{key}={value}" for key, value in case.tags.items()) or "-"
        keys = ",".join(case.leading_keys) or "-"
        lines.append(
            f"{verdict}  {case.name}: {case.action}  {case.resource}  tags={tags}  "
            f"leadingKeys={keys}  expected={case.expected}  actual={actual}"
        )
    lines += ["", f"{len(results)} cases, {passed} passed, {len(results) - passed} failed"]
    return "\n".join(lines) + "\n"


def iam_client(profile: str | None):
    """An IAM client for ``profile`` (or the default credentials)."""
    return boto3.Session(profile_name=profile).client("iam")


def main(argv: list[str] | None = None) -> int:
    """Run every case and write the report; exit code 1 if any decision differs."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", default=DEFAULT_OUT)
    parser.add_argument("--profile")
    args = parser.parse_args(argv)

    policy = role_policy(synthesized_template(), EXECUTOR_HANDLER)
    foreign = set(ACCOUNT_ID.findall(json.dumps(policy))) - {ACCOUNT}
    if foreign:
        # Every case would be decided against the wrong account, and the
        # report would publish a real account id.
        print(
            "error: the policy mentions an account id other than the placeholder", file=sys.stderr
        )
        return 2
    iam = iam_client(args.profile)
    results = [(case, evaluate(iam, policy, case)) for case in CASES]
    report = render(results, datetime.now(UTC).date().isoformat())
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report)
    print(report, end="")
    return 0 if all(actual == case.expected for case, actual in results) else 1


if __name__ == "__main__":
    sys.exit(main())
