import json

import pytest
from aws_cdk import assertions
from aws_cdk.assertions import Match

from tests.infra.helpers import (
    APPROVAL_HANDLER,
    DETECTOR_HANDLER,
    EXECUTOR_HANDLER,
    NOTIFIER_HANDLER,
    actions,
    reminder_machine_id,
    render,
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
    "NotifyReportOnly",
    "NewApprovalId",
    "StartReminder",
    "RequestApproval",
    "RecordRejected",
    "MirrorRejected",
    "NotifyRejected",
    "Rejected",
    "RecordExpired",
    "MirrorExpired",
    "NotifyExpired",
    "Expired",
    "Apply",
    "RecordApplied",
    "NotifyResult",
    "Applied",
    "NotifyFailure",
    "RunFailed",
}
# LambdaInvoke tasks whose only catch is "everything to NotifyFailure".
LAMBDA_TASKS = (
    "Detect",
    "NotifyReportOnly",
    "NotifyRejected",
    "NotifyExpired",
    "Apply",
    "NotifyResult",
)
# RequestApproval has the same retries but routes timeouts and rejections elsewhere.
RETRIED_TASKS = (*LAMBDA_TASKS, "RequestApproval")
CONDITIONAL_CHECK_CATCH = "DynamoDB.ConditionalCheckFailedException"
# Where the DynamoDB states put their response (jsii cannot render a null
# ResultPath in raw ASL); it must not overwrite anything a later state reads.
RECORD_RESULT_PATH = "$.recordResult"
DYNAMODB_STATES = (
    "RecordRejected",
    "MirrorRejected",
    "RecordExpired",
    "MirrorExpired",
    "RecordApplied",
)
DYNAMODB_RETRY = {
    "ErrorEquals": [
        "DynamoDB.ProvisionedThroughputExceededException",
        "DynamoDB.ThrottlingException",
        "DynamoDB.InternalServerErrorException",
    ],
    "IntervalSeconds": 2,
    "MaxAttempts": 3,
    "BackoffRate": 2,
}


def _dynamodb_catches(next_state: str) -> list[dict]:
    """The conditional-failure catch first, then everything else to NotifyFailure."""
    return [
        {
            "ErrorEquals": [CONDITIONAL_CHECK_CATCH],
            "ResultPath": "$.recordError",
            "Next": next_state,
        },
        {"ErrorEquals": ["States.ALL"], "ResultPath": "$.error", "Next": "NotifyFailure"},
    ]


FORBIDDEN_PREFIXES = ("Delete", "Release", "Terminate")
TOO_MANY_REQUESTS_RETRY = {
    "ErrorEquals": ["Lambda.TooManyRequestsException"],
    "IntervalSeconds": 30,
    "MaxAttempts": 5,
    "BackoffRate": 2,
}
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


def _function_id(template: assertions.Template, handler: str) -> str:
    functions = template.find_resources(
        "AWS::Lambda::Function", {"Properties": {"Handler": handler}}
    )
    (function_id,) = functions
    return function_id


def _table_id(template: assertions.Template) -> str:
    (table_id,) = template.find_resources("AWS::DynamoDB::GlobalTable")
    return table_id


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
    assert rule["Next"] == "NewApprovalId"
    assert rule["And"] == [
        {"Variable": "$.config.dryRun", "IsPresent": True},
        {"Variable": "$.config.dryRun", "BooleanEquals": False},
        {"Variable": "$.detect.candidateCount", "NumericGreaterThan": 0},
    ]


def test_new_approval_id_uses_states_uuid(template: assertions.Template):
    state = state_machine_definition(template)["States"]["NewApprovalId"]
    assert state["Type"] == "Pass"
    assert state["Parameters"] == {"approvalId.$": "States.UUID()"}
    assert state["ResultPath"] == "$.approvalRequest"
    assert state["Next"] == "StartReminder"


def test_start_reminder_is_fire_and_forget(template: assertions.Template):
    state = state_machine_definition(template)["States"]["StartReminder"]
    assert state["Type"] == "Task"
    # No ".sync": the weekly run does not wait for the 60 hour reminder.
    assert state["Resource"].endswith(":states:::states:startExecution")
    assert state["Parameters"]["Input"] == {
        "approvalId.$": "$.approvalRequest.approvalId",
        "runId.$": "$.detect.runId",
        "waitSeconds": 60 * 3600,
    }
    assert state["ResultPath"] is None
    assert state["Next"] == "RequestApproval"
    # A reminder that cannot be started must never block the approval request.
    assert state["Catch"] == [
        {"ErrorEquals": ["States.ALL"], "ResultPath": "$.reminderError", "Next": "RequestApproval"}
    ]


def test_start_reminder_targets_the_reminder_machine(template: assertions.Template):
    parts = _raw_definition_parts(template)
    marker = '"StartReminder":{'
    start = next(i for i, part in enumerate(parts) if isinstance(part, str) and marker in part)
    target = next(
        part for part in parts[start + 1 :] if "Ref" in part and "AWS::" not in part["Ref"]
    )
    assert target == {"Ref": reminder_machine_id(template)}


def test_request_approval_waits_for_task_token_with_72h_timeout(template: assertions.Template):
    state = state_machine_definition(template)["States"]["RequestApproval"]
    assert state["Type"] == "Task"
    assert state["Resource"].endswith(":states:::lambda:invoke.waitForTaskToken")
    assert state["TimeoutSeconds"] == 259200
    assert state["Parameters"]["Payload"] == {
        "mode": "approval",
        "taskToken.$": "$$.Task.Token",
        "approvalId.$": "$.approvalRequest.approvalId",
        "detect.$": "$.detect",
        "config.$": "$.config",
    }
    assert state["ResultPath"] == "$.approval"
    assert state["Next"] == "Apply"


def test_request_approval_catches_timeout_then_rejected_then_all(template: assertions.Template):
    state = state_machine_definition(template)["States"]["RequestApproval"]
    assert state["Catch"] == [
        {"ErrorEquals": ["States.Timeout"], "ResultPath": "$.error", "Next": "RecordExpired"},
        {"ErrorEquals": ["Rejected"], "ResultPath": "$.error", "Next": "RecordRejected"},
        {"ErrorEquals": ["States.ALL"], "ResultPath": "$.error", "Next": "NotifyFailure"},
    ]


def test_apply_reads_detect_values_not_approval(template: assertions.Template):
    definition = state_machine_definition(template)
    apply = definition["States"]["Apply"]
    # payload_response_only: Parameters is the executor's event itself.
    assert apply["Parameters"] == {
        "detect.$": "$.detect",
        "config.$": "$.config",
        "approval.$": "$.approval",
        "approvalId.$": "$.approvalRequest.approvalId",
    }
    assert apply["ResultPath"] == "$.apply"
    assert apply["Next"] == "RecordApplied"
    # The plan version and digest come from the detector's output only; the
    # approval callback cannot redirect the executor to another plan.
    text = json.dumps(definition)
    for field in ("planVersionId", "planSha256"):
        assert f"$.approval.{field}" not in text


@pytest.mark.parametrize(
    ("state_name", "status", "mirror"),
    [
        ("RecordRejected", "REJECTED", "MirrorRejected"),
        ("RecordExpired", "EXPIRED", "MirrorExpired"),
    ],
)
def test_record_states_are_conditional_and_catch_conditional_failures(
    template: assertions.Template, state_name: str, status: str, mirror: str
):
    state = state_machine_definition(template)["States"][state_name]
    assert state["Type"] == "Task"
    # The partition and the table name are tokens, shown as "X" by the helper.
    assert state["Resource"] == "arn:X:states:::dynamodb:updateItem"
    assert state["Parameters"] == {
        "TableName": "X",
        "Key": {
            "pk": {"S.$": "States.Format('APPROVAL#{}', $.approvalRequest.approvalId)"},
            "sk": {"S": "META"},
        },
        "UpdateExpression": "SET #s = :s, decidedAt = :t",
        "ConditionExpression": "#s IN (:pending, :deciding)",
        "ExpressionAttributeNames": {"#s": "status"},
        "ExpressionAttributeValues": {
            ":s": {"S": status},
            ":t": {"S.$": "$$.State.EnteredTime"},
            ":pending": {"S": "PENDING"},
            ":deciding": {"S": "DECIDING"},
        },
    }
    assert state["ResultPath"] == RECORD_RESULT_PATH
    assert state["Catch"] == _dynamodb_catches(mirror)
    assert state["Next"] == mirror


@pytest.mark.parametrize(
    ("state_name", "event", "detail", "next_state"),
    [
        ("MirrorRejected", "REJECTED", "$.error", "NotifyRejected"),
        ("MirrorExpired", "EXPIRED", "$.error", "NotifyExpired"),
        ("RecordApplied", "APPROVED", "$.apply", "NotifyResult"),
    ],
)
def test_mirror_items_are_conditional_puts_under_run_partition(
    template: assertions.Template, state_name: str, event: str, detail: str, next_state: str
):
    state = state_machine_definition(template)["States"][state_name]
    assert state["Type"] == "Task"
    assert state["Resource"] == "arn:X:states:::dynamodb:putItem"
    item = {
        "pk": {"S.$": "States.Format('RUN#{}', $.detect.runId)"},
        "sk": {"S.$": "States.Format('APPROVAL#{}', $.approvalRequest.approvalId)"},
        "event": {"S": event},
        "at": {"S.$": "$$.State.EnteredTime"},
        "actor": {"S": "statemachine"},
        "detail": {"S.$": f"States.JsonToString({detail})"},
    }
    if state_name == "RecordApplied":
        item["approvedBy"] = {"S.$": "States.JsonToString($.approval.approvedBy)"}
    assert state["Parameters"] == {
        "TableName": "X",
        "Item": item,
        "ConditionExpression": "attribute_not_exists(pk) AND attribute_not_exists(sk)",
    }
    assert state["ResultPath"] == RECORD_RESULT_PATH
    assert state["Catch"] == _dynamodb_catches(next_state)
    assert state["Next"] == next_state


@pytest.mark.parametrize("state_name", DYNAMODB_STATES)
def test_dynamodb_states_retry_transient_errors_and_alert_on_the_rest(
    template: assertions.Template, state_name: str
):
    state = state_machine_definition(template)["States"][state_name]
    assert state["Retry"] == [DYNAMODB_RETRY]
    (conditional, catch_all) = state["Catch"]
    assert conditional["ErrorEquals"] == [CONDITIONAL_CHECK_CATCH]
    assert conditional["Next"] == state["Next"]
    assert catch_all == {
        "ErrorEquals": ["States.ALL"],
        "ResultPath": "$.error",
        "Next": "NotifyFailure",
    }


def test_record_result_path_is_read_by_no_state(template: assertions.Template):
    text = json.dumps(state_machine_definition(template))
    assert text.count(f'"{RECORD_RESULT_PATH}"') == 5  # the five ResultPath values only
    assert f"{RECORD_RESULT_PATH}." not in text


@pytest.mark.parametrize(
    "state_name",
    ["RecordRejected", "MirrorRejected", "RecordExpired", "MirrorExpired", "RecordApplied"],
)
def test_dynamodb_states_write_the_audit_table(template: assertions.Template, state_name: str):
    parts = _raw_definition_parts(template)
    marker = f'"{state_name}":{{'
    start = next(i for i, part in enumerate(parts) if isinstance(part, str) and marker in part)
    tokens = [part for part in parts[start + 1 : start + 4] if not isinstance(part, str)]
    # Resource (partition), then TableName.
    assert tokens[:2] == [{"Ref": "AWS::Partition"}, {"Ref": _table_id(template)}]


@pytest.mark.parametrize(
    ("state_name", "payload", "end"),
    [
        (
            "NotifyRejected",
            {
                "mode": "rejected",
                "detect.$": "$.detect",
                "config.$": "$.config",
                "error.$": "$.error",
            },
            "Rejected",
        ),
        (
            "NotifyExpired",
            {
                "mode": "expired",
                "detect.$": "$.detect",
                "config.$": "$.config",
                "error.$": "$.error",
            },
            "Expired",
        ),
        (
            "NotifyResult",
            {
                "mode": "result",
                "detect.$": "$.detect",
                "config.$": "$.config",
                "approval.$": "$.approval",
                "apply.$": "$.apply",
            },
            "Applied",
        ),
    ],
)
def test_outcome_notifications_end_in_succeed_states(
    template: assertions.Template, state_name: str, payload: dict, end: str
):
    states = state_machine_definition(template)["States"]
    state = states[state_name]
    assert state["Parameters"] == payload
    assert state["ResultPath"] is None
    assert state["Next"] == end
    assert states[end] == {"Type": "Succeed"}


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


@pytest.mark.parametrize("state_name", RETRIED_TASKS)
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
    assert notify["Next"] == "RunFailed"
    assert states["RunFailed"]["Type"] == "Fail"

    # The topic ARN is a Ref, so the helper replaced it with "X"; check the raw definition.
    assert {"Ref": _topic_id(template, "Lab Janitor Alerts")} in _raw_definition_parts(template)


def test_notify_failure_message_survives_missing_cause(template: assertions.Template):
    # A caught error need not carry a Cause; States.Format with $.error.Cause
    # would then raise inside NotifyFailure and the alert would be lost.
    definition = state_machine_definition(template)
    notify = definition["States"]["NotifyFailure"]
    assert notify["Parameters"]["Message.$"] == (
        "States.Format('Execution {} failed: {}', $$.Execution.Id, States.JsonToString($.error))"
    )
    assert "$.error.Cause" not in json.dumps(definition)


@pytest.mark.parametrize(
    ("state_name", "handler"),
    [
        ("Detect", DETECTOR_HANDLER),
        ("NotifyReportOnly", NOTIFIER_HANDLER),
        ("RequestApproval", NOTIFIER_HANDLER),
        ("NotifyRejected", NOTIFIER_HANDLER),
        ("NotifyExpired", NOTIFIER_HANDLER),
        ("Apply", EXECUTOR_HANDLER),
        ("NotifyResult", NOTIFIER_HANDLER),
    ],
)
def test_lambda_tasks_invoke_their_functions(
    template: assertions.Template, state_name: str, handler: str
):
    function_id = _function_id(template, handler)
    parts = _raw_definition_parts(template)
    marker = f'"{state_name}":{{'
    start = next(i for i, part in enumerate(parts) if isinstance(part, str) and marker in part)
    # The function is the first Fn::GetAtt after the state's name in the joined
    # definition: its Resource, or for RequestApproval (whose Resource is the
    # waitForTaskToken integration) its FunctionName parameter.
    function = next(part for part in parts[start + 1 :] if "Fn::GetAtt" in part)
    assert function == {"Fn::GetAtt": [function_id, "Arn"]}


def test_state_machine_settings(template: assertions.Template):
    machine = template.find_resources("AWS::StepFunctions::StateMachine")[
        state_machine_id(template)
    ]
    properties = machine["Properties"]
    assert properties["StateMachineType"] == "STANDARD"
    assert "StateMachineName" not in properties
    definition = state_machine_definition(template)
    # The 72 h approval wait plus the 2 h run budget: the execution must
    # outlive RequestApproval's own timeout so that it reaches RecordExpired.
    assert definition["TimeoutSeconds"] == (72 + 2) * 3600
    assert definition["TimeoutSeconds"] > definition["States"]["RequestApproval"]["TimeoutSeconds"]

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


# --- outputs, description, tags ------------------------------------------------------------


def test_outputs_are_at_stack_level(template: assertions.Template):
    outputs = template.to_json()["Outputs"]
    assert set(outputs) == {
        "StateMachineArn",
        "PlansBucketName",
        "AuditTableName",
        "ApprovalApiUrl",
    }
    assert outputs["StateMachineArn"]["Value"] == {"Ref": state_machine_id(template)}
    (bucket_id,) = template.find_resources("AWS::S3::Bucket")
    assert outputs["PlansBucketName"]["Value"] == {"Ref": bucket_id}
    assert outputs["AuditTableName"]["Value"] == {"Ref": _table_id(template)}
    (api_id,) = template.find_resources("AWS::ApiGatewayV2::Api")
    assert outputs["ApprovalApiUrl"]["Value"] == {"Fn::GetAtt": [api_id, "ApiEndpoint"]}


def test_no_output_contains_an_email(template: assertions.Template):
    outputs = template.to_json()["Outputs"]
    assert outputs
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


def test_state_machine_role_cannot_invoke_the_approval_handler(template: assertions.Template):
    statements = statements_of_role(template, state_machine_role_id(template))
    invoked = {
        render(resource)
        for st in statements
        if st["Effect"] == "Allow" and "lambda:InvokeFunction" in actions(st)
        for resource in resources(st)
    }
    expected = set()
    for handler in (DETECTOR_HANDLER, NOTIFIER_HANDLER, EXECUTOR_HANDLER):
        function_id = _function_id(template, handler)
        expected |= {f"<{function_id}.Arn>", f"<{function_id}.Arn>:*"}
    assert invoked == expected
    assert _function_id(template, APPROVAL_HANDLER) not in json.dumps(statements)


def test_state_machine_dynamodb_grants_carry_leading_keys(template: assertions.Template):
    statements = statements_of_role(template, state_machine_role_id(template))
    dynamodb = [
        st
        for st in statements
        if st["Effect"] == "Allow" and any(a.startswith("dynamodb:") for a in actions(st))
    ]
    table_arn = {"Fn::GetAtt": [_table_id(template), "Arn"]}
    granted = {}
    for st in dynamodb:
        assert resources(st) == [table_arn], st
        (action,) = actions(st)
        granted[action] = st["Condition"]
    assert len(dynamodb) == 2
    assert granted == {
        "dynamodb:PutItem": {"ForAllValues:StringLike": {"dynamodb:LeadingKeys": ["RUN#*"]}},
        "dynamodb:UpdateItem": {
            "ForAllValues:StringLike": {"dynamodb:LeadingKeys": ["APPROVAL#*"]}
        },
    }


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
