"""Surface 5: configuration and state-machine input.

``lab_janitor.config`` validates ``cdk -c`` values (an operator's typo is the threat
there); ``shared.settings`` reads the execution input that ``Normalize`` merges over the
defaults (foothold (c): anybody who can start an execution chooses every key of it).
"""

import os
from datetime import UTC, datetime

import pytest

from detector import handler as detector_handler
from lab_janitor.config import JanitorConfig
from shared.settings import load_settings


class Node:
    """Stands in for a construct node: ``JanitorConfig`` only reads context from it."""

    def __init__(self, **context) -> None:
        self._context = {"reportEmail": "owner@example.com", **context}

    def try_get_context(self, key: str):
        return self._context.get(key)


def config(**context) -> JanitorConfig:
    return JanitorConfig.from_node(Node(**context))


# --- cdk context -----------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["FALSE", "0", 0, "no", "off", " false", "false ", "disabled"])
def test_pause_switch_is_never_silently_ignored(value):
    """Foothold: none, an operator typo. ``_flag`` keeps a flag on for anything it does not
    recognise, which is the safe side for ``dryRun`` and the unsafe one for a pause switch."""
    try:
        enabled = config(janitorEnabled=value).janitor_enabled
    except ValueError:
        return

    assert enabled is False


@pytest.mark.parametrize("value", ["FALSE", "0", 0, "no", "off", " false", "false ", "f", ""])
def test_no_unrecognised_spelling_of_false_arms_a_deployment(value):
    """The same rule for ``dryRun``: a typo fails the synth, it never arms a deployment."""
    with pytest.raises(ValueError, match="dryRun must be true or false"):
        config(dryRun=value)


@pytest.mark.parametrize("key", ["minAgeDays", "scheduleHour"])
@pytest.mark.parametrize("value", ["\u0667", "\uff17", "1_0", "7\n", "\t7"], ids=ascii)
def test_integer_settings_accept_only_plain_ascii_digits(key, value):
    with pytest.raises(ValueError, match=key):
        config(**{key: value})


@pytest.mark.parametrize("value", [800_000, "1000000", 10**12])
def test_min_age_days_beyond_the_calendar_is_rejected_at_synth(value):
    with pytest.raises(ValueError, match="minAgeDays"):
        config(minAgeDays=value)


class _Sts:
    @staticmethod
    def get_caller_identity() -> dict:
        return {"Account": "123456789012"}


class _Clients:
    @staticmethod
    def get(service: str, region: str) -> _Sts:
        return _Sts()


@pytest.mark.parametrize("value", [800_000, 10**7, 10**30])
def test_huge_min_age_days_in_the_execution_input_does_not_crash_the_detector(value):
    """Foothold (c). Fail-safe (nothing is deleted), but the run dies before the sweep."""
    settings = load_settings({"config": {"minAgeDays": value}}, os.environ)

    detector_handler.start_run(settings, _Clients(), lambda _resource_id, _before: False, None)


@pytest.mark.parametrize(
    "value",
    [
        "a@example.com,b@example.com",
        "a@example.com, b@example.com",
        "a@example.com;b@example.com",
        "a@example.com b@example.com",
        "a@example.com\nb@example.com",
        "a@example.com\r\nBcc: b@example.com",
        "a@example.com\x00",
        "a@example.com\t",
        " a@example.com",
        "a@@example.com",
        "a@example.com@example.net",
        "\uff41@example.com",
        "a@ex\u0430mple.com",
        ["a@example.com", "b@example.com"],
        {"email": "a@example.com"},
    ],
    ids=ascii,
)
@pytest.mark.parametrize("key", ["reportEmail", "alertEmail", "approvalEmail"])
def test_an_email_setting_can_never_name_a_second_recipient(key, value):
    """The approval email is the authority to approve: one address, ASCII, nothing after it."""
    with pytest.raises(ValueError, match=f"{key} must be an email address"):
        config(**{key: value})


@pytest.mark.parametrize(
    "value",
    [
        "*",
        "us-*-1",
        "??-east-1",
        "us-east-*",
        "us-east-1*",
        "us-east-1,*",
        "us-east-1 eu-west-1",
        "us-east-1\neu-west-1",
        "us-east-1/../eu-west-1",
        "us-east-\u0661",
        "us-east-1,us-east-\uff11",
        "global",
        ["us-east-1", ["eu-west-1"]],
        ["us-east-1", None],
        {"us-east-1": True},
    ],
    ids=ascii,
)
def test_allowed_regions_cannot_smuggle_a_wildcard_or_a_second_value(value):
    """The list becomes an ``aws:RequestedRegion`` condition of the delete grants."""
    with pytest.raises(ValueError, match="allowedRegions"):
        config(allowedRegions=value)


def test_allowed_regions_keeps_the_lock_when_given_duplicates():
    assert set(config(allowedRegions="us-east-1,us-east-1,eu-west-1").allowed_regions) == {
        "us-east-1",
        "eu-west-1",
    }


@pytest.mark.parametrize(
    "value",
    [
        "arn:aws:iam::123456789012:role/Approver\n",
        "arn:aws:iam::123456789012:role/Approver\nother",
        "arn:aws:iam::12345678901:role/Approver",
        "arn:aws:iam::1234567890123:role/Approver",
        "arn:aws:iam::\uff11\uff12\uff13\uff14\uff15\uff16\uff17\uff18\uff19\uff10\uff11\uff12:role/Approver",
        "arn:aws:iam::123456789012:user/Approver",
        "arn:aws:iam::123456789012:role/",
        "arn:aws:sts::123456789012:assumed-role/Approver/session",
        "xarn:aws:iam::123456789012:role/Approver",
        "*",
        "",
        123456789012,
    ],
    ids=ascii,
)
def test_approver_role_arn_must_be_one_whole_role_arn(value):
    with pytest.raises(ValueError, match="approv"):
        config(approvalAuth="iam", approverRoleArn=value)


# --- execution input -------------------------------------------------------------------------


def from_input(**keys):
    env = {key: value for key, value in os.environ.items() if key != "DELETIONS_ENABLED"}
    return load_settings({"config": keys}, env)


def test_execution_input_cannot_reach_anything_but_dry_run_and_min_age():
    """Foothold (c): every extra key ``Normalize`` lets through is ignored by the Lambdas."""
    baseline = from_input()
    hostile = from_input(
        deletionsEnabled=True,
        deletions_enabled=True,
        DELETIONS_ENABLED="true",
        LAB_TAG_KEY="Name",
        LAB_TAG_VALUE="prod",
        LAB_NAME_PREFIX="",
        KEEP_TAG_KEY="never-set",
        tagKey="Name",
        namePrefix="",
        keepTag="x=y",
        PLANS_BUCKET="attacker-bucket",
        plansBucket="attacker-bucket",
        AUDIT_TABLE="attacker-table",
        region="eu-west-1",
        AWS_REGION="eu-west-1",
        config={"dryRun": False},
        detect={"planSha256": "0" * 64},
        approval={"decision": "approved"},
        approvalId="forged",
        settings={"deletions_enabled": True},
    )

    assert hostile == baseline
    assert hostile.dry_run is True
    assert hostile.deletions_enabled is False


@pytest.mark.parametrize(
    "value",
    [-1, -(10**9), "0", "", " 0", 0.0, 0.5, 1e3, float("nan"), True, False, None, [0], {"v": 0}],
    ids=repr,
)
def test_min_age_days_of_the_wrong_type_or_sign_falls_back_to_the_deployed_value(value):
    """``minAgeDays: -1``, a float or a nested object must not lower the minimum age."""
    assert from_input(minAgeDays=value).min_age_days == int(os.environ["MIN_AGE_DAYS"])


@pytest.mark.parametrize(
    "value",
    ["false", "False", 0, 0.0, "", None, [], {}, [False], {"dryRun": False}, "no"],
    ids=repr,
)
def test_dry_run_of_the_wrong_type_stays_dry(value):
    assert from_input(dryRun=value).dry_run is True


@pytest.mark.parametrize(
    "event", [{}, {"config": None}, {"config": {}}, {"config": []}, {"config": ""}]
)
def test_missing_or_empty_config_is_a_dry_run(event):
    settings = load_settings(event, os.environ)

    assert settings.dry_run is True
    assert settings.min_age_days == int(os.environ["MIN_AGE_DAYS"])


def test_deployed_markers_cannot_be_blanked_through_the_environment_either():
    """An empty lab tag value would make every untagged resource 'lab': refused at load."""
    for name in (
        "LAB_TAG_KEY",
        "LAB_TAG_VALUE",
        "KEEP_TAG_KEY",
        "KEEP_TAG_VALUE",
        "LAB_NAME_PREFIX",
    ):
        with pytest.raises(ValueError, match=name):
            load_settings({}, dict(os.environ, **{name: ""}))
    for prefix in ("L", "LA", "  "):
        with pytest.raises(ValueError, match="LAB_NAME_PREFIX"):
            load_settings({}, dict(os.environ, LAB_NAME_PREFIX=prefix))


def test_now_is_always_timezone_aware_in_the_detector():
    """A naive ``now`` would make every age comparison raise or drift by the UTC offset."""
    _run_id, ctx = detector_handler.start_run(
        load_settings({}, os.environ), _Clients(), lambda _resource_id, _before: False, None
    )

    assert ctx.now.tzinfo is UTC
    assert abs((datetime.now(UTC) - ctx.now).total_seconds()) < 5
