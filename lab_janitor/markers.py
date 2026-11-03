"""Markers that identify lab resources and the opt-out tag.

These values are the single source of truth on the CDK side; the Lambda
functions receive them through ``lambda_env()`` and read them back in
``shared.settings``.
"""

LAB_NAME_PREFIX = "LAB-"
LAB_TAG_KEY = "Purpose"
LAB_TAG_VALUE = "lab"
KEEP_TAG_KEY = "janitor"
KEEP_TAG_VALUE = "keep"


def lambda_env() -> dict[str, str]:
    """Return the marker environment variables every janitor Lambda needs."""
    return {
        "LAB_NAME_PREFIX": LAB_NAME_PREFIX,
        "LAB_TAG_KEY": LAB_TAG_KEY,
        "LAB_TAG_VALUE": LAB_TAG_VALUE,
        "KEEP_TAG_KEY": KEEP_TAG_KEY,
        "KEEP_TAG_VALUE": KEEP_TAG_VALUE,
    }
