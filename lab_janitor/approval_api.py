"""The approval endpoint: an HTTP API in front of the approval handler."""

import jsii
from aws_cdk import Duration, RemovalPolicy, Stack
from aws_cdk import aws_apigateway as apigateway
from aws_cdk import aws_apigatewayv2 as apigw
from aws_cdk import aws_apigatewayv2_authorizers as authorizers
from aws_cdk import aws_apigatewayv2_integrations as integrations
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from constructs import Construct

from lab_janitor.audit import AuditStore
from lab_janitor.config import JanitorConfig
from lab_janitor.nag import (
    BEARER_LINK_REASON,
    TASK_CALLBACK_REASON,
    acknowledge,
    acknowledge_janitor_function,
)

HANDLER = "approval.handler.handler"
LINK_PATH = "/approvals/{id}"
IAM_PATH = "/iam/approvals/{id}"
# One JSON object per request. No $context.path: on the link routes the path
# carries the approval id, which is a bearer credential.
ACCESS_LOG_FORMAT = (
    '{"requestId":"$context.requestId","requestTime":"$context.requestTime",'
    '"routeKey":"$context.routeKey","status":"$context.status",'
    '"ip":"$context.identity.sourceIp","error":"$context.error.message"}'
)
API_INVOKE_REASON = (
    "Invoked synchronously by API Gateway; the caller receives the error, so there is "
    "no asynchronous event to dead-letter"
)
API_OBSERVABILITY_REASON = (
    "A handful of approval requests per week; observability via access logs, JSON logs "
    "and the audit table"
)


@jsii.implements(apigw.IAccessLogSettings)
class AccessLogSettings:
    """Access log settings for an ``HttpStage`` (a plain dict does not deserialize)."""

    def __init__(
        self, destination: apigw.IAccessLogDestination, format: apigateway.AccessLogFormat
    ) -> None:
        self._destination = destination
        self._format = format

    @property
    def destination(self) -> apigw.IAccessLogDestination:
        """Where the stage writes its access logs."""
        return self._destination

    @property
    def format(self) -> apigateway.AccessLogFormat:
        """The access log line format."""
        return self._format


class ApprovalApi(Construct):
    """HTTP API and Lambda handler that turn an approval decision into a task callback.

    ``approvalAuth`` picks the routes: ``link`` exposes the bearer-link pair
    (``GET`` shows the plan, ``POST`` decides), ``iam`` exposes only the
    SigV4-signed ``POST /iam/approvals/{id}``, ``both`` exposes all three.
    The handler role reads and updates ``APPROVAL#`` records and sends task
    callbacks; it cannot read plans, publish, or touch lab resources.
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        config: JanitorConfig,
        audit: AuditStore,
        code: lambda_.Code,
    ) -> None:
        super().__init__(scope, construct_id)

        self.log_group = logs.LogGroup(
            self,
            "Logs",
            retention=logs.RetentionDays.THREE_MONTHS,
            removal_policy=RemovalPolicy.DESTROY,
        )

        # No reserved concurrency: decisions claim the approval record with a
        # conditional update, so concurrent requests cannot both win.
        self.function = lambda_.Function(
            self,
            "Function",
            runtime=lambda_.Runtime.PYTHON_3_13,
            architecture=lambda_.Architecture.ARM_64,
            handler=HANDLER,
            code=code,
            memory_size=256,
            timeout=Duration.seconds(10),
            log_group=self.log_group,
            logging_format=lambda_.LoggingFormat.JSON,
            environment={
                "AUDIT_TABLE": audit.table.table_name,
                "APPROVAL_AUTH": config.approval_auth,
                "APPROVER_ROLE_ARN": config.approver_role_arn or "",
            },
            description="Lab janitor approval handler: records decisions and resumes the run",
        )

        audit.grant_approval_reads(self.function)
        audit.grant_approval_updates(self.function)
        self.function.add_to_role_policy(
            iam.PolicyStatement(
                sid="TaskCallbacks",
                actions=["states:SendTaskSuccess", "states:SendTaskFailure"],
                resources=["*"],
            )
        )

        self.api = apigw.HttpApi(
            self,
            "Api",
            api_name=f"{Stack.of(self).stack_name}-approvals",
            description="Lab janitor approval endpoint",
            create_default_stage=False,
        )
        self.access_log_group = logs.LogGroup(
            self,
            "AccessLogs",
            retention=logs.RetentionDays.THREE_MONTHS,
            removal_policy=RemovalPolicy.DESTROY,
        )
        self.stage = apigw.HttpStage(
            self,
            "Stage",
            http_api=self.api,
            stage_name="$default",
            auto_deploy=True,
            throttle=apigw.ThrottleSettings(rate_limit=10, burst_limit=20),
            access_log_settings=AccessLogSettings(
                destination=apigw.LogGroupLogDestination(self.access_log_group),
                format=apigateway.AccessLogFormat.custom(ACCESS_LOG_FORMAT),
            ),
        )
        self.url = self.api.api_endpoint

        integration = integrations.HttpLambdaIntegration("Handler", self.function)
        if config.approval_auth in ("link", "both"):
            link_routes = self.api.add_routes(
                path=LINK_PATH,
                methods=[apigw.HttpMethod.GET, apigw.HttpMethod.POST],
                integration=integration,
            )
            for route in link_routes:
                acknowledge(route, {"AwsSolutions::AwsSolutions-APIG4": BEARER_LINK_REASON})
        if config.approval_auth in ("iam", "both"):
            self.api.add_routes(
                path=IAM_PATH,
                methods=[apigw.HttpMethod.POST],
                integration=integration,
                authorizer=authorizers.HttpIamAuthorizer(),
            )

        acknowledge_janitor_function(
            self.function,
            None,
            invocation_reason=API_INVOKE_REASON,
            observability_reason=API_OBSERVABILITY_REASON,
        )
        role = self.function.role
        if role is None:  # Function always creates a role when none is passed in.
            raise RuntimeError("approval function has no execution role")
        acknowledge(role, {"AwsSolutions::AwsSolutions-IAM5[Resource::*]": TASK_CALLBACK_REASON})
