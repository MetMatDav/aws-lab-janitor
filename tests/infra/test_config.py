import aws_cdk as cdk
import pytest
from aws_cdk import assertions

from lab_janitor.config import JanitorConfig


def test_missing_report_email_fails_with_hint(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("JANITOR_REPORT_EMAIL", raising=False)
    app = cdk.App()

    with pytest.raises(ValueError, match="-c reportEmail"):
        JanitorConfig.from_node(app.node)


def test_no_output_contains_email(template: assertions.Template):
    outputs = template.to_json().get("Outputs", {})

    for name, output in outputs.items():
        assert "@" not in str(output.get("Value")), f"output {name} leaks an email address"


def _config(**context: object) -> JanitorConfig:
    app = cdk.App(context={"reportEmail": "test@example.com", **context})
    return JanitorConfig.from_node(app.node)


@pytest.mark.parametrize(("value", "dry_run"), [("false", False), ("False", False), (False, False)])
def test_explicit_false_disables_dry_run(value: object, dry_run: bool):
    assert _config(dryRun=value).dry_run is dry_run


@pytest.mark.parametrize("value", ["no", 0, "FALSE"])
def test_other_falsy_looking_values_keep_dry_run(value: object):
    assert _config(dryRun=value).dry_run is True


@pytest.mark.parametrize(
    ("value", "expected"), [("false", False), ("False", False), (False, False)]
)
def test_reserved_concurrency_flag(value: object, expected: bool):
    assert _config(reservedConcurrency=value).reserved_concurrency is expected
    assert _config().reserved_concurrency is True


def test_integer_settings_accept_cli_strings():
    config = _config(minAgeDays="0", scheduleHour="23")
    assert (config.min_age_days, config.schedule_hour) == (0, 23)


@pytest.mark.parametrize("value", [-1, "-1", "seven", 1.5, True, "2.5"])
def test_invalid_min_age_days_is_rejected(value: object):
    with pytest.raises(ValueError, match="minAgeDays must be an integer >= 0"):
        _config(minAgeDays=value)


@pytest.mark.parametrize("value", [-1, 24, "24", "nine", 9.5, False])
def test_invalid_schedule_hour_is_rejected(value: object):
    with pytest.raises(ValueError, match=r"scheduleHour must be an integer in 0\.\.23"):
        _config(scheduleHour=value)


ROLE_ARN = "arn:aws:iam::111122223333:role/LabJanitorApprover"


@pytest.mark.parametrize("value", ["link", "iam", "both"])
def test_approval_auth_accepts_the_three_modes(value: str):
    context = {"approvalAuth": value}
    if value != "link":
        context["approverRoleArn"] = ROLE_ARN
    assert _config(**context).approval_auth == value


def test_approval_auth_defaults_to_link_without_a_role():
    config = _config()
    assert (config.approval_auth, config.approver_role_arn) == ("link", None)


@pytest.mark.parametrize("value", ["LINK", "sso", "", 1])
def test_approval_auth_rejects_other_values(value: object):
    with pytest.raises(ValueError, match="approvalAuth"):
        _config(approvalAuth=value, approverRoleArn=ROLE_ARN)


@pytest.mark.parametrize("mode", ["iam", "both"])
def test_iam_modes_require_a_valid_approver_role_arn(mode: str):
    with pytest.raises(ValueError, match="approverRoleArn"):
        _config(approvalAuth=mode)
    for bad in ["not-an-arn", "arn:aws:iam::1234:role/x", "arn:aws:iam::111122223333:user/x"]:
        with pytest.raises(ValueError, match="approverRoleArn"):
            _config(approvalAuth=mode, approverRoleArn=bad)
    gov = "arn:aws-us-gov:iam::111122223333:role/path/Approver"
    assert _config(approvalAuth=mode, approverRoleArn=gov).approver_role_arn == gov


def test_allowed_regions_from_comma_string_and_list():
    assert _config(allowedRegions="us-east-1, eu-west-1").allowed_regions == (
        "us-east-1",
        "eu-west-1",
    )
    assert _config(allowedRegions=["us-gov-west-1", " ap-southeast-2 "]).allowed_regions == (
        "us-gov-west-1",
        "ap-southeast-2",
    )
    assert _config(allowedRegions="us-east-1,,").allowed_regions == ("us-east-1",)
    assert _config(allowedRegions=[]).allowed_regions == ()
    assert _config(allowedRegions="").allowed_regions == ()
    assert _config().allowed_regions == ()


@pytest.mark.parametrize("value", ["us-east", "US-EAST-1", "us-east-1;eu-west-1", ["us_east_1"], 7])
def test_allowed_regions_rejects_malformed_entries(value: object):
    with pytest.raises(ValueError, match="allowedRegions"):
        _config(allowedRegions=value)


def test_approval_email_defaults_to_report_email():
    assert _config().approval_email == "test@example.com"
    assert _config(approvalEmail="approver@example.com").approval_email == "approver@example.com"
