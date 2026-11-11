"""Approve or reject a Lab Janitor deletion plan with your AWS identity.

Signs ``POST <url>/iam/approvals/<approvalId>`` with SigV4 (service
``execute-api``) using the credentials of ``--profile`` and prints the HTTP
status and the JSON answer. Exit code 0 means the API answered 2xx.

    python scripts/approve.py --url https://<api-id>.execute-api.<region>.amazonaws.com \\
        <approvalId> approve|reject [--profile P] [--region R]

The request is signed for the region in the API's host name. ``--region``
overrides it; a custom domain falls back to the profile's region.
"""

import argparse
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.exceptions import BotoCoreError

SERVICE = "execute-api"
TIMEOUT_SECONDS = 30
HTTP_OK_LOW, HTTP_OK_HIGH = 200, 300
_API_HOST = re.compile(r"[a-z0-9]+\.execute-api\.([a-z0-9-]+)\.amazonaws\.com(\.cn)?", re.ASCII)


def region_from_url(url: str) -> str | None:
    """The region in an ``execute-api`` host name, or ``None`` for any other host.

    A request signed for another region is refused with 403, which reads like
    a permissions problem; the URL already says where the API lives.
    """
    match = _API_HOST.fullmatch(urllib.parse.urlsplit(url).hostname or "")
    return match.group(1) if match else None


def build_parser() -> argparse.ArgumentParser:
    """The command line: API URL, approval id, decision and the AWS profile and region."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--url", required=True, help="the ApprovalApiUrl stack output (https)")
    parser.add_argument("approval_id", help="the approval id from the approval email")
    parser.add_argument("decision", choices=("approve", "reject"))
    parser.add_argument("--profile", help="AWS profile whose role is the configured approver")
    parser.add_argument(
        "--region", help="signing region; defaults to the one in --url, then the profile's"
    )
    return parser


def signed_request(url: str, body: bytes, credentials, region: str) -> AWSRequest:
    """A ``POST`` of ``body`` to ``url`` signed for API Gateway with ``credentials``."""
    request = AWSRequest(
        method="POST", url=url, data=body, headers={"Content-Type": "application/json"}
    )
    SigV4Auth(credentials, SERVICE, region).add_auth(request)
    return request


def _send(request: AWSRequest) -> tuple[int, str]:
    """Send a signed request; return the HTTP status and the response text."""
    prepared = request.prepare()
    outgoing = urllib.request.Request(  # noqa: S310 - main() refuses any URL but https
        prepared.url, data=prepared.body, headers=dict(prepared.headers), method="POST"
    )
    try:
        with urllib.request.urlopen(outgoing, timeout=TIMEOUT_SECONDS) as response:  # noqa: S310 - https only
            return response.status, response.read().decode()
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode()


def _show(text: str) -> str:
    """The response as pretty JSON, or as-is if the API did not answer with JSON."""
    try:
        return json.dumps(json.loads(text), indent=2)
    except ValueError:
        return text


def main(argv: list[str] | None = None) -> int:
    """Run the CLI; returns the process exit code."""
    args = build_parser().parse_args(argv)
    if not args.url.startswith("https://"):
        print("error: --url must start with https://", file=sys.stderr)
        return 1

    try:
        session = boto3.Session(profile_name=args.profile, region_name=args.region)
        credentials = session.get_credentials()
        frozen = credentials.get_frozen_credentials() if credentials else None
    except BotoCoreError as error:  # unknown profile, expired SSO token
        print(f"error: {error}\nIf the SSO session expired, run: aws sso login", file=sys.stderr)
        return 1
    region = args.region or region_from_url(args.url) or session.region_name
    if frozen is None or region is None:
        print("error: no AWS credentials or region; use --profile and --region", file=sys.stderr)
        return 1

    url = f"{args.url.rstrip('/')}/iam/approvals/{args.approval_id}"
    body = json.dumps({"decision": args.decision}).encode()
    try:
        status, text = _send(signed_request(url, body, frozen, region))
    except urllib.error.URLError as error:
        print(f"error: could not reach the approval API: {error.reason}", file=sys.stderr)
        return 1

    print(f"status: {status}")
    print(_show(text))
    return 0 if HTTP_OK_LOW <= status < HTTP_OK_HIGH else 1


if __name__ == "__main__":
    sys.exit(main())
