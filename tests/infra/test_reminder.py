from aws_cdk import assertions

from tests.infra.helpers import (
    NOTIFIER_HANDLER,
    actions,
    reminder_definition,
    reminder_machine_id,
    reminder_role_id,
    render,
    resources,
    state_machine_role_id,
    statements_of_role,
)

TOO_MANY_REQUESTS_RETRY = {
    "ErrorEquals": ["Lambda.TooManyRequestsException"],
    "IntervalSeconds": 30,
    "MaxAttempts": 5,
    "BackoffRate": 2,
}


def _notifier_id(template: assertions.Template) -> str:
    (function_id,) = template.find_resources(
        "AWS::Lambda::Function", {"Properties": {"Handler": NOTIFIER_HANDLER}}
    )
    return function_id


def test_there_are_two_state_machines(template: assertions.Template):
    template.resource_count_is("AWS::StepFunctions::StateMachine", 2)


def test_reminder_waits_then_invokes_the_notifier_in_reminder_mode(
    template: assertions.Template,
):
    definition = reminder_definition(template)
    states = definition["States"]

    assert set(states) == {"Wait", "Remind", "Reminded"}
    assert definition["StartAt"] == "Wait"
    assert states["Wait"] == {"Type": "Wait", "SecondsPath": "$.waitSeconds", "Next": "Remind"}
    remind = states["Remind"]
    assert remind["Type"] == "Task"
    # payload_response_only: Parameters is the notifier's event itself.
    assert remind["Parameters"] == {"mode": "reminder", "approvalId.$": "$.approvalId"}
    assert TOO_MANY_REQUESTS_RETRY in remind["Retry"]
    assert remind["Next"] == "Reminded"
    assert states["Reminded"] == {"Type": "Succeed"}


def test_reminder_machine_is_standard_and_times_out_after_61_hours(
    template: assertions.Template,
):
    machine = template.find_resources("AWS::StepFunctions::StateMachine")[
        reminder_machine_id(template)
    ]
    assert machine["Properties"]["StateMachineType"] == "STANDARD"
    assert "StateMachineName" not in machine["Properties"]
    assert reminder_definition(template)["TimeoutSeconds"] == 61 * 3600


def test_reminder_role_can_only_invoke_the_notifier(template: assertions.Template):
    statements = statements_of_role(template, reminder_role_id(template))
    notifier = _notifier_id(template)

    assert {action for st in statements for action in actions(st)} == {"lambda:InvokeFunction"}
    assert {render(resource) for st in statements for resource in resources(st)} == {
        f"<{notifier}.Arn>",
        f"<{notifier}.Arn>:*",
    }
    assert all(st["Effect"] == "Allow" for st in statements)


def test_weekly_role_can_start_only_the_reminder(template: assertions.Template):
    statements = statements_of_role(template, state_machine_role_id(template))
    starts = [st for st in statements if "states:StartExecution" in actions(st)]

    assert len(starts) == 1
    (statement,) = starts
    assert actions(statement) == ["states:StartExecution"]
    assert resources(statement) == [{"Ref": reminder_machine_id(template)}]
