"""Pure helpers that decide whether a resource belongs to the lab."""

from datetime import datetime

from shared.settings import Settings

# Resources the janitor's own stack creates carry this tag; the janitor never reports them.
JANITOR_PROJECT_TAG = ("Project", "aws-lab-janitor")


def tag_value(tags: list[dict] | None, key: str) -> str | None:
    """Return the value of tag ``key``, or None.

    Accepts both ``{"Key", "Value"}`` and ``{"key", "value"}`` shapes.
    """
    for tag in tags or []:
        tag_key = tag.get("Key", tag.get("key"))
        if tag_key == key:
            return tag.get("Value", tag.get("value"))
    return None


def is_lab_tagged(tags: list[dict] | None, settings: Settings) -> bool:
    """True when the lab tag is present with the exact lab value."""
    return tag_value(tags, settings.tag_key) == settings.tag_value


def is_janitor_owned(tags: list[dict] | None) -> bool:
    """True when the resource carries the project tag of the janitor's own stack."""
    key, value = JANITOR_PROJECT_TAG
    return tag_value(tags, key) == value


def is_lab(name: str | None, tags: list[dict] | None, settings: Settings) -> bool:
    """True when the name has the lab prefix (case-sensitive) or the lab tag matches."""
    if name is not None and name.startswith(settings.name_prefix):
        return True
    return is_lab_tagged(tags, settings)


def is_kept(tags: list[dict] | None, settings: Settings) -> bool:
    """True when the resource carries the keep tag that exempts it from cleanup."""
    return tag_value(tags, settings.keep_tag_key) == settings.keep_tag_value


def age_days(timestamp: datetime | None, now: datetime) -> int | None:
    """Whole days elapsed between ``timestamp`` and ``now``, or None without a timestamp."""
    if timestamp is None:
        return None
    return (now - timestamp).days
