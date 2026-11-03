import os
from datetime import UTC, datetime

import pytest

from shared.marking import age_days, is_kept, is_lab, is_lab_tagged
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
