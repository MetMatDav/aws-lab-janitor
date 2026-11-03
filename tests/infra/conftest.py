import json
import py_compile
from pathlib import Path

import aws_cdk as cdk
import pytest
from aws_cdk import assertions

from lab_janitor.stack import LabJanitorStack

ENV = cdk.Environment(account="123456789012", region="us-east-1")
ROOT = Path(__file__).resolve().parents[2]
SOURCE_DIR = ROOT / "src"


@pytest.fixture(scope="session")
def stack() -> LabJanitorStack:
    """Build the janitor stack once per session (jsii synthesis is slow)."""
    app = cdk.App(
        context={"reportEmail": "test@example.com", "approvalEmail": "approver@example.com"}
    )
    return LabJanitorStack(app, "LabJanitorStack", env=ENV)


@pytest.fixture(scope="session")
def template(stack: LabJanitorStack) -> assertions.Template:
    """Synthesized CloudFormation template of the janitor stack."""
    return assertions.Template.from_stack(stack)


@pytest.fixture(scope="session")
def no_reserve_template() -> assertions.Template:
    """Template synthesized with ``reservedConcurrency=false``."""
    app = cdk.App(context={"reportEmail": "test@example.com", "reservedConcurrency": "false"})
    return assertions.Template.from_stack(LabJanitorStack(app, "LabJanitorStack", env=ENV))


@pytest.fixture(scope="session")
def cli_assembly(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Cloud assembly synthesized the way the CDK CLI does it.

    The context from ``cdk.json`` (feature flags included) is applied, path
    metadata is enabled (the CLI turns it on) and assets are staged into a
    real output directory. A ``.pyc`` file is compiled into ``src`` first, so
    the asset exclusion check always has something to exclude.
    """
    py_compile.compile(str(SOURCE_DIR / "shared" / "__init__.py"), doraise=True)
    assert list(SOURCE_DIR.rglob("*.pyc"))

    outdir = tmp_path_factory.mktemp("cdk.out")
    app = cdk.App(
        outdir=str(outdir),
        context={
            **json.loads((ROOT / "cdk.json").read_text())["context"],
            "reportEmail": "cli-test@example.org",
            "approvalEmail": "cli-approver@example.net",
            "aws:cdk:enable-path-metadata": True,
        },
    )
    LabJanitorStack(app, "LabJanitorStack", env=ENV)
    app.synth()
    return outdir


@pytest.fixture(scope="session")
def cli_template(cli_assembly: Path) -> dict:
    """The CloudFormation template from ``cli_assembly``, as a dict."""
    return json.loads((cli_assembly / "LabJanitorStack.template.json").read_text())
