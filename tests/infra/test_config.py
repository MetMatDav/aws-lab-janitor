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
