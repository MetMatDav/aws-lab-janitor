"""Snapshot of the synthesized template.

Regenerate after an intended change with
``UPDATE_SNAPSHOTS=1 pytest tests/infra/test_snapshot.py`` and review the diff.
"""

import json
import os
import re
from pathlib import Path

from aws_cdk import assertions

SNAPSHOT = Path(__file__).resolve().parents[1] / "snapshots" / "LabJanitorStack.template.json"
ASSET_KEY = re.compile(r'("S3Key": ")[0-9a-f]{64}\.zip(")')


def _normalized(template: assertions.Template) -> str:
    """The template as stable JSON, with content-hashed asset keys replaced."""
    rendered = json.dumps(template.to_json(), indent=2, sort_keys=True)
    return ASSET_KEY.sub(r"\1ASSET.zip\2", rendered) + "\n"


def test_template_snapshot(template: assertions.Template):
    current = _normalized(template)
    if os.environ.get("UPDATE_SNAPSHOTS") == "1":
        SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
        SNAPSHOT.write_text(current)
    assert SNAPSHOT.exists(), "no snapshot yet: run with UPDATE_SNAPSHOTS=1"
    assert json.loads(current) == json.loads(SNAPSHOT.read_text())
