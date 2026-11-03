import aws_cdk as cdk
import pytest
from aws_cdk import assertions

from lab_janitor.stack import LabJanitorStack


@pytest.fixture(scope="session")
def stack() -> LabJanitorStack:
    """Build the janitor stack once per session (jsii synthesis is slow)."""
    app = cdk.App(context={"reportEmail": "test@example.com"})
    return LabJanitorStack(
        app,
        "LabJanitorStack",
        env=cdk.Environment(account="123456789012", region="us-east-1"),
    )


@pytest.fixture(scope="session")
def template(stack: LabJanitorStack) -> assertions.Template:
    """Synthesized CloudFormation template of the janitor stack."""
    return assertions.Template.from_stack(stack)
