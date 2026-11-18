"""Nothing personal in the tree: no account id, address, IP or name of a real resource."""

import pytest

from tests.docs import hygiene


def test_no_forbidden_token_in_tracked_files():
    found = {
        str(path): hits
        for path in hygiene.tracked_text_files()
        if (hits := hygiene.find_forbidden(path.read_text(errors="replace")))
    }
    assert found == {}


# Assembled at run time, so this file passes the test it holds.
@pytest.mark.parametrize(
    "text",
    [
        "account " + "9999" + "88887777" + " here",
        "bucket cdk-assets-" + "9999" + "88887777" + "-us-east-1",
        "write to someone@" + "mailbox.test",
        "write to someone@real" + "example.com",
        "write to someone@not" + "github.com",
        "from " + ".".join(["8", "8", "8", "8"]),
        "cd /home/" + "someone/project",
        "cd /Users/" + "someone/project",
    ],
)
def test_find_forbidden_catches(text):
    assert hygiene.find_forbidden(text)


@pytest.mark.parametrize(
    "text",
    [
        "arn:aws:iam::123456789012:role/x and 111122223333",
        "ci@example.com, noreply@anthropic.com, no-reply@sns.amazonaws.com",
        "198.51.100.7, 203.0.113.9, 192.0.2.1, 127.0.0.1, 0.0.0.0",
        "aws-cdk-lib 2.275.0 and botocore 1.43.123",
        "a plan sha256 " + "3" * 64,
        "approval 11111111-aaaa-bbbb-cccc-222222222222",
        "run 20261112T195659Z-bbb8 at 2026-11-12T19:56:59.123456+00:00",
    ],
)
def test_find_forbidden_allows(text):
    assert hygiene.find_forbidden(text) == []


def test_a_forbidden_name_is_reported_by_its_hash_not_its_text():
    name = "made-up-" + "name"
    digest = hygiene.digest(name)
    forbidden = frozenset({digest})

    assert hygiene.find_forbidden(f"s3://{name}/key and {name.upper()}.", forbidden) == [
        f"sha256:{digest[:12]}",
        f"sha256:{digest[:12]}",
    ]
