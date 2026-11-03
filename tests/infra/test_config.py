import aws_cdk as cdk
import pytest
from aws_cdk import assertions

from lab_janitor.config import JanitorConfig


def test_missing_report_email_fails_with_hint(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("JANITOR_REPORT_EMAIL", raising=False)
    app = cdk.App()

    with pytest.raises(ValueError, match="-c reportEmail"):
        JanitorConfig.from_node(app.node)


def test_no_output_contains_email(template: assertions.Template):
    outputs = template.to_json().get("Outputs", {})

    for name, output in outputs.items():
        assert "@" not in str(output.get("Value")), f"output {name} leaks an email address"
