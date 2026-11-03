import aws_cdk as cdk
import pytest
from aws_cdk import assertions
from aws_cdk.assertions import Match

from lab_janitor.stack import LabJanitorStack
from tests.infra.conftest import ENV
from tests.infra.helpers import (
    APPROVAL_HANDLER,
    actions,
    approval_statements,
    render,
    resources,
)

APPROVER_ROLE_ARN = "arn:aws:iam::123456789012:role/Approver"
LINK_ROUTES = {"GET /approvals/{id}", "POST /approvals/{id}"}
IAM_ROUTE = "POST /iam/approvals/{id}"
APPROVAL_KEYS = {"ForAllValues:StringLike": {"dynamodb:LeadingKeys": ["APPROVAL#*"]}}


def _template_for(approval_auth: str) -> assertions.Template:
    app = cdk.App(
        context={
            "reportEmail": "test@example.com",
            "approvalAuth": approval_auth,
            "approverRoleArn": APPROVER_ROLE_ARN,
        }
    )
    return assertions.Template.from_stack(LabJanitorStack(app, "LabJanitorStack", env=ENV))


@pytest.fixture(scope="module")
def iam_template() -> assertions.Template:
    return _template_for("iam")


@pytest.fixture(scope="module")
def both_template() -> assertions.Template:
    return _template_for("both")


def _routes(template: assertions.Template) -> dict[str, dict]:
    return {
        route["Properties"]["RouteKey"]: route["Properties"]
        for route in template.find_resources("AWS::ApiGatewayV2::Route").values()
    }


def _function(template: assertions.Template) -> dict:
    functions = template.find_resources(
        "AWS::Lambda::Function", {"Properties": {"Handler": APPROVAL_HANDLER}}
    )
    (function,) = functions.values()
    return function


def test_one_http_api_with_description(template: assertions.Template):
    template.resource_count_is("AWS::ApiGatewayV2::Api", 1)
    template.has_resource_properties(
        "AWS::ApiGatewayV2::Api",
        {"ProtocolType": "HTTP", "Description": "Lab janitor approval endpoint"},
    )


def test_link_mode_exposes_only_the_two_link_routes(template: assertions.Template):
    routes = _routes(template)
    assert set(routes) == LINK_ROUTES
    for route in routes.values():
        assert route["AuthorizationType"] == "NONE"


def test_iam_mode_exposes_only_the_iam_route_with_aws_iam(iam_template: assertions.Template):
    routes = _routes(iam_template)
    assert set(routes) == {IAM_ROUTE}
    assert routes[IAM_ROUTE]["AuthorizationType"] == "AWS_IAM"
    variables = _function(iam_template)["Properties"]["Environment"]["Variables"]
    assert variables["APPROVAL_AUTH"] == "iam"
    assert variables["APPROVER_ROLE_ARN"] == APPROVER_ROLE_ARN


def test_both_mode_exposes_all_three(both_template: assertions.Template):
    routes = _routes(both_template)
    assert set(routes) == LINK_ROUTES | {IAM_ROUTE}
    assert routes[IAM_ROUTE]["AuthorizationType"] == "AWS_IAM"
    for key in LINK_ROUTES:
        assert routes[key]["AuthorizationType"] == "NONE"


def test_every_route_targets_the_approval_handler(both_template: assertions.Template):
    (integration_id,) = both_template.find_resources("AWS::ApiGatewayV2::Integration")
    for route in _routes(both_template).values():
        assert render(route["Target"]) == f"integrations/<{integration_id}>"
    (integration,) = both_template.find_resources("AWS::ApiGatewayV2::Integration").values()
    function_id = next(
        iter(
            both_template.find_resources(
                "AWS::Lambda::Function", {"Properties": {"Handler": APPROVAL_HANDLER}}
            )
        )
    )
    assert integration["Properties"]["IntegrationType"] == "AWS_PROXY"
    assert integration["Properties"]["IntegrationUri"] == {"Fn::GetAtt": [function_id, "Arn"]}


def test_stage_is_default_with_throttle_and_access_logs_without_path(
    template: assertions.Template,
):
    (stage,) = template.find_resources("AWS::ApiGatewayV2::Stage").values()
    properties = stage["Properties"]
    assert properties["StageName"] == "$default"
    assert properties["AutoDeploy"] is True
    assert properties["DefaultRouteSettings"] == {
        "ThrottlingBurstLimit": 20,
        "ThrottlingRateLimit": 10,
    }
    log_format = properties["AccessLogSettings"]["Format"]
    assert "$context.path" not in log_format
    for field in ("$context.requestId", "$context.routeKey", "$context.status"):
        assert field in log_format
    log_group_id = properties["AccessLogSettings"]["DestinationArn"]["Fn::GetAtt"][0]
    group = template.find_resources("AWS::Logs::LogGroup")[log_group_id]
    assert group["Properties"]["RetentionInDays"] == 90
    assert group["DeletionPolicy"] == "Delete"


def test_handler_function_settings(template: assertions.Template):
    template.has_resource_properties(
        "AWS::Lambda::Function",
        {
            "Runtime": "python3.13",
            "Architectures": ["arm64"],
            "Handler": APPROVAL_HANDLER,
            "MemorySize": 256,
            "Timeout": 10,
            "LoggingConfig": Match.object_like({"LogFormat": "JSON"}),
        },
    )
    log_group_id = _function(template)["Properties"]["LoggingConfig"]["LogGroup"]["Ref"]
    group = template.find_resources("AWS::Logs::LogGroup")[log_group_id]
    assert group["Properties"]["RetentionInDays"] == 90
    assert group["DeletionPolicy"] == "Delete"


def test_handler_env_and_grants(template: assertions.Template):
    variables = _function(template)["Properties"]["Environment"]["Variables"]
    assert variables == {
        "AUDIT_TABLE": {"Ref": "AuditTable18A3095C"},
        "APPROVAL_AUTH": "link",
        "APPROVER_ROLE_ARN": "",
    }

    statements = approval_statements(template)
    granted = {a for st in statements for a in actions(st)}
    assert granted == {
        "dynamodb:GetItem",
        "dynamodb:UpdateItem",
        "states:SendTaskSuccess",
        "states:SendTaskFailure",
    }
    for st in statements:
        assert st["Effect"] == "Allow"
        if any(a.startswith("dynamodb:") for a in actions(st)):
            assert resources(st) == [{"Fn::GetAtt": ["AuditTable18A3095C", "Arn"]}]
            assert st["Condition"] == APPROVAL_KEYS
    (callbacks,) = [st for st in statements if st.get("Sid") == "TaskCallbacks"]
    assert sorted(actions(callbacks)) == ["states:SendTaskFailure", "states:SendTaskSuccess"]
    assert callbacks["Resource"] == "*"
    assert "Condition" not in callbacks
    assert not any(a.startswith(("s3:", "sns:")) for a in granted)


def test_handler_has_no_reserved_concurrency(template: assertions.Template):
    assert "ReservedConcurrentExecutions" not in _function(template)["Properties"]


def test_api_gateway_may_invoke_the_handler_only_from_this_api(template: assertions.Template):
    (api_id,) = template.find_resources("AWS::ApiGatewayV2::Api")
    permissions = [
        permission["Properties"]
        for permission in template.find_resources("AWS::Lambda::Permission").values()
        if permission["Properties"]["Principal"] == "apigateway.amazonaws.com"
    ]
    assert len(permissions) == len(LINK_ROUTES)
    for permission in permissions:
        assert render(permission["SourceArn"]).startswith(
            f"arn:<AWS::Partition>:execute-api:us-east-1:123456789012:<{api_id}>/*/*/"
        )


def test_notifier_receives_the_approval_url(template: assertions.Template):
    (api_id,) = template.find_resources("AWS::ApiGatewayV2::Api")
    functions = template.find_resources(
        "AWS::Lambda::Function", {"Properties": {"Handler": "notifier.handler.handler"}}
    )
    (notifier,) = functions.values()
    url = notifier["Properties"]["Environment"]["Variables"]["APPROVAL_URL"]
    # The API endpoint has no trailing slash, so the notifier appends "/approvals/<id>".
    assert url == {"Fn::GetAtt": [api_id, "ApiEndpoint"]}
