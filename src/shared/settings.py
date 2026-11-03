"""Runtime settings for the janitor Lambdas."""

from collections.abc import Mapping
from dataclasses import dataclass

MIN_PREFIX_LENGTH = 3
DEFAULT_MIN_AGE_DAYS = "7"
DEFAULT_REGION = "us-east-1"


@dataclass(frozen=True)
class Settings:
    """Immutable janitor configuration."""

    dry_run: bool
    min_age_days: int
    name_prefix: str
    tag_key: str
    tag_value: str
    keep_tag_key: str
    keep_tag_value: str
    plans_bucket: str
    audit_table: str
    reports_topic_arn: str
    region: str
    # Only the executor reads it; any value other than the exact string "true" keeps it off.
    deletions_enabled: bool = False


def _env_min_age_days(env: Mapping[str, str]) -> int:
    """``MIN_AGE_DAYS`` from the environment; a non-integer or negative value is an error."""
    raw = env.get("MIN_AGE_DAYS", DEFAULT_MIN_AGE_DAYS)
    try:
        value = int(raw)
    except ValueError:
        value = -1
    if value < 0:
        raise ValueError(f"MIN_AGE_DAYS must be an integer >= 0, got {raw!r}")
    return value


def load_settings(event: dict, env: Mapping[str, str]) -> Settings:
    """Build Settings from the invocation event and the environment.

    Dry-run is the safe default: it is disabled only when the event carries the
    boolean ``config.dryRun = False``. A string such as ``"false"`` keeps it on.
    """
    config = event.get("config") or {}

    dry_run = config.get("dryRun") is not False

    min_age = config.get("minAgeDays")
    if isinstance(min_age, int) and not isinstance(min_age, bool) and min_age >= 0:
        min_age_days = min_age
    else:
        min_age_days = _env_min_age_days(env)

    markers = {
        name: env.get(name, "")
        for name in (
            "LAB_NAME_PREFIX",
            "LAB_TAG_KEY",
            "LAB_TAG_VALUE",
            "KEEP_TAG_KEY",
            "KEEP_TAG_VALUE",
        )
    }
    for name, value in markers.items():
        if not value:
            raise ValueError(f"{name} must not be empty")
    if len(markers["LAB_NAME_PREFIX"]) < MIN_PREFIX_LENGTH:
        raise ValueError(f"LAB_NAME_PREFIX must be at least {MIN_PREFIX_LENGTH} characters")

    return Settings(
        dry_run=dry_run,
        min_age_days=min_age_days,
        name_prefix=markers["LAB_NAME_PREFIX"],
        tag_key=markers["LAB_TAG_KEY"],
        tag_value=markers["LAB_TAG_VALUE"],
        keep_tag_key=markers["KEEP_TAG_KEY"],
        keep_tag_value=markers["KEEP_TAG_VALUE"],
        plans_bucket=env.get("PLANS_BUCKET", ""),
        audit_table=env.get("AUDIT_TABLE", ""),
        reports_topic_arn=env.get("REPORTS_TOPIC_ARN", ""),
        region=env.get("AWS_REGION", DEFAULT_REGION),
        deletions_enabled=env.get("DELETIONS_ENABLED") == "true",
    )
