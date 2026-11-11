import os
from datetime import UTC, datetime

import pytest

from shared.marking import (
    JANITOR_PROJECT_TAG,
    age_days,
    is_janitor_owned,
    is_kept,
    is_lab,
    is_lab_tagged,
)
from shared.settings import load_settings


@pytest.fixture
def settings():
    return load_settings({}, os.environ)


def test_prefix_is_case_sensitive(settings):
    assert is_lab("LAB-x", None, settings) and not is_lab("lab-x", None, settings)


def test_tag_marks_lab_but_not_candidate_helpers(settings):
    tags = [{"Key": "Purpose", "Value": "lab"}]
    assert is_lab("prod-db", tags, settings) and is_lab_tagged(tags, settings)
    assert not is_lab_tagged([{"Key": "Purpose", "Value": "Lab"}], settings)


def test_keep_tag(settings):
    assert is_kept([{"Key": "janitor", "Value": "keep"}], settings)


def test_age_days():
    now = datetime(2026, 11, 10, tzinfo=UTC)
    assert age_days(datetime(2026, 11, 1, tzinfo=UTC), now) == 9
    assert age_days(None, now) is None


def test_is_janitor_owned():
    key, value = JANITOR_PROJECT_TAG
    assert (key, value) == ("Project", "aws-lab-janitor")
    assert is_janitor_owned([{"Key": "Project", "Value": "aws-lab-janitor"}])
    assert is_janitor_owned([{"key": "Project", "value": "aws-lab-janitor"}])
    assert not is_janitor_owned([{"Key": "Project", "Value": "other"}])
    assert not is_janitor_owned([{"Key": "Purpose", "Value": "lab"}])
    assert not is_janitor_owned([])
    assert not is_janitor_owned(None)


def test_lowercase_tag_shape_is_read(settings):
    # Internet Monitor and a few other services return {"key", "value"} tags.
    assert is_lab_tagged([{"key": "Purpose", "value": "lab"}], settings)
    assert is_kept([{"key": "janitor", "value": "keep"}], settings)


@pytest.mark.parametrize(
    "tags",
    [
        None,
        [],
        [{"Key": "janitor", "Value": "Keep"}],
        [{"Key": "Janitor", "Value": "keep"}],
        [{"Key": "janitor", "Value": ""}],
        [{"Key": "Purpose", "Value": "lab"}],
    ],
)
def test_is_kept_negatives(settings, tags):
    # Only the exact key and value exempt a resource; a near miss must not.
    assert is_kept(tags, settings) is False
