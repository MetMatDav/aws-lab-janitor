"""The approval reminder: a second, tiny state machine.

The weekly workflow starts one execution right before it asks for approval
and does not wait for it. The execution sleeps, then asks the notifier to
remind the approver; the notifier sends nothing unless the approval is still
pending. A separate machine keeps the 60 hour wait out of the weekly run,
whose approval task must end the moment a decision arrives.
"""

from aws_cdk import Duration
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_stepfunctions as sfn
from aws_cdk import aws_stepfunctions_tasks as tasks
from constructs import Construct

from lab_janitor.nag import OBSERVABILITY_REASON, acknowledge, wildcard_resource_finding
from lab_janitor.workflow_approval import REMINDER_AFTER, add_throttle_retry

# Room for the notifier's retries after the wait.
REMINDER_TIMEOUT = REMINDER_AFTER.plus(Duration.hours(1))


class ApprovalReminder(Construct):
    """State machine that waits ``$.waitSeconds`` and then invokes the notifier's reminder mode.

    Input: ``{"approvalId": ..., "runId": ..., "waitSeconds": ...}``. The
    wait comes from the input so an operator can start an execution by hand
    with a few seconds and see the reminder without waiting 60 hours. The
    role can invoke the notifier and nothing else.
    """

    def __init__(self, scope: Construct, construct_id: str, *, notifier: lambda_.IFunction) -> None:
        super().__init__(scope, construct_id)

        wait = sfn.Wait(self, "Wait", time=sfn.WaitTime.seconds_path("$.waitSeconds"))
        remind = tasks.LambdaInvoke(
            self,
            "Remind",
            lambda_function=notifier,
            payload=sfn.TaskInput.from_object(
                {"mode": "reminder", "approvalId": sfn.JsonPath.string_at("$.approvalId")}
            ),
            payload_response_only=True,
            retry_on_service_exceptions=True,
        )
        add_throttle_retry(remind)

        # No logs option: the execution history holds all three states, and
        # log delivery would add wildcard-resource permissions to this role.
        self.state_machine = sfn.StateMachine(
            self,
            "StateMachine",
            definition_body=sfn.DefinitionBody.from_chainable(
                wait.next(remind).next(sfn.Succeed(self, "Reminded"))
            ),
            state_machine_type=sfn.StateMachineType.STANDARD,
            timeout=REMINDER_TIMEOUT,
        )
        acknowledge(
            self.state_machine.role,
            {
                wildcard_resource_finding(
                    notifier, ":*"
                ): "CDK LambdaInvoke grants the function and its versions"
            },
        )
        acknowledge(
            self.state_machine,
            {
                "AwsSolutions::AwsSolutions-SF1": (
                    "Three-state reminder; the execution history holds every event"
                ),
                "AwsSolutions::AwsSolutions-SF2": OBSERVABILITY_REASON,
                "Serverless::Serverless-StepFunctionStateMachineXray": OBSERVABILITY_REASON,
            },
        )
