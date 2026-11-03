from aws_cdk import assertions

from tests.infra.helpers import (
    DETECTOR_HANDLER,
    actions,
    detector_statements,
    function_role_id,
    iam_statements,
    resources,
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
