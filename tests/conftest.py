import os

import pytest
from hypothesis import settings as hypothesis_settings

# Property tests (tests/property): no per-example deadline, because the first
# example pays for imports. "ci" is derandomized so a red build can be reproduced.
hypothesis_settings.register_profile("dev", deadline=None, max_examples=100)
hypothesis_settings.register_profile("ci", deadline=None, max_examples=300, derandomize=True)
hypothesis_settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "dev"))


@pytest.fixture(scope="session", autouse=True)
def _aws_and_janitor_env():
    """Set the environment variables every handler test needs."""
    values = {
        "AWS_DEFAULT_REGION": "us-east-1",
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "LAB_NAME_PREFIX": "LAB-",
        "LAB_TAG_KEY": "Purpose",
        "LAB_TAG_VALUE": "lab",
        "KEEP_TAG_KEY": "janitor",
        "KEEP_TAG_VALUE": "keep",
        "PLANS_BUCKET": "test-plans",
        "AUDIT_TABLE": "test-audit",
        "REPORTS_TOPIC_ARN": "arn:aws:sns:us-east-1:123456789012:reports",
        "MIN_AGE_DAYS": "7",
    }
    with pytest.MonkeyPatch.context() as mp:
        for key, value in values.items():
            mp.setenv(key, value)
        yield
