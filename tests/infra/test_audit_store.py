import aws_cdk as cdk
from aws_cdk import assertions
from aws_cdk import aws_iam as iam
from aws_cdk.assertions import Match

from lab_janitor.audit import AuditStore
from tests.infra.conftest import ENV
from tests.infra.helpers import NOTIFIER_HANDLER, actions, iam_statements, role_statements

ROOT_ARN = {"Fn::Join": ["", ["arn:", {"Ref": "AWS::Partition"}, ":iam::123456789012:root"]]}
TABLE_ARN = {"Fn::GetAtt": ["AuditTable18A3095C", "Arn"]}
APPROVAL_KEYS = {"ForAllValues:StringLike": {"dynamodb:LeadingKeys": ["APPROVAL#*"]}}
DYNAMODB_WRITES = {
    "dynamodb:PutItem",
    "dynamodb:UpdateItem",
    "dynamodb:DeleteItem",
    "dynamodb:BatchWriteItem",
}


def test_plans_bucket_policy_denies_delete_except_root(template: assertions.Template):
    policies = template.find_resources("AWS::S3::BucketPolicy")
    statements = [
        statement
        for policy in policies.values()
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]
    ]

    delete_denies = [
        statement
        for statement in statements
        if statement["Effect"] == "Deny"
        and statement.get("Principal") == "*"
        and sorted(statement["Action"]) == ["s3:DeleteObject", "s3:DeleteObjectVersion"]
    ]
    assert len(delete_denies) == 1
    (deny,) = delete_denies
    assert deny["Condition"] == {"StringNotEquals": {"aws:PrincipalArn": ROOT_ARN}}

    template.has_resource(
        "AWS::S3::Bucket",
        {
            "Properties": Match.object_like(
                {
                    "VersioningConfiguration": {"Status": "Enabled"},
                    "OwnershipControls": {"Rules": [{"ObjectOwnership": "BucketOwnerEnforced"}]},
                    "PublicAccessBlockConfiguration": {
                        "BlockPublicAcls": True,
                        "BlockPublicPolicy": True,
                        "IgnorePublicAcls": True,
                        "RestrictPublicBuckets": True,
                    },
                    "LifecycleConfiguration": {
                        "Rules": [
                            Match.object_like(
                                {
                                    "Prefix": "plans/",
                                    "ExpirationInDays": 400,
                                    "NoncurrentVersionExpiration": {"NoncurrentDays": 30},
                                    "Status": "Enabled",
                                }
                            )
                        ]
                    },
                }
            ),
            "DeletionPolicy": "Retain",
            "UpdateReplacePolicy": "Retain",
        },
    )


def test_audit_table_has_gsi_pitr_and_ttl(template: assertions.Template):
    tables = template.find_resources("AWS::DynamoDB::GlobalTable")
    assert len(tables) == 1
    (table,) = tables.values()
    props = table["Properties"]

    assert table["DeletionPolicy"] == "Retain"
    assert "TableName" not in props
    assert props["BillingMode"] == "PAY_PER_REQUEST"
    assert props["KeySchema"] == [
        {"AttributeName": "pk", "KeyType": "HASH"},
        {"AttributeName": "sk", "KeyType": "RANGE"},
    ]
    assert props["TimeToLiveSpecification"] == {"AttributeName": "ttl", "Enabled": True}
    assert props["GlobalSecondaryIndexes"] == [
        {
            "IndexName": "by-resource",
            "KeySchema": [
                {"AttributeName": "resourceId", "KeyType": "HASH"},
                {"AttributeName": "at", "KeyType": "RANGE"},
            ],
            "Projection": {"ProjectionType": "ALL"},
        }
    ]
    (replica,) = props["Replicas"]
    assert replica["PointInTimeRecoverySpecification"] == {"PointInTimeRecoveryEnabled": True}


def test_root_arn_uses_the_partition(template: assertions.Template):
    statements = [
        st
        for policy in template.find_resources("AWS::S3::BucketPolicy").values()
        for st in policy["Properties"]["PolicyDocument"]["Statement"]
    ]
    root_conditions = [
        st["Condition"]["StringNotEquals"]["aws:PrincipalArn"]
        for st in statements
        if "s3:DeleteObject" in actions(st)
    ]
    assert root_conditions == [ROOT_ARN]
    assert "arn:aws:" not in str(template.to_json())


def test_approval_grants_carry_leading_keys(template: assertions.Template):
    statements = role_statements(template, NOTIFIER_HANDLER)
    writes = [st for st in statements if DYNAMODB_WRITES & set(actions(st))]
    assert len(writes) == 1
    (write,) = writes
    assert write["Effect"] == "Allow"
    assert sorted(actions(write)) == ["dynamodb:PutItem", "dynamodb:UpdateItem"]
    assert write["Resource"] == TABLE_ARN
    assert write["Condition"] == APPROVAL_KEYS


def test_every_dynamodb_write_grant_in_the_stack_carries_leading_keys(
    template: assertions.Template,
):
    writes = [
        st
        for st in iam_statements(template)
        if st["Effect"] == "Allow" and DYNAMODB_WRITES & set(actions(st))
    ]
    assert len(writes) >= 2  # detector RUN#* and notifier APPROVAL#*
    for st in writes:
        assert set(st["Condition"]) == {"ForAllValues:StringLike"}, st
        assert st["Condition"]["ForAllValues:StringLike"]["dynamodb:LeadingKeys"], st


def test_grant_helpers_for_later_components():
    app = cdk.App()
    stack = cdk.Stack(app, "Bare", env=ENV)
    audit = AuditStore(stack, "Audit")
    role = iam.Role(stack, "Role", assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"))
    audit.grant_approval_reads(role)
    audit.grant_approval_updates(role)
    audit.grant_plan_reads(role)

    statements = iam_statements(assertions.Template.from_stack(stack))
    by_actions = {tuple(sorted(actions(st))): st for st in statements}
    assert set(by_actions) == {
        ("dynamodb:GetItem",),
        ("dynamodb:UpdateItem",),
        ("s3:GetObject", "s3:GetObjectVersion"),
    }
    for key in [("dynamodb:GetItem",), ("dynamodb:UpdateItem",)]:
        assert by_actions[key]["Condition"] == APPROVAL_KEYS
    plan_read = by_actions[("s3:GetObject", "s3:GetObjectVersion")]
    assert plan_read["Resource"]["Fn::Join"][1][1] == "/plans/*"
    assert "Condition" not in plan_read
