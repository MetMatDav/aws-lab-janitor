import os

import pytest

from shared.settings import load_settings


def test_defaults_to_dry_run_without_config():
    assert load_settings({}, os.environ).dry_run is True


def test_string_false_stays_dry_run():
    assert load_settings({"config": {"dryRun": "false"}}, os.environ).dry_run is True


def test_boolean_false_disables_dry_run():
    assert load_settings({"config": {"dryRun": False}}, os.environ).dry_run is False


def test_min_age_from_config_overrides_env():
    assert load_settings({"config": {"minAgeDays": 0}}, os.environ).min_age_days == 0
    assert load_settings({}, os.environ).min_age_days == 7


def test_empty_prefix_is_rejected():
    env = dict(os.environ, LAB_NAME_PREFIX="")
    with pytest.raises(ValueError, match="LAB_NAME_PREFIX"):
        load_settings({}, env)


def test_missing_optional_env_does_not_raise():
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("REPORTS_TOPIC_ARN", "PLANS_BUCKET", "AUDIT_TABLE")
    }
    assert load_settings({}, env).reports_topic_arn == ""


def test_boolean_min_age_is_ignored():
    assert load_settings({"config": {"minAgeDays": True}}, os.environ).min_age_days == 7


@pytest.mark.parametrize("value", ["-1", "seven"])
def test_invalid_env_min_age_is_rejected(value):
    env = dict(os.environ, MIN_AGE_DAYS=value)
    with pytest.raises(ValueError, match="MIN_AGE_DAYS must be an integer >= 0"):
        load_settings({}, env)
