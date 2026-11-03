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


def acknowledge_janitor_function(function: lambda_.Function, plans_bucket: s3.IBucket) -> None:
    """Acknowledge the findings every janitor Lambda function shares.

    The execution role's findings go on the role, the function's on the
    function. Both janitor functions hold an object grant under ``plans/``.
    """
    role = function.role
    if role is None:  # Function always creates a role when none is passed in.
        raise RuntimeError("janitor function has no execution role")
    acknowledge(
        role,
        {
            BASIC_EXECUTION_ROLE: LOGGING_BASELINE_REASON,
            wildcard_resource_finding(plans_bucket, "/plans/*"): PLANS_PREFIX_REASON,
        },
    )
    acknowledge(
        function,
        {
            "AwsSolutions::AwsSolutions-L1": RUNTIME_REASON,
            "Serverless::Serverless-LambdaLatestVersion": RUNTIME_REASON,
            "Serverless::Serverless-LambdaDLQ": SYNC_INVOKE_REASON,
            "Serverless::Serverless-LambdaTracing": OBSERVABILITY_REASON,
        },
    )
