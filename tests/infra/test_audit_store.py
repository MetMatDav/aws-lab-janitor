from aws_cdk import assertions
from aws_cdk.assertions import Match


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
    principal_arn = deny["Condition"]["StringNotEquals"]["aws:PrincipalArn"]
    assert principal_arn == "arn:aws:iam::123456789012:root"
    assert principal_arn.endswith(":root")

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
