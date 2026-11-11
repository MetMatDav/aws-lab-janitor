"""Helpers to inspect IAM statements and the state machine in a synthesized template."""

import json

from aws_cdk import assertions

DETECTOR_HANDLER = "detector.handler.handler"
NOTIFIER_HANDLER = "notifier.handler.handler"
APPROVAL_HANDLER = "approval.handler.handler"
EXECUTOR_HANDLER = "executor.handler.handler"


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


def render(value: object) -> str:
    """A resource value as text: ``Fn::Join`` parts joined, any ``Ref`` shown as ``<name>``.

    ``{"Fn::Join": ["", ["arn:", {"Ref": "AWS::Partition"}, ":ec2:*::snapshot/*"]]}``
    renders as ``arn:<AWS::Partition>:ec2:*::snapshot/*``.
    """
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and "Ref" in value:
        return f"<{value['Ref']}>"
    if isinstance(value, dict) and "Fn::GetAtt" in value:
        return "<{}>".format(".".join(value["Fn::GetAtt"]))
    if isinstance(value, dict) and "Fn::Join" in value:
        separator, parts = value["Fn::Join"]
        return separator.join(render(part) for part in parts)
    raise ValueError(f"cannot render {value!r}")


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
    return statements_of_role(template, function_role_id(template, handler))


def statements_of_role(template: assertions.Template, role_id: str) -> list[dict]:
    """Statements of the attached ``AWS::IAM::Policy`` resources and inline policies of a role."""
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


def executor_statements(template: assertions.Template) -> list[dict]:
    """Statements granted to the executor function's execution role."""
    return role_statements(template, EXECUTOR_HANDLER)


def approval_statements(template: assertions.Template) -> list[dict]:
    """Statements granted to the approval handler function's execution role."""
    return role_statements(template, APPROVAL_HANDLER)


def _definition(machine: dict) -> dict:
    """A state machine resource's definition as a dict.

    The ``DefinitionString`` is an ``Fn::Join``; every non-string part (a
    ``Ref`` or ``Fn::GetAtt`` to an ARN) is replaced by the literal ``"X"``
    before the joined string is parsed.
    """
    definition = machine["Properties"]["DefinitionString"]
    if isinstance(definition, str):
        return json.loads(definition)
    parts = definition["Fn::Join"][1]
    return json.loads("".join(part if isinstance(part, str) else "X" for part in parts))


def _machine_with_state(template: assertions.Template, state: str) -> str:
    """Logical id of the only state machine whose definition has a state named ``state``."""
    machines = template.find_resources("AWS::StepFunctions::StateMachine")
    matches = [
        logical_id
        for logical_id, machine in machines.items()
        if state in _definition(machine)["States"]
    ]
    assert len(matches) == 1, f"expected exactly one state machine with a {state} state"
    return matches[0]


def state_machine_id(template: assertions.Template) -> str:
    """Logical id of the weekly state machine (the one with the ``Detect`` state)."""
    return _machine_with_state(template, "Detect")


def reminder_machine_id(template: assertions.Template) -> str:
    """Logical id of the approval reminder state machine (the one with the ``Remind`` state)."""
    return _machine_with_state(template, "Remind")


def _machine_role_id(template: assertions.Template, machine_id: str) -> str:
    machine = template.find_resources("AWS::StepFunctions::StateMachine")[machine_id]
    return machine["Properties"]["RoleArn"]["Fn::GetAtt"][0]


def state_machine_role_id(template: assertions.Template) -> str:
    """Logical id of the weekly state machine's execution role."""
    return _machine_role_id(template, state_machine_id(template))


def reminder_role_id(template: assertions.Template) -> str:
    """Logical id of the reminder state machine's execution role."""
    return _machine_role_id(template, reminder_machine_id(template))


def state_machine_definition(template: assertions.Template) -> dict:
    """The weekly state machine's definition as a dict."""
    machines = template.find_resources("AWS::StepFunctions::StateMachine")
    return _definition(machines[state_machine_id(template)])


def reminder_definition(template: assertions.Template) -> dict:
    """The reminder state machine's definition as a dict."""
    machines = template.find_resources("AWS::StepFunctions::StateMachine")
    return _definition(machines[reminder_machine_id(template)])
