"""Helpers to inspect IAM statements in a synthesized template."""

from aws_cdk import assertions

DETECTOR_HANDLER = "detector.handler.handler"


def _references(value: object, logical_id: str) -> bool:
    """Return True when ``value`` is a ``Ref`` or ``Fn::GetAtt`` to ``logical_id``."""
    if not isinstance(value, dict):
        return False
    if value.get("Ref") == logical_id:
        return True
    get_att = value.get("Fn::GetAtt")
    return isinstance(get_att, list) and bool(get_att) and get_att[0] == logical_id


def _policy_statements(document: dict) -> list[dict]:
    statements = document.get("Statement", [])
    return [statements] if isinstance(statements, dict) else list(statements)


def _role_inline_statements(role: dict) -> list[dict]:
    return [
        statement
        for policy in role.get("Properties", {}).get("Policies", [])
        for statement in _policy_statements(policy["PolicyDocument"])
    ]


def iam_statements(template: assertions.Template) -> list[dict]:
    """Every statement of every IAM policy and every inline role policy."""
    policies = template.find_resources("AWS::IAM::Policy")
    roles = template.find_resources("AWS::IAM::Role")
    return [
        statement
        for policy in policies.values()
        for statement in _policy_statements(policy["Properties"]["PolicyDocument"])
    ] + [statement for role in roles.values() for statement in _role_inline_statements(role)]


def actions(statement: dict) -> list[str]:
    """The statement's ``Action`` as a list, whether it is a string or a list."""
    value = statement.get("Action", [])
    return [value] if isinstance(value, str) else list(value)


def resources(statement: dict) -> list:
    """The statement's ``Resource`` as a list, whether it is a single value or a list."""
    value = statement.get("Resource", [])
    return list(value) if isinstance(value, list) else [value]


def function_role_id(template: assertions.Template, handler: str) -> str:
    """Logical id of the execution role of the function whose handler is ``handler``."""
    functions = template.find_resources(
        "AWS::Lambda::Function", {"Properties": {"Handler": handler}}
    )
    assert len(functions) == 1, f"expected one function with handler {handler}"
    (function,) = functions.values()
    return function["Properties"]["Role"]["Fn::GetAtt"][0]


def role_statements(template: assertions.Template, handler: str) -> list[dict]:
    """Statements granted to the execution role of the function with ``handler``.

    Includes every ``AWS::IAM::Policy`` attached to the role and the role's
    inline policies.
    """
    role_id = function_role_id(template, handler)
    policies = template.find_resources("AWS::IAM::Policy")
    attached = [
        statement
        for policy in policies.values()
        if any(_references(role, role_id) for role in policy["Properties"].get("Roles", []))
        for statement in _policy_statements(policy["Properties"]["PolicyDocument"])
    ]
    role = template.find_resources("AWS::IAM::Role")[role_id]
    return attached + _role_inline_statements(role)


def detector_statements(template: assertions.Template) -> list[dict]:
    """Statements granted to the detector function's execution role."""
    return role_statements(template, DETECTOR_HANDLER)
