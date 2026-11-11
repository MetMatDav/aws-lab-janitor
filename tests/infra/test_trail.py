"""The CloudTrail trail that records the audit trail's own data events."""

from aws_cdk import assertions

from tests.infra.helpers import render

TRAIL = "AWS::CloudTrail::Trail"
TRAIL_ARN = "arn:<AWS::Partition>:cloudtrail:us-east-1:123456789012:trail/LabJanitorStack-audit"
DATA_SELECTORS = ["Plan objects", "Audit table items"]


def _trail(template: assertions.Template) -> dict:
    (trail,) = template.find_resources(TRAIL).values()
    return trail


def _selectors(template: assertions.Template) -> dict[str, dict[str, dict]]:
    """``{selector name: {field: field selector}}`` of the trail."""
    return {
        selector["Name"]: {field["Field"]: field for field in selector["FieldSelectors"]}
        for selector in _trail(template)["Properties"]["AdvancedEventSelectors"]
    }


def _plans_bucket_id(template: assertions.Template) -> str:
    return template.to_json()["Outputs"]["PlansBucketName"]["Value"]["Ref"]


def _log_bucket_id(template: assertions.Template) -> str:
    return template.to_json()["Outputs"]["TrailLogBucketName"]["Value"]["Ref"]


def _log_bucket_policy(template: assertions.Template) -> tuple[str, dict]:
    log_bucket = _log_bucket_id(template)
    ((policy_id, policy),) = [
        (logical_id, resource)
        for logical_id, resource in template.find_resources("AWS::S3::BucketPolicy").items()
        if resource["Properties"]["Bucket"] == {"Ref": log_bucket}
    ]
    return policy_id, policy


def test_default_mode_is_a_multi_region_trail_with_management_and_data_events(
    template: assertions.Template,
):
    properties = _trail(template)["Properties"]

    for flag in (
        "IsMultiRegionTrail",
        "IncludeGlobalServiceEvents",
        "EnableLogFileValidation",
        "IsLogging",
    ):
        assert properties[flag] is True, flag
    assert properties["TrailName"] == "LabJanitorStack-audit"
    assert properties["S3BucketName"] == {"Ref": _log_bucket_id(template)}
    selectors = _selectors(template)
    assert list(selectors) == ["Management events", *DATA_SELECTORS]
    assert selectors["Management events"] == {
        "eventCategory": {"Field": "eventCategory", "Equals": ["Management"]}
    }
    assert "EventSelectors" not in properties


def test_data_events_only_logs_no_management_events(data_only_template: assertions.Template):
    properties = _trail(data_only_template)["Properties"]

    # An account with a trail already pays for its management events once.
    assert properties["IsMultiRegionTrail"] is False
    assert properties["IncludeGlobalServiceEvents"] is False
    selectors = _selectors(data_only_template)
    assert list(selectors) == DATA_SELECTORS
    for fields in selectors.values():
        assert fields["eventCategory"]["Equals"] == ["Data"]


def test_data_selectors_pin_the_plans_prefix_and_the_audit_table(
    data_only_template: assertions.Template,
):
    selectors = _selectors(data_only_template)
    plans_bucket = _plans_bucket_id(data_only_template)
    (table_id,) = data_only_template.find_resources("AWS::DynamoDB::GlobalTable")

    objects = selectors["Plan objects"]
    assert set(objects) == {"eventCategory", "resources.type", "resources.ARN"}
    assert objects["resources.type"]["Equals"] == ["AWS::S3::Object"]
    (prefix,) = objects["resources.ARN"]["StartsWith"]
    assert render(prefix) == f"<{plans_bucket}.Arn>/plans/"

    items = selectors["Audit table items"]
    assert set(items) == {"eventCategory", "resources.type", "resources.ARN"}
    assert items["resources.type"]["Equals"] == ["AWS::DynamoDB::Table"]
    assert items["resources.ARN"]["Equals"] == [{"Fn::GetAtt": [table_id, "Arn"]}]


def test_none_creates_no_trail_and_no_log_bucket(no_trail_template: assertions.Template):
    no_trail_template.resource_count_is(TRAIL, 0)
    no_trail_template.resource_count_is("AWS::S3::Bucket", 1)
    assert "TrailLogBucketName" not in no_trail_template.to_json()["Outputs"]


def test_log_bucket_is_private_versioned_and_retained(template: assertions.Template):
    bucket = template.find_resources("AWS::S3::Bucket")[_log_bucket_id(template)]
    properties = bucket["Properties"]

    assert properties["VersioningConfiguration"] == {"Status": "Enabled"}
    assert properties["PublicAccessBlockConfiguration"] == {
        "BlockPublicAcls": True,
        "BlockPublicPolicy": True,
        "IgnorePublicAcls": True,
        "RestrictPublicBuckets": True,
    }
    assert properties["OwnershipControls"] == {
        "Rules": [{"ObjectOwnership": "BucketOwnerEnforced"}]
    }
    (rule,) = properties["LifecycleConfiguration"]["Rules"]
    assert rule["Status"] == "Enabled"
    assert rule["ExpirationInDays"] == 400
    assert rule["NoncurrentVersionExpiration"] == {"NoncurrentDays": 30}
    assert bucket["DeletionPolicy"] == "Retain"
    assert bucket["UpdateReplacePolicy"] == "Retain"


def test_log_bucket_policy_allows_only_this_trail(template: assertions.Template):
    log_bucket = _log_bucket_id(template)
    _policy_id, policy = _log_bucket_policy(template)
    statements = policy["Properties"]["PolicyDocument"]["Statement"]
    allows = [st for st in statements if st["Effect"] == "Allow"]
    denies = [st for st in statements if st["Effect"] == "Deny"]

    # Everything else is the TLS-only Deny that enforce_ssl adds.
    assert len(denies) == 1
    assert denies[0]["Condition"] == {"Bool": {"aws:SecureTransport": "false"}}
    by_action = {st["Action"]: st for st in allows}
    assert set(by_action) == {"s3:GetBucketAcl", "s3:PutObject"}
    for statement in allows:
        assert statement["Principal"] == {"Service": "cloudtrail.amazonaws.com"}
        assert render(statement["Condition"]["StringEquals"]["aws:SourceArn"]) == TRAIL_ARN
    assert by_action["s3:GetBucketAcl"]["Resource"] == {"Fn::GetAtt": [log_bucket, "Arn"]}
    assert set(by_action["s3:GetBucketAcl"]["Condition"]["StringEquals"]) == {"aws:SourceArn"}
    put = by_action["s3:PutObject"]
    assert render(put["Resource"]) == f"<{log_bucket}.Arn>/AWSLogs/123456789012/*"
    assert put["Condition"]["StringEquals"]["s3:x-amz-acl"] == "bucket-owner-full-control"


def test_trail_waits_for_the_bucket_policy(template: assertions.Template):
    policy_id, _policy = _log_bucket_policy(template)

    # CloudTrail checks the bucket policy when the trail is created.
    assert policy_id in _trail(template)["DependsOn"]


def test_three_modes_synthesize_the_rest_of_the_stack(
    template: assertions.Template,
    data_only_template: assertions.Template,
    no_trail_template: assertions.Template,
):
    for each in (template, data_only_template, no_trail_template):
        assert "StateMachineArn" in each.to_json()["Outputs"]
        each.resource_count_is("AWS::CloudWatch::Alarm", 6)
