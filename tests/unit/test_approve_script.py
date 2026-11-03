"""The SigV4 approval CLI: request signing and argument parsing."""

import json

import pytest
from botocore.credentials import Credentials
from botocore.exceptions import ProfileNotFound

from scripts.approve import build_parser, main, signed_request

URL = "https://abc123.execute-api.us-east-1.amazonaws.com/iam/approvals/0b1c2d3e"
BODY = json.dumps({"decision": "approve"}).encode()
CREDENTIALS = Credentials("AKIAEXAMPLE", "secret")


def test_signed_request_has_sigv4_authorization_header():
    request = signed_request(URL, BODY, CREDENTIALS, "us-east-1")

    authorization = request.headers["Authorization"]
    assert authorization.startswith("AWS4-HMAC-SHA256")
    assert "execute-api" in authorization
    assert "us-east-1" in authorization
    assert "X-Amz-Date" in request.headers
    assert request.headers["Content-Type"] == "application/json"
    assert request.method == "POST"
    assert request.url == URL
    assert request.body == BODY


def test_signed_request_includes_session_token():
    credentials = Credentials("AKIAEXAMPLE", "secret", token="placeholder-token")  # noqa: S106 - fake value

    request = signed_request(URL, BODY, credentials, "us-east-1")

    assert request.headers["X-Amz-Security-Token"] == "placeholder-token"


def test_cli_rejects_other_decisions(capsys):
    with pytest.raises(SystemExit) as exit_info:
        build_parser().parse_args(["--url", "https://example.com", "0b1c2d3e", "maybe"])

    assert exit_info.value.code == 2
    assert "invalid choice: 'maybe'" in capsys.readouterr().err


def test_cli_refuses_a_url_that_is_not_https(capsys):
    code = main(["--url", "http://example.com", "0b1c2d3e", "approve"])

    assert code == 1
    assert "https" in capsys.readouterr().err


def test_cli_reports_unusable_credentials_without_a_traceback(monkeypatch, capsys):
    def unusable_session(**_kwargs):
        raise ProfileNotFound(profile="missing")

    monkeypatch.setattr("scripts.approve.boto3.Session", unusable_session)

    code = main(["--url", "https://example.com", "0b1c2d3e", "approve", "--profile", "missing"])

    assert code == 1
    assert "missing" in capsys.readouterr().err


class _FakeSession:
    region_name = "us-east-1"

    def __init__(self, **_kwargs):
        pass

    def get_credentials(self):
        return CREDENTIALS


@pytest.mark.parametrize(("status", "exit_code"), [(200, 0), (409, 1), (403, 1)])
def test_cli_exit_code_follows_the_http_status(monkeypatch, capsys, status, exit_code):
    sent = []

    def fake_send(request):
        sent.append(request)
        return status, json.dumps({"status": "APPROVED"})

    monkeypatch.setattr("scripts.approve.boto3.Session", _FakeSession)
    monkeypatch.setattr("scripts.approve._send", fake_send)

    code = main(["--url", "https://abc123.execute-api.us-east-1.amazonaws.com/", "0b1c", "approve"])

    assert code == exit_code
    assert f"status: {status}" in capsys.readouterr().out
    assert sent[0].url == "https://abc123.execute-api.us-east-1.amazonaws.com/iam/approvals/0b1c"
    assert json.loads(sent[0].body) == {"decision": "approve"}
