"""The SigV4 approval CLI: request signing and argument parsing."""

import io
import json
import urllib.error

import pytest
from botocore.credentials import Credentials
from botocore.exceptions import ProfileNotFound

from scripts.approve import build_parser, main, region_from_url, signed_request

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


@pytest.mark.parametrize(
    ("url", "region"),
    [
        ("https://abc123.execute-api.eu-west-1.amazonaws.com", "eu-west-1"),
        ("https://abc123.execute-api.us-gov-west-1.amazonaws.com/", "us-gov-west-1"),
        ("https://abc123.execute-api.cn-north-1.amazonaws.com.cn", "cn-north-1"),
        ("https://approvals.example.com", None),
        ("https://execute-api.eu-west-1.amazonaws.com.evil.example", None),
    ],
)
def test_region_from_url(url: str, region: str | None):
    assert region_from_url(url) == region


def _signing_region(request) -> str:
    # Credential=<key>/<date>/<region>/execute-api/aws4_request
    return request.headers["Authorization"].split("Credential=")[1].split("/")[2]


def test_region_defaults_to_the_api_host_not_the_profile(monkeypatch):
    sent = []
    monkeypatch.setattr("scripts.approve.boto3.Session", _FakeSession)  # profile: us-east-1
    monkeypatch.setattr(
        "scripts.approve._send", lambda request: sent.append(request) or (200, "{}")
    )

    # A request signed for the profile's region instead would come back as 403.
    assert (
        main(["--url", "https://abc123.execute-api.eu-west-1.amazonaws.com", "0b1c", "approve"])
        == 0
    )
    assert _signing_region(sent[0]) == "eu-west-1"


def test_explicit_region_wins_over_the_api_host(monkeypatch):
    sent = []

    class _Session(_FakeSession):
        def __init__(self, **kwargs):
            self.region_name = kwargs.get("region_name") or "us-east-1"

    monkeypatch.setattr("scripts.approve.boto3.Session", _Session)
    monkeypatch.setattr(
        "scripts.approve._send", lambda request: sent.append(request) or (200, "{}")
    )

    url = "https://abc123.execute-api.eu-west-1.amazonaws.com"
    assert main(["--url", url, "0b1c", "approve", "--region", "ap-south-1"]) == 0
    assert _signing_region(sent[0]) == "ap-south-1"


def test_custom_domain_falls_back_to_the_profile_region(monkeypatch):
    sent = []
    monkeypatch.setattr("scripts.approve.boto3.Session", _FakeSession)
    monkeypatch.setattr(
        "scripts.approve._send", lambda request: sent.append(request) or (200, "{}")
    )

    assert main(["--url", "https://approvals.example.com", "0b1c", "approve"]) == 0
    assert _signing_region(sent[0]) == "us-east-1"


def test_http_error_prints_status_and_body_and_exits_1(monkeypatch, capsys):
    def refuse(_request, timeout):
        raise urllib.error.HTTPError(
            "https://example.com", 403, "Forbidden", None, io.BytesIO(b'{"error": "forbidden"}')
        )

    monkeypatch.setattr("scripts.approve.boto3.Session", _FakeSession)
    monkeypatch.setattr("scripts.approve.urllib.request.urlopen", refuse)

    code = main(["--url", "https://abc123.execute-api.us-east-1.amazonaws.com", "0b1c", "reject"])

    out = capsys.readouterr().out
    assert code == 1
    assert "status: 403" in out
    assert '"error": "forbidden"' in out


def test_url_error_exits_1_with_the_reason(monkeypatch, capsys):
    def unreachable(_request, timeout):
        raise urllib.error.URLError("Name or service not known")

    monkeypatch.setattr("scripts.approve.boto3.Session", _FakeSession)
    monkeypatch.setattr("scripts.approve.urllib.request.urlopen", unreachable)

    code = main(["--url", "https://abc123.execute-api.us-east-1.amazonaws.com", "0b1c", "approve"])

    assert code == 1
    assert "could not reach the approval API: Name or service not known" in capsys.readouterr().err
