"""cdk-nag acknowledgments shared by several constructs.

Each finding is acknowledged next to the construct it concerns, with the exact
rule id from the synth report and a reason that explains why the finding does
not apply. Ids carry the plugin prefix (``AwsSolutions::`` or ``Serverless::``):
``Validations.acknowledge`` treats an unprefixed id as a CDK annotation.
"""

from aws_cdk import Acknowledgment, CfnResource, Stack, Validations
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_s3 as s3
from constructs import IConstruct

BASIC_EXECUTION_ROLE = (
    "AwsSolutions::AwsSolutions-IAM4"
    "[Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole]"
)

LOGGING_BASELINE_REASON = (
    "Lambda logging baseline: CreateLogGroup, CreateLogStream, PutLogEvents only"
)
PLANS_PREFIX_REASON = "Object-level grant scoped to the plans/ prefix"
RUNTIME_REASON = "Python 3.13 pinned across repo, CI and runtime; upgrade tracked in roadmap"
SYNC_INVOKE_REASON = (
    "Invoked synchronously by Step Functions; failures are caught by the state machine and alarmed"
)
OBSERVABILITY_REASON = "Weekly batch; observability via report, logs and alarms"
TASK_CALLBACK_REASON = (
    "states:SendTaskSuccess and SendTaskFailure do not support resource-level permissions; "
    "the task token is the capability"
)
BEARER_LINK_REASON = (
    "Approval links are bearer ids by design (spec 4.3, ADR-0008); GET is read-only and POST "
    "needs a typed confirmation; the IAM route is available via approvalAuth"
)
EXECUTOR_DESCRIBE_REASON = (
    "Re-verification Describe* calls do not support resource-level permissions"
)


def acknowledge(scope: IConstruct, findings: dict[str, str]) -> None:
    """Acknowledge each ``{rule id: reason}`` finding on ``scope`` and its children."""
    Validations.of(scope).acknowledge(
        *(Acknowledgment(id=rule_id, reason=reason) for rule_id, reason in findings.items())
    )


def wildcard_resource_finding(resource: IConstruct, suffix: str) -> str:
    """The IAM5 finding id for a grant on ``<resource arn><suffix>``.

    cdk-nag names the resource by its logical id, so the id is computed rather
    than copied, and keeps matching if the construct tree changes.
    """
    cfn_resource = resource.node.default_child
    if not isinstance(cfn_resource, CfnResource):
        raise TypeError(f"{resource.node.path} has no CloudFormation resource")
    logical_id = Stack.of(resource).get_logical_id(cfn_resource)
    return f"AwsSolutions::AwsSolutions-IAM5[Resource::<{logical_id}.Arn>{suffix}]"


def _nag_text(value: object) -> str:
    """A resolved value in cdk-nag's notation: ``Ref`` as ``<Name>``, joins flattened."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and "Ref" in value:
        return f"<{value['Ref']}>"
    if isinstance(value, dict) and "Fn::GetAtt" in value:
        return "<{}>".format(".".join(value["Fn::GetAtt"]))
    if isinstance(value, dict) and "Fn::Join" in value:
        separator, parts = value["Fn::Join"]
        return separator.join(_nag_text(part) for part in parts)
    raise TypeError(f"cannot name a cdk-nag finding for {value!r}")


def wildcard_arn_finding(scope: IConstruct, arn: str) -> str:
    """The IAM5 finding id for a grant on the wildcard ``arn`` built in ``scope``.

    The ARN may hold tokens (partition, account); they are resolved the way
    cdk-nag names them, so no account id is written into the source.
    """
    resolved = Stack.of(scope).resolve(arn)
    return f"AwsSolutions::AwsSolutions-IAM5[Resource::{_nag_text(resolved)}]"


def acknowledge_janitor_function(
    function: lambda_.Function,
    plans_bucket: s3.IBucket | None,
    *,
    invocation_reason: str = SYNC_INVOKE_REASON,
    observability_reason: str = OBSERVABILITY_REASON,
) -> None:
    """Acknowledge the findings every janitor Lambda function shares.

    The execution role's findings go on the role, the function's on the
    function. ``plans_bucket`` is the bucket of the function's object grant
    under ``plans/``, or None for a function without one. The two reasons
    default to those of the functions the state machine invokes.
    """
    role = function.role
    if role is None:  # Function always creates a role when none is passed in.
        raise RuntimeError("janitor function has no execution role")
    role_findings = {BASIC_EXECUTION_ROLE: LOGGING_BASELINE_REASON}
    if plans_bucket is not None:
        role_findings[wildcard_resource_finding(plans_bucket, "/plans/*")] = PLANS_PREFIX_REASON
    acknowledge(role, role_findings)
    acknowledge(
        function,
        {
            "AwsSolutions::AwsSolutions-L1": RUNTIME_REASON,
            "Serverless::Serverless-LambdaLatestVersion": RUNTIME_REASON,
            "Serverless::Serverless-LambdaDLQ": invocation_reason,
            "Serverless::Serverless-LambdaTracing": observability_reason,
        },
    )
