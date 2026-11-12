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


@pytest.mark.parametrize("key", ["dryRun", "janitorEnabled", "reservedConcurrency"])
@pytest.mark.parametrize("value", ["no", 0, 1, "FALSE", "TRUE", "yes", " false", "", "0"])
def test_a_flag_that_is_not_true_or_false_fails_the_synth(key: str, value: object):
    # Guessing either way is wrong for one of them: a typo in dryRun must not
    # arm deletions, a typo in janitorEnabled must not leave the schedule on.
    with pytest.raises(ValueError, match=f"{key} must be true or false"):
        _config(**{key: value})


@pytest.mark.parametrize("value", ["true", "True", True])
def test_flags_accept_the_explicit_true_forms(value: object):
    config = _config(dryRun=value, janitorEnabled=value, reservedConcurrency=value)
    assert (config.dry_run, config.janitor_enabled, config.reserved_concurrency) == (
        True,
        True,
        True,
    )


@pytest.mark.parametrize("value", ["false", "False", False])
def test_explicit_false_pauses_the_janitor(value: object):
    assert _config(janitorEnabled=value).janitor_enabled is False


@pytest.mark.parametrize(
    ("value", "expected"), [("false", False), ("False", False), (False, False)]
)
def test_reserved_concurrency_flag(value: object, expected: bool):
    assert _config(reservedConcurrency=value).reserved_concurrency is expected
    assert _config().reserved_concurrency is True


def test_integer_settings_accept_cli_strings():
    config = _config(minAgeDays="0", scheduleHour="23")
    assert (config.min_age_days, config.schedule_hour) == (0, 23)


@pytest.mark.parametrize("value", [-1, "-1", "seven", 1.5, True, "2.5", 3651, "3651"])
def test_invalid_min_age_days_is_rejected(value: object):
    with pytest.raises(ValueError, match="minAgeDays must be an integer in 0..3650"):
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


@pytest.mark.parametrize(
    "value",
    [
        "arn:aws:iam::111122223333:role/*",
        "arn:aws:iam::111122223333:role/a b",
        "arn:aws:iam::111122223333:role/",
        "arn:aws:iam::111122223333:role/path/",
        "arn:aws-:iam::111122223333:role/Approver",
        "arn:aws:iam::111122223333:role/Approver\r",
        "arn:aws:iam::111122223333:role/Approver?",
    ],
)
def test_approver_role_arn_names_one_role_with_iam_characters_only(value: str):
    with pytest.raises(ValueError, match="approverRoleArn"):
        _config(approvalAuth="iam", approverRoleArn=value)


@pytest.mark.parametrize(
    "value",
    [
        "arn:aws:iam::111122223333:role/Approver",
        "arn:aws:iam::111122223333:role/team/sub/Approver_1+=,.@-x",
        "arn:aws-cn:iam::111122223333:role/Approver",
    ],
)
def test_approver_role_arn_accepts_paths_and_every_iam_name_character(value: str):
    assert _config(approvalAuth="iam", approverRoleArn=value).approver_role_arn == value


def test_allowed_regions_from_comma_string_and_list():
    assert _config(allowedRegions="us-east-1, eu-west-1").allowed_regions == (
        "us-east-1",
        "eu-west-1",
    )
    assert _config(allowedRegions=["us-east-1", " ap-southeast-2 "]).allowed_regions == (
        "us-east-1",
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


@pytest.mark.parametrize("key", ["reportEmail", "alertEmail", "approvalEmail"])
@pytest.mark.parametrize("value", ["TU_CORREO", "you@example", "@example.com", "a b@example.com"])
def test_placeholder_or_malformed_email_is_rejected_at_synth(key: str, value: str):
    """A placeholder left in the deploy command must fail at synth, not in CloudFormation.

    SNS rejects a malformed subscription endpoint only at deploy time, which
    costs a full stack rollback.
    """
    context = {"reportEmail": "test@example.com", key: value}

    with pytest.raises(ValueError, match=f"{key} must be an email address"):
        JanitorConfig.from_node(cdk.App(context=context).node)


@pytest.mark.parametrize("value", ["full", "dataEventsOnly", "none"])
def test_create_trail_accepts_the_three_modes(value: str):
    assert _config(createTrail=value).create_trail == value


def test_create_trail_defaults_to_full():
    assert _config().create_trail == "full"


@pytest.mark.parametrize("value", ["FULL", "data", "", True, 1])
def test_create_trail_rejects_other_values(value: object):
    with pytest.raises(ValueError, match="createTrail must be one of full, dataEventsOnly, none"):
        _config(createTrail=value)


def test_allowed_regions_must_include_us_east_1():
    with pytest.raises(ValueError, match="allowedRegions must include us-east-1"):
        _config(allowedRegions="eu-west-1,us-west-2")
    assert _config(allowedRegions="eu-west-1,us-east-1").allowed_regions == (
        "eu-west-1",
        "us-east-1",
    )


@pytest.mark.parametrize(
    "value",
    ["a,b@example.com", "<a>@example.com", '"a"@example.com', "a@example..com", "a@.example.com"],
)
def test_email_shapes_sns_would_refuse_at_deploy_fail_the_synth(value: str):
    with pytest.raises(ValueError, match="reportEmail must be an email address"):
        _config(reportEmail=value)


@pytest.mark.parametrize(
    "value", ["first.last+tag@example.com", "a_b%c-d@mail.example.com", "A1@EXAMPLE.COM"]
)
def test_ordinary_email_shapes_are_accepted(value: str):
    assert _config(reportEmail=value).report_email == value


@pytest.mark.parametrize("value", ["a@example.com\n", "a@exämple.com", "a b@example.com"])
def test_email_pattern_is_ascii_and_anchored(value: str):
    with pytest.raises(ValueError, match="reportEmail must be an email address"):
        _config(reportEmail=value)


def test_region_pattern_is_ascii_and_anchored():
    # An Arabic-Indic digit matches a bare \d; region names are ASCII.
    with pytest.raises(ValueError, match="invalid region name"):
        _config(allowedRegions="us-east-1,eu-west-\u0661")
