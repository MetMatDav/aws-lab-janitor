import pytest
from aws_cdk import assertions

from lab_janitor.markers import (
    KEEP_TAG_KEY,
    KEEP_TAG_VALUE,
    LAB_NAME_PREFIX,
    LAB_TAG_KEY,
    LAB_TAG_VALUE,
)
from tests.infra.helpers import (
    APPROVAL_HANDLER,
    DETECTOR_HANDLER,
    EXECUTOR_HANDLER,
    NOTIFIER_HANDLER,
    actions,
    detector_statements,
    executor_statements,
    function_role_id,
    iam_statements,
    reminder_role_id,
    render,
    resources,
    role_statements,
    state_machine_role_id,
)

MUTATING_PREFIXES = (
    "Delete",
    "Release",
    "Terminate",
    "Create",
    "Put",
    "Update",
    "Tag",
    "Untag",
    "Modify",
    "Stop",
    "Start",
    "Reboot",
)

DETECTOR_READ_ACTIONS = {
    "ec2:DescribeRegions",
    "ec2:DescribeInstances",
    "ec2:DescribeNatGateways",
    "ec2:DescribeAddresses",
    "ec2:DescribeVolumes",
    "ec2:DescribeSnapshots",
    "ec2:DescribeImages",
    "ec2:DescribeVpcEndpoints",
    "ec2:DescribeNetworkInterfaces",
    "elasticloadbalancing:DescribeLoadBalancers",
    "elasticloadbalancing:DescribeTags",
    "rds:DescribeDBInstances",
    "rds:DescribeDBClusters",
    "rds:ListTagsForResource",
    "elasticache:DescribeCacheClusters",
    "elasticache:ListTagsForResource",
    "internetmonitor:ListMonitors",
    "internetmonitor:ListTagsForResource",
    "cloudwatch:ListDashboards",
    "cloudwatch:ListTagsForResource",
    "s3:ListAllMyBuckets",
    "s3:GetBucketTagging",
    "cloudfront:ListDistributions",
    "sts:GetCallerIdentity",
}

# Only these remove or alter resources; the guardrails below hunt for them.
DELETE_PREFIXES = ("Delete", "Release", "Terminate")

EXECUTOR_MUTATING_ACTIONS = {
    "ec2:DeleteSnapshot",
    "ec2:DeleteVolume",
    "ec2:ReleaseAddress",
    "cloudwatch:DeleteDashboards",
    "internetmonitor:UpdateMonitor",
    "internetmonitor:DeleteMonitor",
}
# The executor's own audit trail: RUN# items (LeadingKeys) and run events on
# the default bus. They write evidence, not lab resources.
EXECUTOR_AUDIT_WRITES = {"dynamodb:PutItem", "events:PutEvents"}
EXECUTOR_ACTIONS = (
    EXECUTOR_MUTATING_ACTIONS
    | EXECUTOR_AUDIT_WRITES
    | {
        "internetmonitor:GetMonitor",
        "ec2:DescribeSnapshots",
        "ec2:DescribeVolumes",
        "ec2:DescribeAddresses",
        "ec2:DescribeImages",
        "cloudwatch:ListTagsForResource",
        "s3:GetObject",
        "s3:GetObjectVersion",
        "dynamodb:GetItem",
    }
)
NOTIFIER_ACTIONS = {
    "s3:GetObject",
    "s3:GetObjectVersion",
    "dynamodb:PutItem",
    "dynamodb:UpdateItem",
    "sns:Publish",
    "sns:ListSubscriptionsByTopic",
}

TABLE_ARN = {"Fn::GetAtt": ["AuditTable18A3095C", "Arn"]}


def _allow_statements_with(statements: list[dict], action: str) -> list[dict]:
    return [st for st in statements if st["Effect"] == "Allow" and action in actions(st)]


def test_detector_role_has_no_mutating_actions_outside_its_bucket_and_table(
    template: assertions.Template,
):
    statements = detector_statements(template)
    assert statements
    for st in statements:
        for a in actions(st):
            if a in {"s3:PutObject", "dynamodb:PutItem"}:
                continue
            assert not a.split(":")[1].startswith(MUTATING_PREFIXES), a


def test_detector_role_action_set_is_exact(template: assertions.Template):
    granted = {a for st in detector_statements(template) for a in actions(st)}
    assert granted == DETECTOR_READ_ACTIONS | {"s3:PutObject", "dynamodb:PutItem", "dynamodb:Query"}


def test_detector_role_managed_policies_are_only_basic_execution(template: assertions.Template):
    role = template.find_resources("AWS::IAM::Role")[function_role_id(template, DETECTOR_HANDLER)]
    (managed,) = role["Properties"]["ManagedPolicyArns"]
    # The partition is a Ref, so the ARN is an Fn::Join whose last part is a literal.
    arn_tail = managed if isinstance(managed, str) else managed["Fn::Join"][1][-1]
    assert arn_tail.endswith("service-role/AWSLambdaBasicExecutionRole")


def test_detector_read_actions_are_exactly_the_listed_set(template: assertions.Template):
    sweeps = [
        st for st in detector_statements(template) if st.get("Sid") == "DetectorReadOnlySweep"
    ]
    assert len(sweeps) == 1
    (sweep,) = sweeps
    assert sweep["Effect"] == "Allow"
    assert sweep["Resource"] == "*"
    assert len(actions(sweep)) == len(DETECTOR_READ_ACTIONS) == 24
    assert set(actions(sweep)) == DETECTOR_READ_ACTIONS


def test_detector_can_put_plans_only_under_plans_prefix(template: assertions.Template):
    puts = _allow_statements_with(detector_statements(template), "s3:PutObject")
    assert len(puts) == 1
    (put,) = puts
    assert actions(put) == ["s3:PutObject"]
    (resource,) = resources(put)
    join = resource["Fn::Join"][1]
    assert join[0] == {"Fn::GetAtt": ["AuditPlansBucketDEC41664", "Arn"]}
    assert join[1] == "/plans/*"


def test_detector_dynamodb_write_grants_carry_leading_keys(template: assertions.Template):
    statements = detector_statements(template)
    writes = _allow_statements_with(statements, "dynamodb:PutItem") + _allow_statements_with(
        statements, "dynamodb:UpdateItem"
    )
    assert writes
    for st in writes:
        assert st["Condition"] == {"ForAllValues:StringLike": {"dynamodb:LeadingKeys": ["RUN#*"]}}


def test_detector_query_grant_is_limited_to_table_and_index(template: assertions.Template):
    queries = _allow_statements_with(detector_statements(template), "dynamodb:Query")
    assert len(queries) == 1
    (query,) = queries
    assert resources(query) == [
        TABLE_ARN,
        {"Fn::Join": ["", [TABLE_ARN, "/index/by-resource"]]},
    ]


def test_plans_bucket_policy_denies_put_except_detector_role(template: assertions.Template):
    role_id = function_role_id(template, DETECTOR_HANDLER)
    policies = template.find_resources("AWS::S3::BucketPolicy")
    put_denies = [
        st
        for policy in policies.values()
        for st in policy["Properties"]["PolicyDocument"]["Statement"]
        if st["Effect"] == "Deny" and st.get("Principal") == "*" and actions(st) == ["s3:PutObject"]
    ]
    assert len(put_denies) == 1
    (deny,) = put_denies
    (resource,) = resources(deny)
    assert resource["Fn::Join"][1][1] == "/plans/*"
    assert deny["Condition"] == {
        "StringNotEquals": {"aws:PrincipalArn": {"Fn::GetAtt": [role_id, "Arn"]}}
    }


def test_no_statement_in_the_stack_allows_iam_or_wildcard_actions(template: assertions.Template):
    for st in iam_statements(template):
        if st["Effect"] != "Allow":
            continue
        for a in actions(st):
            assert a != "*", st
            assert not a.startswith("iam:"), st
            assert not a.endswith(":*"), st


# --- template-wide delete guardrails (Plan 2) -----------------------------------------------


@pytest.fixture(params=["template", "live_template"])
def any_template(request: pytest.FixtureRequest) -> assertions.Template:
    """The dry-run template and the live (deleting) template; both must hold the guardrails."""
    return request.getfixturevalue(request.param)


def _role_refs(value: object) -> str | None:
    """The logical id a ``Ref`` or ``Fn::GetAtt`` points to, if any."""
    if isinstance(value, dict) and "Ref" in value:
        return value["Ref"]
    if isinstance(value, dict) and "Fn::GetAtt" in value:
        return value["Fn::GetAtt"][0]
    return None


def _all_policy_statements(template: assertions.Template) -> list[tuple[str, set[str], dict]]:
    """Every statement of every policy document in the template, whatever the resource type.

    Reads ``Properties.PolicyDocument`` (IAM policies and the S3, SNS and SQS
    resource policies) and ``Properties.Policies[*].PolicyDocument`` (inline
    role policies). Each statement comes with the logical id of the resource
    holding it and the roles it is granted to: the attached roles of an
    ``AWS::IAM::Policy``, the role itself for an inline policy, and no role
    for a resource policy.
    """
    found = []
    for logical_id, resource in template.to_json()["Resources"].items():
        properties = resource.get("Properties", {})
        documents = []
        if "PolicyDocument" in properties:
            if resource["Type"] == "AWS::IAM::Policy":
                roles = {_role_refs(role) for role in properties.get("Roles", [])}
            else:
                roles = set()
            documents.append((properties["PolicyDocument"], roles))
        for policy in properties.get("Policies", []):
            roles = {logical_id} if resource["Type"] == "AWS::IAM::Role" else set()
            documents.append((policy["PolicyDocument"], roles))
        for document, roles in documents:
            statements = document.get("Statement", [])
            for statement in [statements] if isinstance(statements, dict) else statements:
                found.append((logical_id, roles, statement))
    return found


def _is_delete(action: str) -> bool:
    return action.split(":")[1].startswith(DELETE_PREFIXES)


def test_every_policy_is_attached_to_a_role_only(any_template: assertions.Template):
    # So the per-role checks below see every identity statement in the template.
    assert not any_template.find_resources("AWS::IAM::ManagedPolicy")
    for policy in any_template.find_resources("AWS::IAM::Policy").values():
        properties = policy["Properties"]
        assert properties.get("Roles")
        assert "Users" not in properties
        assert "Groups" not in properties


def test_policy_scan_covers_identity_and_resource_policies(any_template: assertions.Template):
    holders = {
        any_template.to_json()["Resources"][logical_id]["Type"]
        for logical_id, _, _ in _all_policy_statements(any_template)
    }
    assert {
        "AWS::IAM::Policy",
        "AWS::S3::BucketPolicy",
        "AWS::SNS::TopicPolicy",
        "AWS::SQS::QueuePolicy",
    } <= holders


def test_delete_allows_belong_to_the_executor_or_are_log_delivery(
    any_template: assertions.Template,
):
    executor_role = function_role_id(any_template, EXECUTOR_HANDLER)
    machine_role = state_machine_role_id(any_template)
    executor_deletes = set()
    for logical_id, roles, st in _all_policy_statements(any_template):
        if st["Effect"] != "Allow":
            continue
        deletes = {a for a in actions(st) if _is_delete(a)}
        if not deletes:
            continue
        if roles == {executor_role}:
            executor_deletes |= deletes
            continue
        assert roles == {machine_role}, (logical_id, roles, deletes)
        assert deletes == {"logs:DeleteLogDelivery"}, (logical_id, deletes)
    assert executor_deletes == {a for a in EXECUTOR_MUTATING_ACTIONS if _is_delete(a)}


def test_executor_mutating_allows_are_locked_to_lab_resources(any_template: assertions.Template):
    locked = 0
    for st in executor_statements(any_template):
        if st["Effect"] != "Allow":
            continue
        mutating = [
            a
            for a in actions(st)
            if a.split(":")[1].startswith(MUTATING_PREFIXES) and a not in EXECUTOR_AUDIT_WRITES
        ]
        if not mutating:
            continue
        assert set(mutating) <= EXECUTOR_MUTATING_ACTIONS, mutating
        assert "*" not in resources(st), st
        lab_arns = all(render(r).endswith(f"/{LAB_NAME_PREFIX}*") for r in resources(st))
        lab_tag = (
            st.get("Condition", {}).get("StringEquals", {}).get(f"aws:ResourceTag/{LAB_TAG_KEY}")
            == LAB_TAG_VALUE
        )
        assert lab_arns or lab_tag, st
        locked += 1
    assert locked == 2


def test_executor_audit_writes_are_scoped(any_template: assertions.Template):
    for st in executor_statements(any_template):
        if st["Effect"] == "Allow" and set(actions(st)) & EXECUTOR_AUDIT_WRITES:
            assert "*" not in resources(st), st


def test_executor_denies_mutations_on_kept_resources(any_template: assertions.Template):
    denies = [st for st in executor_statements(any_template) if st["Effect"] == "Deny"]
    assert len(denies) == 1
    (deny,) = denies
    assert deny["Sid"] == "ExecutorDenyKept"
    assert set(actions(deny)) == EXECUTOR_MUTATING_ACTIONS
    assert deny["Resource"] == "*"
    assert deny["Condition"] == {
        "StringEquals": {f"aws:ResourceTag/{KEEP_TAG_KEY}": KEEP_TAG_VALUE}
    }


def test_no_policy_statement_uses_not_action_or_not_resource(any_template: assertions.Template):
    # Every check in this file reads Action and Resource; a NotAction or
    # NotResource statement would grant things none of them can see.
    for logical_id, _roles, statement in _all_policy_statements(any_template):
        assert "NotAction" not in statement, logical_id
        assert "NotResource" not in statement, logical_id
        assert "NotPrincipal" not in statement, logical_id


@pytest.mark.parametrize(
    "handler", [DETECTOR_HANDLER, NOTIFIER_HANDLER, APPROVAL_HANDLER, EXECUTOR_HANDLER]
)
def test_function_roles_carry_only_the_basic_execution_managed_policy(
    any_template: assertions.Template, handler: str
):
    # A managed policy is invisible to the statement scans above.
    role = any_template.find_resources("AWS::IAM::Role")[function_role_id(any_template, handler)]
    (managed,) = role["Properties"]["ManagedPolicyArns"]
    assert render(managed).endswith(":iam::aws:policy/service-role/AWSLambdaBasicExecutionRole")


def test_state_machine_roles_have_no_managed_policies(any_template: assertions.Template):
    roles = any_template.find_resources("AWS::IAM::Role")
    for role_id in (state_machine_role_id(any_template), reminder_role_id(any_template)):
        assert "ManagedPolicyArns" not in roles[role_id]["Properties"], role_id


def test_executor_role_action_set_is_exact(any_template: assertions.Template):
    granted = {a for st in executor_statements(any_template) for a in actions(st)}
    assert granted == EXECUTOR_ACTIONS


def test_notifier_role_action_set_is_exact(any_template: assertions.Template):
    granted = {a for st in role_statements(any_template, NOTIFIER_HANDLER) for a in actions(st)}
    assert granted == NOTIFIER_ACTIONS


def test_dynamodb_grants_carry_leading_keys_except_query(any_template: assertions.Template):
    seen = 0
    for st in iam_statements(any_template):
        dynamodb = {a for a in actions(st) if a.startswith("dynamodb:")}
        if st["Effect"] != "Allow" or not dynamodb or dynamodb == {"dynamodb:Query"}:
            continue
        assert "dynamodb:Query" not in dynamodb, st
        leading = st["Condition"]["ForAllValues:StringLike"]["dynamodb:LeadingKeys"]
        assert leading in (["RUN#*"], ["APPROVAL#*"]), st
        seen += 1
    assert seen


def test_task_callbacks_only_on_the_approval_handler(any_template: assertions.Template):
    approval_role = function_role_id(any_template, APPROVAL_HANDLER)
    found = False
    for logical_id, roles, st in _all_policy_statements(any_template):
        if not any(a.startswith("states:SendTask") for a in actions(st)):
            continue
        assert roles == {approval_role}, (logical_id, roles)
        assert st["Effect"] == "Allow"
        assert st["Resource"] == "*"
        found = True
    assert found
