"""Surface 2: approving without being the approver.

Foothold (a): somebody who only sees or guesses an approval URL. The link is a
bearer credential by design, so these tests attack everything around it: the
confirmation code, the request parsing, the routes, the id in the path, races
and the IAM route's caller check.
"""

import base64
import json
import random

import pytest

from approval import handler as approval
from tests.adversarial.approval_helpers import (
    CALLER_ARN,
    CONFIRMATION,
    IAM_APPROVER,
    IAM_ROUTE,
    LINK_APPROVER,
    LINK_ROUTE,
    ROLE_ARN,
    call,
    claim_params,
    failure_params,
    finalize_params,
    form,
    get_params,
    http_event,
    record_item,
    success_params,
)
from tests.adversarial.helpers import ACCOUNT, APPROVAL_ID, RUN_ID

IAM_BODY = json.dumps({"decision": "approve"})


def pending(api, **overrides) -> None:
    api.ddb.add_response("get_item", {"Item": record_item(**overrides)}, get_params())


def lost_claim(api, approver: dict, old_status: str) -> None:
    api.ddb.add_client_error(
        "update_item",
        service_error_code="ConditionalCheckFailedException",
        http_status_code=400,
        expected_params=claim_params(approver),
        modeled_fields={"Item": record_item(old_status)},
    )


def nothing_decided(api) -> bool:
    return api.writes == [] and api.callbacks == []


@pytest.fixture
def no_store(monkeypatch):
    """Mode ``both`` with the approver set; the request fails if it reaches any AWS client."""

    def refuse():
        raise AssertionError("the request must be refused before any dependency is built")

    monkeypatch.setenv("APPROVAL_AUTH", "both")
    monkeypatch.setenv("APPROVER_ROLE_ARN", ROLE_ARN)
    monkeypatch.setattr(approval, "build_dependencies", refuse)


# --- the confirmation code -------------------------------------------------------------------


@pytest.mark.parametrize(
    "confirm",
    [
        " ",
        "abc",
        "bc1",
        "abc12",
        "xabc1",
        "-abc1",
        RUN_ID,
        "abc1\x00",
        "\x00abc1",
        "a b c 1",
        "\uff41\uff42\uff43\uff11",
        "abc\uff11",
        "\u0430bc1",
        "abc\u00b9",
        "a\u200bbc1",
        "%61bc1",
        "abc1" * 4096,
        "*",
        "%",
        "....",
        "0000",
        "abc1,abc1",
        '["abc1"]',
    ],
    ids=lambda value: ascii(value[:24]),
)
def test_confirmation_lookalikes_are_refused_before_any_write(api, confirm):
    """Substrings, the whole run id, NUL padding, fullwidth and Cyrillic homoglyphs, overlong."""
    pending(api)

    response = call(http_event("POST", LINK_ROUTE, body=form(confirm=confirm)))

    assert response["statusCode"] == 400
    assert "confirmation code did not match" in response["body"]
    assert nothing_decided(api)


def test_confirmation_is_compared_in_constant_time_on_bytes(monkeypatch):
    """``compare_digest`` raises on non-ASCII str, so the handler must compare encoded bytes."""
    seen = []
    real = approval.secrets.compare_digest

    def spy(left, right):
        seen.append((left, right))
        return real(left, right)

    monkeypatch.setattr(approval.secrets, "compare_digest", spy)

    assert approval._confirmation_matches("\u00f1and\u00fa", RUN_ID) is False
    assert approval._confirmation_matches(CONFIRMATION, RUN_ID) is True
    assert len(seen) == 2
    assert all(isinstance(left, bytes) and isinstance(right, bytes) for left, right in seen)


def test_code_in_the_query_string_does_not_count(api):
    """A crafted link ``...?decision=approve&confirm=abc1`` cannot turn a click into a decision."""
    query = form()
    pending(api)

    get = call(http_event("GET", LINK_ROUTE, query=query))
    post_empty = call(http_event("POST", LINK_ROUTE, query=query))
    post_partial = call(http_event("POST", LINK_ROUTE, query=query, body="decision=approve"))

    assert get["statusCode"] == 200
    assert "<form" in get["body"]
    assert post_empty["statusCode"] == 400
    assert post_partial["statusCode"] == 400
    assert nothing_decided(api)


@pytest.mark.parametrize(
    "body",
    [
        f"decision=reject&decision=approve&confirm={CONFIRMATION}",
        f"decision=approve&decision=approve&confirm={CONFIRMATION}",
        f"decision=approve&confirm=zzzz&confirm={CONFIRMATION}",
        f"decision=approve&confirm={CONFIRMATION}&confirm={CONFIRMATION}",
        f"decision=approve&confirm={CONFIRMATION}&a=1&b=2&c=3",
        f"decision[]=approve&confirm={CONFIRMATION}",
        f"decision=approve&confirm[]={CONFIRMATION}",
        f"Decision=approve&Confirm={CONFIRMATION}",
        f"decision=approve;confirm={CONFIRMATION}",
        f"decision%3Dapprove%26confirm%3D{CONFIRMATION}",
        f"\ndecision=approve&confirm={CONFIRMATION}",
        f"decision=approve&confirm={CONFIRMATION}&",
        f"&decision=approve&confirm={CONFIRMATION}",
    ],
)
def test_duplicated_or_mangled_form_fields_are_refused(no_store, body):
    response = call(http_event("POST", LINK_ROUTE, body=body))

    assert response["statusCode"] == 400


@pytest.mark.parametrize(
    "decision",
    [
        "APPROVE",
        "Approve",
        "approved",
        "approve ",
        " approve",
        "approve\x00",
        "yes",
        "true",
        "1",
        "reject,approve",
        "accept",
        "approve\nreject",
        "\u0430pprove",
    ],
    ids=ascii,
)
def test_link_decision_must_be_exactly_approve_or_reject(no_store, decision):
    response = call(http_event("POST", LINK_ROUTE, body=form(decision=decision)))

    assert response["statusCode"] == 400


# --- content types and encodings -------------------------------------------------------------

MULTIPART = (
    "--boundary\r\n"
    'Content-Disposition: form-data; name="decision"\r\n\r\napprove\r\n'
    "--boundary\r\n"
    f'Content-Disposition: form-data; name="confirm"\r\n\r\n{CONFIRMATION}\r\n'
    "--boundary--\r\n"
)


@pytest.mark.parametrize(
    ("content_type", "body"),
    [
        ("application/json", json.dumps({"decision": "approve", "confirm": CONFIRMATION})),
        ("application/json", json.dumps(form())),
        ("multipart/form-data; boundary=boundary", MULTIPART),
        ("text/plain", f"decision=approve\r\nconfirm={CONFIRMATION}\r\n"),
        ("application/xml", f"<decision>approve</decision><confirm>{CONFIRMATION}</confirm>"),
        ("application/x-www-form-urlencoded; charset=utf-16", form().encode().hex()),
        (None, "approve"),
    ],
)
def test_link_route_accepts_nothing_but_a_urlencoded_form(no_store, content_type, body):
    headers = {"content-type": content_type} if content_type else {}

    response = call(http_event("POST", LINK_ROUTE, body=body, headers=headers))

    assert response["statusCode"] == 400


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


@pytest.mark.parametrize(
    ("body", "flag"),
    [
        (form(), True),
        (_b64(form().encode()), False),
        (_b64(_b64(form().encode()).encode()), True),
        (_b64(form().encode()) + "\n", True),
        (" " + _b64(form().encode()), True),
        (_b64(form().encode()).rstrip("="), True),
        (_b64(b"decision=approve&confirm=\xff\xfe"), True),
        (_b64(form().encode("utf-16")), True),
        (form(), "false"),
        (form(), 1),
    ],
)
def test_base64_tricks_are_refused(no_store, body, flag):
    event = http_event("POST", LINK_ROUTE, body=body)
    event["isBase64Encoded"] = flag

    response = call(event)

    assert response["statusCode"] == 400


def test_iam_route_refuses_a_form_and_the_link_route_refuses_an_iam_body(api):
    """Neither route's proof substitutes for the other's."""
    as_approver_on_link = http_event("POST", LINK_ROUTE, body=IAM_BODY, iam_arn=CALLER_ARN)
    form_on_iam = http_event("POST", IAM_ROUTE, body=form(), iam_arn=CALLER_ARN)

    assert call(as_approver_on_link)["statusCode"] == 400
    assert call(form_on_iam)["statusCode"] == 400
    assert api.reads == []
    assert nothing_decided(api)


@pytest.mark.parametrize(
    "body",
    [
        '{"decision": "APPROVE"}',
        '{"decision": "approve "}',
        '{"decision": ["approve"]}',
        '{"decision": {"approve": true}}',
        '{"decision": null}',
        '{"decision": true}',
        '{"Decision": "approve"}',
        '"approve"',
        '[{"decision": "approve"}]',
        '{"decision": "approve"}{"decision": "approve"}',
        '{"decision": "approve"',
        "decision=approve",
        '{"decision": NaN}',
    ],
)
def test_iam_decision_must_be_exactly_approve_or_reject(api, body):
    response = call(http_event("POST", IAM_ROUTE, body=body, iam_arn=CALLER_ARN))

    assert response["statusCode"] == 400
    assert api.reads == []
    assert nothing_decided(api)


@pytest.mark.parametrize(
    "body",
    ["[" * 100_000, '{"decision":' * 50_000, '{"a":' * 100_000 + "1" + "}" * 100_000],
    ids=["arrays", "truncated-objects", "objects"],
)
def test_deeply_nested_json_cannot_decide_or_crash_the_runtime(api, body):
    """Only the signed approver gets this far; the parser's RecursionError must stay contained."""
    response = call(http_event("POST", IAM_ROUTE, body=body, iam_arn=CALLER_ARN))

    assert response["statusCode"] in {400, 500}
    assert api.reads == []
    assert nothing_decided(api)


# --- routes and methods ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "route_key",
    [
        "HEAD /approvals/{id}",
        "PUT /approvals/{id}",
        "PATCH /approvals/{id}",
        "DELETE /approvals/{id}",
        "OPTIONS /approvals/{id}",
        "ANY /approvals/{id}",
        "$default",
        "post /approvals/{id}",
        "POST /approvals/{id}/",
        "POST  /approvals/{id}",
        "POST /Approvals/{id}",
        "POST /approvals/{proxy+}",
        "GET /iam/approvals/{id}",
        "PUT /iam/approvals/{id}",
        "POST /iam/approvals/{id}/",
        "POST /approvals/{id} ",
        "",
    ],
)
def test_only_the_three_declared_routes_exist(no_store, monkeypatch, route_key):
    monkeypatch.setenv("APPROVAL_AUTH", "both")
    event = http_event("POST", LINK_ROUTE, body=form(), iam_arn=CALLER_ARN)
    event["routeKey"] = route_key

    assert call(event)["statusCode"] == 404


def test_the_route_decides_not_the_method_or_path_fields_of_the_request(api):
    """A GET route whose other fields claim POST (and carry a valid form) stays read-only."""
    pending(api)
    event = http_event("POST", LINK_ROUTE, body=form())
    event["routeKey"] = "GET /approvals/{id}"
    event["headers"]["x-http-method-override"] = "POST"

    response = call(event)

    assert response["statusCode"] == 200
    assert "<form" in response["body"]
    assert nothing_decided(api)


# --- the id in the path ----------------------------------------------------------------------

PATH_TRICKS = [
    f"../{APPROVAL_ID}",
    f"{APPROVAL_ID}/",
    f" {APPROVAL_ID}",
    f"{APPROVAL_ID} ",
    f"{APPROVAL_ID}\x00",
    f"{APPROVAL_ID}%00",
    f"%2e%2e%2f{APPROVAL_ID}",
    APPROVAL_ID.upper(),
    APPROVAL_ID.replace("-", ""),
    f"APPROVAL#{APPROVAL_ID}",
    f"RUN#{RUN_ID}",
    f"{APPROVAL_ID}#META",
    "META",
    "RUN#SUMMARY",
    "#",
    "*",
    "x" * 5000,
    "\u00f1and\u00fa",
    '{"S": "x"}',
]


@pytest.mark.parametrize("path_id", PATH_TRICKS, ids=lambda value: ascii(value[:40]))
def test_path_id_is_used_verbatim_under_the_approval_prefix_and_nowhere_else(api, path_id):
    """No trimming, decoding or case folding: a near miss of a real id finds nothing.

    The Stubber checks the key of every read: always ``APPROVAL#<id as sent>`` with sort key
    ``META``, so the id can never address a ``RUN#`` item or another record kind.
    """
    for _ in range(3):
        api.ddb.add_response("get_item", {}, get_params(path_id))

    get = call(http_event("GET", LINK_ROUTE, path_id))
    post = call(http_event("POST", LINK_ROUTE, path_id, body=form()))
    iam = call(http_event("POST", IAM_ROUTE, path_id, body=IAM_BODY, iam_arn=CALLER_ARN))

    assert [get["statusCode"], post["statusCode"], iam["statusCode"]] == [404, 404, 404]
    assert nothing_decided(api)


@pytest.mark.parametrize("path_parameters", [None, {}, {"id": ""}, {"id": None}, {"ID": "x"}])
def test_missing_path_id_never_reaches_the_store(no_store, path_parameters):
    event = http_event("POST", LINK_ROUTE, body=form())
    event["pathParameters"] = path_parameters

    assert call(event)["statusCode"] == 404


# --- races and replays -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("winner", "loser", "callback", "final"),
    [
        ("approve", "reject", "send_task_success", "APPROVED"),
        ("reject", "approve", "send_task_failure", "REJECTED"),
    ],
)
def test_approve_and_reject_racing_only_the_claim_winner_calls_back(
    api, winner, loser, callback, final
):
    """Both requests read PENDING; the loser's conditional claim fails and it sends nothing."""
    params = success_params if winner == "approve" else failure_params
    pending(api)
    api.ddb.add_response("update_item", {}, claim_params(LINK_APPROVER))
    api.sfn.add_response(callback, {}, params(LINK_APPROVER))
    api.ddb.add_response("update_item", {}, finalize_params(final))
    pending(api)  # the loser's read was taken before the winner's claim landed
    lost_claim(api, LINK_APPROVER, final)

    first = call(http_event("POST", LINK_ROUTE, body=form(decision=winner)))
    second = call(http_event("POST", LINK_ROUTE, body=form(decision=loser)))

    assert (first["statusCode"], second["statusCode"]) == (200, 409)
    assert [name for name, _params in api.callbacks] == [callback]


@pytest.mark.parametrize("held_by", ["DECIDING", "APPROVED", "REJECTED"])
def test_iam_approver_cannot_override_a_decision_in_flight(api, held_by):
    pending(api)
    lost_claim(api, IAM_APPROVER, held_by)

    response = call(http_event("POST", IAM_ROUTE, body=IAM_BODY, iam_arn=CALLER_ARN))

    assert response["statusCode"] == 409
    assert api.callbacks == []


@pytest.mark.parametrize("old_status", ["EXPIRED", "PENDING"])
def test_iam_route_cannot_decide_an_expired_approval(api, old_status):
    """EXPIRED, or still PENDING but past ``expiresAt``: the IAM route has no early check,
    so the conditional claim is what stops it."""
    pending(api, expiresAt="2026-11-03T08:59:59+00:00")
    lost_claim(api, IAM_APPROVER, old_status)

    response = call(http_event("POST", IAM_ROUTE, body=IAM_BODY, iam_arn=CALLER_ARN))

    assert response["statusCode"] == 410
    assert api.callbacks == []


@pytest.mark.parametrize(
    "expires_at",
    [
        "2026-11-03T09:00:00+00:00",
        "2026-11-03T08:59:59.999999+00:00",
        "2026-11-03T08:59:59+00:00",
    ],
)
def test_link_route_refuses_a_pending_record_at_or_past_its_expiry(api, expires_at):
    """``expiresAt`` is compared as text: the two shapes ``isoformat()`` produces (with and
    without microseconds) must order correctly against a whole-second ``now``."""
    pending(api, expiresAt=expires_at)

    response = call(http_event("POST", LINK_ROUTE, body=form()))

    assert response["statusCode"] == 410
    assert nothing_decided(api)


def test_unknown_callback_failure_after_the_claim_is_reported_as_unknown(api):
    """Foothold: none (robustness of the claim-then-callback sequence).

    For a ``BotoCoreError`` the handler already answers 504 'may have been recorded'.
    Any other exception between the claim and the finalize has the same unknown outcome
    but falls through to the catch-all 500, whose page says the opposite.
    """
    pending(api)
    api.ddb.add_response("update_item", {}, claim_params(LINK_APPROVER))

    def dies(**_params):
        raise OSError("connection reset while the response was being read")

    api.sfn.client.send_task_success = dies

    response = call(http_event("POST", LINK_ROUTE, body=form()))

    assert response["statusCode"] == 504


# --- the IAM route's caller check ------------------------------------------------------------


@pytest.mark.parametrize(
    "authorizer",
    [
        "absent",
        None,
        {},
        {"iam": None},
        {"iam": {}},
        {"iam": {"userArn": None}},
        {"iam": {"userArn": ""}},
        {"iam": {"userarn": CALLER_ARN}},
        {"iam": {"userId": "AROAEXAMPLE:session", "accountId": ACCOUNT}},
        {"jwt": {"claims": {"userArn": CALLER_ARN}}},
        {"lambda": {"userArn": CALLER_ARN}},
        {"userArn": CALLER_ARN},
        {"iam": {"userArn": f" {CALLER_ARN}"}},
        {"iam": {"userArn": CALLER_ARN.upper()}},
    ],
    ids=repr,
)
def test_iam_route_without_the_authorizer_identity_is_forbidden(no_store, authorizer):
    """Reached without SigV4 (misrouted, or the authorizer dropped): caller-supplied fields
    that merely name the approver must not count."""
    event = http_event(
        "POST",
        IAM_ROUTE,
        body=json.dumps({"decision": "approve", "principal": CALLER_ARN, "userArn": CALLER_ARN}),
        headers={
            "x-amzn-principal": CALLER_ARN,
            "x-amz-security-token": "spoofed",
            "authorization": f"AWS4-HMAC-SHA256 Credential={CALLER_ARN}",
            "x-approver": ROLE_ARN,
        },
        query=f"userArn={CALLER_ARN}",
    )
    if authorizer != "absent":
        event["requestContext"]["authorizer"] = authorizer

    assert call(event)["statusCode"] == 403


@pytest.mark.parametrize(
    "role_arn",
    [
        f"arn:aws:iam::{ACCOUNT}:role/*",
        f"arn:aws:iam::{ACCOUNT}:role/App*",
        f"arn:aws:iam::{ACCOUNT}:role/?pprover",
        f"arn:aws:iam::{ACCOUNT}:role/Approver/",
        f"arn:aws:iam::{ACCOUNT}:role/",
        "arn:aws:iam::*:role/Approver",
        "arn:aws:iam:::role/Approver",
        f"arn:*:iam::{ACCOUNT}:role/Approver",
        f"arn:aws:sts::{ACCOUNT}:assumed-role/Approver/*",
        f"arn:aws:iam::{ACCOUNT}:root",
        "*",
    ],
)
def test_wildcards_in_the_configured_approver_arn_match_nobody(no_store, monkeypatch, role_arn):
    """``lab_janitor.config`` accepts ``role/*``; at run time it must not act as a glob."""
    monkeypatch.setenv("APPROVER_ROLE_ARN", role_arn)

    response = call(http_event("POST", IAM_ROUTE, body=IAM_BODY, iam_arn=CALLER_ARN))

    assert response["statusCode"] == 403


def test_near_miss_caller_arns_are_never_the_approver():
    """Seeded mutation of a valid caller ARN: only a session of the approver role passes."""
    rng = random.Random(20261112)  # noqa: S311 - a reproducible fuzz seed, not a secret
    alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789+=,.@_-"
    assert approval.allowed_principal(CALLER_ARN, ROLE_ARN) is True

    for _ in range(2000):
        mutant = list(CALLER_ARN)
        for _ in range(rng.randint(1, 3)):
            position = rng.randrange(len(mutant))
            action = rng.choice(("replace", "insert", "delete"))
            if action == "replace":
                mutant[position] = rng.choice(alphabet + ":/")
            elif action == "insert":
                mutant.insert(position, rng.choice(alphabet + ":/"))
            else:
                del mutant[position]
        caller = "".join(mutant)
        parts = caller.split(":", 5)
        # The region field (parts[3]) is ignored by the check; STS ARNs never carry one.
        still_the_role_session = (
            len(parts) == 6
            and (parts[0], parts[1], parts[2], parts[4]) == ("arn", "aws", "sts", ACCOUNT)
            and parts[5].split("/")[:2] == ["assumed-role", "Approver"]
            and parts[5].count("/") == 2
        )
        if not still_the_role_session:
            assert approval.allowed_principal(caller, ROLE_ARN) is False, caller


# --- the audit identity ----------------------------------------------------------------------


def test_forwarding_headers_cannot_spoof_the_source_ip_in_the_audit_record(api):
    """The approver identity comes from API Gateway's request context, not from headers."""
    pending(api)
    api.ddb.add_response("update_item", {}, claim_params(LINK_APPROVER))
    api.sfn.add_response("send_task_success", {}, success_params(LINK_APPROVER))
    api.ddb.add_response("update_item", {}, finalize_params("APPROVED"))
    spoofed = {
        "x-forwarded-for": "203.0.113.99, 10.0.0.1",
        "forwarded": "for=203.0.113.99",
        "x-real-ip": "203.0.113.99",
        "true-client-ip": "203.0.113.99",
        "x-amzn-principal": CALLER_ARN,
    }

    response = call(http_event("POST", LINK_ROUTE, body=form(), headers=spoofed))

    assert response["statusCode"] == 200
    ((_name, callback),) = api.callbacks
    assert json.loads(callback["output"])["approvedBy"] == LINK_APPROVER
