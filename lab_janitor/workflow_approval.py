"""The approval branch of the weekly workflow.

``NewApprovalId`` names the approval, ``RequestApproval`` emails the link and
waits up to 72 hours for the decision (task token), and the run ends one of
three ways:

- rejected: ``RecordRejected`` -> ``MirrorRejected`` -> ``NotifyRejected`` -> ``Rejected``
- expired: ``RecordExpired`` -> ``MirrorExpired`` -> ``NotifyExpired`` -> ``Expired``
- approved: ``Apply`` -> ``RecordApplied`` -> ``NotifyResult`` -> ``Applied``

``Record*`` closes the approval record if it is still undecided, ``Mirror*``
and ``RecordApplied`` append the outcome as a ``RUN#`` audit event. All five
are conditional writes; a conditional failure means the item is already
there, so the run carries on.

The DynamoDB states are raw ASL (``CustomState``): the CDK DynamoDB task
constructs grant UpdateItem/PutItem on the whole table without a
``dynamodb:LeadingKeys`` condition. ``CustomState`` grants nothing; the
workflow adds the two scoped audit grants to the state machine role itself.

Their ``ResultPath`` is ``$.recordResult``, not ``null``: jsii drops ``None``
values from ``state_json``, so a ``null`` written here never reaches the
definition, and the default ``$`` would replace the run's state with the
DynamoDB response. No later state reads ``$.recordResult``.
"""

from aws_cdk import Duration, Stack
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_stepfunctions as sfn
from aws_cdk import aws_stepfunctions_tasks as tasks
from constructs import Construct

THROTTLE_ERRORS = ["Lambda.TooManyRequestsException"]
APPROVAL_TIMEOUT = Duration.hours(72)
CONDITIONAL_CHECK_FAILED = ["DynamoDB.ConditionalCheckFailedException"]

APPROVAL_ID = "$.approvalRequest.approvalId"
APPROVAL_KEY = f"States.Format('APPROVAL#{{}}', {APPROVAL_ID})"
RUN_KEY = "States.Format('RUN#{}', $.detect.runId)"
ENTERED_TIME = "$$.State.EnteredTime"
RECORD_RESULT_PATH = "$.recordResult"
STATE_MACHINE_ACTOR = "statemachine"


def add_throttle_retry(task: sfn.TaskStateBase) -> None:
    """Retry a Lambda task that was throttled, with a slow backoff."""
    task.add_retry(
        errors=THROTTLE_ERRORS,
        interval=Duration.seconds(30),
        backoff_rate=2,
        max_attempts=5,
    )


def _paths(**paths: str) -> dict[str, str]:
    """``{key: JsonPath.object_at(path)}`` for each keyword."""
    return {key: sfn.JsonPath.object_at(path) for key, path in paths.items()}


class ApprovalBranch:
    """Builds the states that follow ``HasCandidates`` when a run needs approval."""

    def __init__(
        self,
        scope: Construct,
        *,
        notifier: lambda_.IFunction,
        executor: lambda_.IFunction,
        table_name: str,
        notify_failure: sfn.IChainable,
    ) -> None:
        self._scope = scope
        self._notifier = notifier
        self._executor = executor
        self._table_name = table_name
        self._notify_failure = notify_failure
        self._partition = Stack.of(scope).partition

    def build(self) -> sfn.Chain:
        """The branch, starting at ``NewApprovalId``."""
        record_rejected = self._outcome("Rejected", "REJECTED", mode="rejected")
        record_expired = self._outcome("Expired", "EXPIRED", mode="expired")

        new_approval_id = sfn.Pass(
            self._scope,
            "NewApprovalId",
            parameters={"approvalId": sfn.JsonPath.uuid()},
            result_path="$.approvalRequest",
        )
        request_approval = tasks.LambdaInvoke(
            self._scope,
            "RequestApproval",
            lambda_function=self._notifier,
            integration_pattern=sfn.IntegrationPattern.WAIT_FOR_TASK_TOKEN,
            payload=sfn.TaskInput.from_object(
                {
                    "mode": "approval",
                    "taskToken": sfn.JsonPath.task_token,
                    "approvalId": sfn.JsonPath.string_at(APPROVAL_ID),
                    **_paths(detect="$.detect", config="$.config"),
                }
            ),
            task_timeout=sfn.Timeout.duration(APPROVAL_TIMEOUT),
            result_path="$.approval",
            # The notifier's approval mode is idempotent per approval id: a
            # retry refreshes the stored task token.
            retry_on_service_exceptions=True,
        )
        add_throttle_retry(request_approval)
        request_approval.add_catch(
            record_expired, errors=[sfn.Errors.TIMEOUT], result_path="$.error"
        )
        request_approval.add_catch(record_rejected, errors=["Rejected"], result_path="$.error")
        request_approval.add_catch(
            self._notify_failure, errors=[sfn.Errors.ALL], result_path="$.error"
        )

        apply = tasks.LambdaInvoke(
            self._scope,
            "Apply",
            lambda_function=self._executor,
            # The plan version and digest reach the executor through $.detect
            # only; nothing from the approval callback selects the plan.
            payload=sfn.TaskInput.from_object(
                {
                    **_paths(detect="$.detect", config="$.config", approval="$.approval"),
                    "approvalId": sfn.JsonPath.string_at(APPROVAL_ID),
                }
            ),
            payload_response_only=True,
            result_path="$.apply",
            retry_on_service_exceptions=True,
        )
        add_throttle_retry(apply)
        apply.add_catch(self._notify_failure, result_path="$.error")

        notify_result = self._notify(
            "NotifyResult",
            "result",
            _paths(detect="$.detect", config="$.config", approval="$.approval", apply="$.apply"),
        )
        notify_result.next(sfn.Succeed(self._scope, "Applied"))
        record_applied = self._run_event(
            "RecordApplied",
            "APPROVED",
            "$.apply",
            approvedBy={"S.$": "States.JsonToString($.approval.approvedBy)"},
        )
        _continue_to(record_applied, notify_result)

        return new_approval_id.next(request_approval).next(apply).next(record_applied)

    def _outcome(self, outcome: str, status: str, *, mode: str) -> sfn.CustomState:
        """``Record<outcome>`` -> ``Mirror<outcome>`` -> ``Notify<outcome>`` -> ``<outcome>``."""
        notify = self._notify(
            f"Notify{outcome}",
            mode,
            _paths(detect="$.detect", config="$.config", error="$.error"),
        )
        notify.next(sfn.Succeed(self._scope, outcome))
        mirror = self._run_event(f"Mirror{outcome}", status, "$.error")
        _continue_to(mirror, notify)
        record = self._record_decision(f"Record{outcome}", status)
        _continue_to(record, mirror)
        return record

    def _notify(self, name: str, mode: str, paths: dict[str, str]) -> tasks.LambdaInvoke:
        task = tasks.LambdaInvoke(
            self._scope,
            name,
            lambda_function=self._notifier,
            payload=sfn.TaskInput.from_object({"mode": mode, **paths}),
            payload_response_only=True,
            result_path=sfn.JsonPath.DISCARD,
            retry_on_service_exceptions=True,
        )
        add_throttle_retry(task)
        task.add_catch(self._notify_failure, result_path="$.error")
        return task

    def _record_decision(self, name: str, status: str) -> sfn.CustomState:
        """Close the approval record with ``status`` if it is still undecided."""
        return self._dynamodb_task(
            name,
            "updateItem",
            {
                "Key": {"pk": {"S.$": APPROVAL_KEY}, "sk": {"S": "META"}},
                "UpdateExpression": "SET #s = :s, decidedAt = :t",
                "ConditionExpression": "#s IN (:pending, :deciding)",
                "ExpressionAttributeNames": {"#s": "status"},
                "ExpressionAttributeValues": {
                    ":s": {"S": status},
                    ":t": {"S.$": ENTERED_TIME},
                    ":pending": {"S": "PENDING"},
                    ":deciding": {"S": "DECIDING"},
                },
            },
        )

    def _run_event(
        self, name: str, event: str, detail_path: str, **extra: dict[str, str]
    ) -> sfn.CustomState:
        """Append the outcome to the run's audit partition, once.

        The detail is stored as a JSON string: the ASL DynamoDB integration
        has no way to turn a JSON path into a DynamoDB map.
        """
        return self._dynamodb_task(
            name,
            "putItem",
            {
                "Item": {
                    "pk": {"S.$": RUN_KEY},
                    "sk": {"S.$": APPROVAL_KEY},
                    "event": {"S": event},
                    "at": {"S.$": ENTERED_TIME},
                    "actor": {"S": STATE_MACHINE_ACTOR},
                    "detail": {"S.$": f"States.JsonToString({detail_path})"},
                    **extra,
                },
                "ConditionExpression": "attribute_not_exists(pk) AND attribute_not_exists(sk)",
            },
        )

    def _dynamodb_task(self, name: str, action: str, parameters: dict) -> sfn.CustomState:
        return sfn.CustomState(
            self._scope,
            name,
            state_json={
                "Type": "Task",
                "Resource": f"arn:{self._partition}:states:::dynamodb:{action}",
                "Parameters": {"TableName": self._table_name, **parameters},
                "ResultPath": RECORD_RESULT_PATH,
            },
        )


def _continue_to(state: sfn.CustomState, next_state: sfn.IChainable) -> None:
    """Go on to ``next_state`` whether the conditional write happened or was already done."""
    state.add_catch(next_state, errors=CONDITIONAL_CHECK_FAILED, result_path="$.recordError")
    state.next(next_state)
