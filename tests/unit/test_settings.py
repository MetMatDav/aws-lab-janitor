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


@pytest.mark.parametrize("value", [3651, 800_000, 10**30])
def test_min_age_beyond_the_cap_in_the_event_falls_back_to_the_deployed_value(value):
    assert load_settings({"config": {"minAgeDays": value}}, os.environ).min_age_days == 7


def test_min_age_at_the_cap_is_honoured():
    assert load_settings({"config": {"minAgeDays": 3650}}, os.environ).min_age_days == 3650


@pytest.mark.parametrize("value", ["-1", "seven", "\u0663", "1_0", " 7", "7\n", "+7", "3651"])
def test_invalid_env_min_age_is_rejected(value):
    env = dict(os.environ, MIN_AGE_DAYS=value)
    with pytest.raises(ValueError, match="MIN_AGE_DAYS must be an integer in 0..3650"):
        load_settings({}, env)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("true", True), ("True", False), ("1", False), ("", False), (None, False)],
)
def test_deletions_enabled_requires_exact_true(raw, expected):
    env = {k: v for k, v in os.environ.items() if k != "DELETIONS_ENABLED"}
    if raw is not None:
        env["DELETIONS_ENABLED"] = raw
    assert load_settings({}, env).deletions_enabled is expected


def test_prefix_shorter_than_three_is_rejected():
    # A one or two letter prefix would match half the account.
    env = dict(os.environ, LAB_NAME_PREFIX="LA")
    with pytest.raises(ValueError, match="LAB_NAME_PREFIX must be at least 3 characters"):
        load_settings({}, env)
    assert load_settings({}, dict(os.environ, LAB_NAME_PREFIX="LA-")).name_prefix == "LA-"


@pytest.mark.parametrize("name", ["LAB_TAG_KEY", "LAB_TAG_VALUE", "KEEP_TAG_KEY", "KEEP_TAG_VALUE"])
def test_each_empty_marker_is_rejected(name):
    # An empty marker would match resources that have no such tag at all.
    with pytest.raises(ValueError, match=f"{name} must not be empty"):
        load_settings({}, dict(os.environ, **{name: ""}))


def test_region_falls_back_to_us_east_1():
    env = {key: value for key, value in os.environ.items() if key != "AWS_REGION"}
    assert load_settings({}, env).region == "us-east-1"
    assert load_settings({}, dict(env, AWS_REGION="eu-west-1")).region == "eu-west-1"
