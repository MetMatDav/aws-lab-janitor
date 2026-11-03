import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _run_app(**env: str) -> subprocess.CompletedProcess:
    clean = {k: v for k, v in os.environ.items() if not k.startswith("CDK_")}
    return subprocess.run(
        [sys.executable, "app.py"],
        cwd=ROOT,
        env={**clean, **env, "CDK_CONTEXT_JSON": '{"reportEmail": "test@example.com"}'},
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )


@pytest.mark.parametrize(
    ("env", "missing"),
    [
        ({"CDK_DEFAULT_REGION": "us-east-1"}, "CDK_DEFAULT_ACCOUNT"),
        ({"CDK_DEFAULT_ACCOUNT": "123456789012"}, "CDK_DEFAULT_REGION"),
    ],
)
def test_app_requires_account_and_region(env: dict[str, str], missing: str):
    result = _run_app(**env)
    assert result.returncode != 0
    assert missing in result.stderr
    present = next(iter(env))
    assert f"{present} is not set" not in result.stderr
