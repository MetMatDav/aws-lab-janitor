import json

import pytest
from aws_cdk import assertions
from aws_cdk.assertions import Match

from tests.infra.helpers import (
    DETECTOR_HANDLER,
    NOTIFIER_HANDLER,
    actions,
    resources,
    role_statements,
    state_machine_definition,
    state_machine_id,
    state_machine_role_id,
    statements_of_role,
)

STATE_NAMES = {
    "Defaults",
    "Normalize",
    "Detect",
    "HasCandidates",
    "ApprovalNotYetImplemented",
    "NotifyReportOnly",
    "NotifyFailure",
    "RunFailed",
}
LAMBDA_TASKS = ("Detect", "NotifyReportOnly")
FORBIDDEN_PREFIXES = ("Delete", "Release", "Terminate")
TOO_MANY_REQUESTS_RETRY = {
    "ErrorEquals": ["Lambda.TooManyRequestsException"],
    "IntervalSeconds": 30,
    "MaxAttempts": 5,
    "BackoffRate": 2,
}
FIVE_MINUTES = 300
# What CDK grants the state machine role for its logs option (see the Step
# Functions guide, "IAM Policies for logging to CloudWatch Logs").
LOG_DELIVERY_ACTIONS = {
    "logs:CreateLogDelivery",
    "logs:GetLogDelivery",
    "logs:UpdateLogDelivery",
    "logs:DeleteLogDelivery",
    "logs:ListLogDeliveries",
    "logs:PutResourcePolicy",
    "logs:DescribeResourcePolicies",
    "logs:DescribeLogGroups",
}


def _topic_id(template: assertions.Template, display_name: str) -> str:
    topics = template.find_resources(
        "AWS::SNS::Topic", {"Properties": {"DisplayName": display_name}}
    )
    (topic_id,) = topics
    return topic_id


def _alarm(template: assertions.Template, metric_name_or_expression: str) -> dict:
    alarms = template.find_resources("AWS::CloudWatch::Alarm")
    matches = [
        alarm
        for alarm in alarms.values()
        if metric_name_or_expression in json.dumps(alarm["Properties"])
    ]
    assert len(matches) == 1, metric_name_or_expression
    (alarm,) = matches
    return alarm["Properties"]


# --- state machine -------------------------------------------------------------------------


def test_state_names_are_exact(template: assertions.Template):
    definition = state_machine_definition(template)
    assert set(definition["States"]) == STATE_NAMES
    assert definition["StartAt"] == "Defaults"


def test_choice_takes_report_path_for_string_false(template: assertions.Template):
    definition = state_machine_definition(template)
    assert definition["States"]["Defaults"]["Result"]["dryRun"] is True
    choice = definition["States"]["HasCandidates"]
    assert choice["Default"] == "NotifyReportOnly"
    assert "StringEquals" not in json.dumps(choice)
    assert "BooleanEquals" in json.dumps(choice)


def test_choice_requires_explicit_false_and_candidates(template: assertions.Template):
    choice = state_machine_definition(template)["States"]["HasCandidates"]
    (rule,) = choice["Choices"]
    assert rule["Next"] == "ApprovalNotYetImplemented"
    assert rule["And"] == [
        {"Variable": "$.config.dryRun", "IsPresent": True},
        {"Variable": "$.config.dryRun", "BooleanEquals": False},
        {"Variable": "$.detect.candidateCount", "NumericGreaterThan": 0},
    ]


def test_approval_placeholder_continues_to_report(template: assertions.Template):
    states = state_machine_definition(template)["States"]
    assert states["ApprovalNotYetImplemented"]["Type"] == "Pass"
    assert states["ApprovalNotYetImplemented"]["Next"] == "NotifyReportOnly"


def test_defaults_and_normalize_merge_execution_input(template: assertions.Template):
    states = state_machine_definition(template)["States"]
    assert states["Defaults"]["Result"] == {"dryRun": True, "minAgeDays": 7}
    assert states["Defaults"]["ResultPath"] == "$.defaults"
    assert states["Defaults"]["Next"] == "Normalize"
    assert states["Normalize"]["Parameters"] == {
        "config.$": "States.JsonMerge($.defaults, $$.Execution.Input, false)"
    }
    assert states["Normalize"]["Next"] == "Detect"


def _raw_definition_parts(template: assertions.Template) -> list:
    machine = template.find_resources("AWS::StepFunctions::StateMachine")[
        state_machine_id(template)
    ]
    return machine["Properties"]["DefinitionString"]["Fn::Join"][1]


def test_detect_passes_config_and_keeps_handler_result(template: assertions.Template):
    detect = state_machine_definition(template)["States"]["Detect"]
    # payload_response_only: the task calls the function ARN directly, so
    # Parameters is the payload itself and the result is the handler's return.
    assert detect["Parameters"] == {"config.$": "$.config"}
    assert detect["ResultPath"] == "$.detect"
    assert "ResultSelector" not in detect
    assert detect["Next"] == "HasCandidates"


def test_notify_report_only_payload(template: assertions.Template):
    notify = state_machine_definition(template)["States"]["NotifyReportOnly"]
    assert notify["Parameters"] == {
        "mode": "report",
        "detect.$": "$.detect",
        "config.$": "$.config",
    }
    assert notify["End"] is True


@pytest.mark.parametrize("state_name", LAMBDA_TASKS)
def test_lambda_invoke_tasks_retry_on_too_many_requests(
    template: assertions.Template, state_name: str
):
    state = state_machine_definition(template)["States"][state_name]
    assert TOO_MANY_REQUESTS_RETRY in state["Retry"]
    service_retries = [r for r in state["Retry"] if "Lambda.ServiceException" in r["ErrorEquals"]]
    assert len(service_retries) == 1


@pytest.mark.parametrize("state_name", LAMBDA_TASKS)
def test_lambda_invoke_tasks_catch_to_notify_failure(
    template: assertions.Template, state_name: str
):
    state = state_machine_definition(template)["States"][state_name]
    assert state["Catch"] == [
        {"ErrorEquals": ["States.ALL"], "ResultPath": "$.error", "Next": "NotifyFailure"}
    ]


def test_notify_failure_publishes_to_alerts_then_fails(template: assertions.Template):
    states = state_machine_definition(template)["States"]
    notify = states["NotifyFailure"]
    assert notify["Resource"].endswith(":states:::sns:publish")
    assert notify["Parameters"]["Subject"] == "[Lab Janitor] Run failed"
    assert notify["Parameters"]["Message.$"] == (
        "States.Format('Execution {} failed with {}: {}', "
        "$$.Execution.Id, $.error.Error, $.error.Cause)"
    )
    assert notify["Next"] == "RunFailed"
    assert states["RunFailed"]["Type"] == "Fail"

    # The topic ARN is a Ref, so the helper replaced it with "X"; check the raw definition.
    assert {"Ref": _topic_id(template, "Lab Janitor Alerts")} in _raw_definition_parts(template)


@pytest.mark.parametrize(
    ("state_name", "handler"),
    [("Detect", DETECTOR_HANDLER), ("NotifyReportOnly", NOTIFIER_HANDLER)],
)
def test_lambda_tasks_invoke_their_functions(
    template: assertions.Template, state_name: str, handler: str
):
    functions = template.find_resources(
        "AWS::Lambda::Function", {"Properties": {"Handler": handler}}
    )
    (function_id,) = functions
    parts = _raw_definition_parts(template)
    marker = f'"{state_name}":{{'
    start = next(i for i, part in enumerate(parts) if isinstance(part, str) and marker in part)
    # The state's Resource is the first token after its name in the joined definition.
    resource = next(part for part in parts[start + 1 :] if not isinstance(part, str))
    assert resource == {"Fn::GetAtt": [function_id, "Arn"]}


def test_state_machine_settings(template: assertions.Template):
    machine = template.find_resources("AWS::StepFunctions::StateMachine")[
        state_machine_id(template)
    ]
    properties = machine["Properties"]
    assert properties["StateMachineType"] == "STANDARD"
    assert "StateMachineName" not in properties
    assert state_machine_definition(template)["TimeoutSeconds"] == 7200

    logging = properties["LoggingConfiguration"]
    assert logging["Level"] == "ERROR"
    assert logging.get("IncludeExecutionData", False) is False
    (destination,) = logging["Destinations"]
    log_group_id = destination["CloudWatchLogsLogGroup"]["LogGroupArn"]["Fn::GetAtt"][0]
    group = template.find_resources("AWS::Logs::LogGroup")[log_group_id]
    assert group["Properties"]["RetentionInDays"] == 90
    assert group["DeletionPolicy"] == "Delete"


# --- schedule ------------------------------------------------------------------------------


def test_schedule_input_and_retries(template: assertions.Template):
    template.has_resource_properties(
        "AWS::Scheduler::Schedule",
        {
            "ScheduleExpressionTimezone": "America/Bogota",
            "State": "ENABLED",
            "Target": Match.object_like(
                {"RetryPolicy": {"MaximumRetryAttempts": 2, "MaximumEventAgeInSeconds": 3600}}
            ),
        },
    )


def test_schedule_runs_monday_morning_with_dry_run_input(template: assertions.Template):
    schedules = template.find_resources("AWS::Scheduler::Schedule")
    assert len(schedules) == 1
    (schedule,) = schedules.values()
    properties = schedule["Properties"]
    assert properties["ScheduleExpression"] == "cron(0 9 ? * MON *)"
    assert properties["Description"] == "Starts the lab janitor every Monday"
    target = properties["Target"]
    assert json.loads(target["Input"]) == {"dryRun": True, "minAgeDays": 7}
    assert target["Arn"] == {"Ref": state_machine_id(template)}
    (dlq_id,) = template.find_resources("AWS::SQS::Queue")
    assert target["DeadLetterConfig"] == {"Arn": {"Fn::GetAtt": [dlq_id, "Arn"]}}


def test_schedule_follows_janitor_enabled_and_dry_run_context(
    live_template: assertions.Template,
):
    (schedule,) = live_template.find_resources("AWS::Scheduler::Schedule").values()
    properties = schedule["Properties"]
    assert properties["State"] == "DISABLED"
    assert json.loads(properties["Target"]["Input"]) == {"dryRun": False, "minAgeDays": 7}
    assert '"dryRun":false' in properties["Target"]["Input"]


# --- alarms --------------------------------------------------------------------------------


def test_alarms(template: assertions.Template):
    template.resource_count_is("AWS::CloudWatch::Alarm", 2)
    template.has_resource_properties(
        "AWS::CloudWatch::Alarm", {"TreatMissingData": "notBreaching", "Threshold": 0}
    )


def test_dlq_alarm(template: assertions.Template):
    alarm = _alarm(template, "ApproximateNumberOfMessagesVisible")
    (dlq_id,) = template.find_resources("AWS::SQS::Queue")
    assert alarm["Namespace"] == "AWS/SQS"
    assert alarm["Dimensions"] == [
        {"Name": "QueueName", "Value": {"Fn::GetAtt": [dlq_id, "QueueName"]}}
    ]
    assert alarm["Statistic"] == "Maximum"
    assert alarm["Period"] == FIVE_MINUTES
    assert alarm["EvaluationPeriods"] == 1
    assert alarm["Threshold"] == 0
    assert alarm["ComparisonOperator"] == "GreaterThanThreshold"
    assert alarm["TreatMissingData"] == "notBreaching"


def test_execution_failed_alarm(template: assertions.Template):
    alarm = _alarm(template, "failed + timedOut")
    assert alarm["Threshold"] == 1
    assert alarm["ComparisonOperator"] == "GreaterThanOrEqualToThreshold"
    assert alarm["TreatMissingData"] == "notBreaching"
    metrics = {m["Id"]: m for m in alarm["Metrics"]}
    assert metrics["expr_1"]["Expression"] == "failed + timedOut"
    for metric_id, metric_name in (
        ("failed", "ExecutionsFailed"),
        ("timedOut", "ExecutionsTimedOut"),
    ):
        stat = metrics[metric_id]["MetricStat"]
        assert stat["Metric"]["MetricName"] == metric_name
        assert stat["Metric"]["Namespace"] == "AWS/States"
        assert stat["Stat"] == "Sum"
        assert stat["Period"] == FIVE_MINUTES


def test_alarms_notify_alerts_on_alarm_and_ok(template: assertions.Template):
    alerts = [{"Ref": _topic_id(template, "Lab Janitor Alerts")}]
    for alarm in template.find_resources("AWS::CloudWatch::Alarm").values():
        assert alarm["Properties"]["AlarmActions"] == alerts
        assert alarm["Properties"]["OKActions"] == alerts
        assert "—" not in alarm["Properties"]["AlarmDescription"]


# --- outputs, description, tags ------------------------------------------------------------


def test_outputs_carry_no_email(template: assertions.Template):
    outputs = template.find_outputs("*")
    names = sorted(outputs)
    assert len(names) == 3
    for prefix in ("StateMachineArn", "PlansBucketName", "AuditTableName"):
        assert any(prefix in name for name in names), prefix
    assert "@" not in json.dumps(outputs)


def test_stack_has_description_and_tags(template: assertions.Template):
    assert template.to_json()["Description"] == (
        "Lab janitor: weekly sweep of lab leftovers with plan, approval and audit trail"
    )
    machine = template.find_resources("AWS::StepFunctions::StateMachine")[
        state_machine_id(template)
    ]
    tags = {t["Key"]: t["Value"] for t in machine["Properties"]["Tags"]}
    assert tags == {"Project": "aws-lab-janitor", "ManagedBy": "cdk"}
    for function in template.find_resources("AWS::Lambda::Function").values():
        function_tags = {t["Key"]: t["Value"] for t in function["Properties"]["Tags"]}
        assert function_tags == {"Project": "aws-lab-janitor", "ManagedBy": "cdk"}


# --- IAM guardrails ------------------------------------------------------------------------


@pytest.mark.parametrize("handler", [DETECTOR_HANDLER, NOTIFIER_HANDLER])
def test_no_allow_statement_grants_delete_actions(template: assertions.Template, handler: str):
    # Plan 1 guardrail: no Lambda role may Delete*/Release*/Terminate* anything.
    # Scoped per Lambda role because the state machine role legitimately holds
    # logs:DeleteLogDelivery (see the next test).
    statements = role_statements(template, handler)
    assert statements
    for st in statements:
        if st["Effect"] != "Allow":
            continue
        for a in actions(st):
            assert not a.split(":")[1].startswith(FORBIDDEN_PREFIXES), (handler, a)


def test_state_machine_role_delete_actions_are_only_log_delivery(template: assertions.Template):
    # CDK's state machine logging grants the vended-logs actions
    # (logs:CreateLogDelivery, logs:DeleteLogDelivery, ...) on "*": Step
    # Functions needs them to manage the delivery to its log group. That is the
    # only Delete* the state machine may hold; it cannot delete lab resources.
    statements = statements_of_role(template, state_machine_role_id(template))
    deletes = {
        a
        for st in statements
        if st["Effect"] == "Allow"
        for a in actions(st)
        if a.split(":")[1].startswith(FORBIDDEN_PREFIXES)
    }
    assert deletes == {"logs:DeleteLogDelivery"}


def test_state_machine_wildcard_resource_is_only_log_delivery(template: assertions.Template):
    # The AwsSolutions-IAM5[Resource::*] acknowledgment on this role covers the
    # log-delivery grant only; a wider grant on "*" must fail here first.
    statements = statements_of_role(template, state_machine_role_id(template))
    wildcard = [st for st in statements if st["Effect"] == "Allow" and "*" in resources(st)]
    assert len(wildcard) == 1
    (statement,) = wildcard
    assert resources(statement) == ["*"]
    assert set(actions(statement)) == LOG_DELIVERY_ACTIONS
    assert len(actions(statement)) == len(LOG_DELIVERY_ACTIONS)
