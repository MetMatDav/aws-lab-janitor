"""CloudWatch alarms: every way the janitor can fail without anyone noticing.

All alarms notify the alerts topic when they fire and when they recover.
"""

from aws_cdk import Duration
from aws_cdk import aws_cloudwatch as cloudwatch
from aws_cdk import aws_cloudwatch_actions as cw_actions
from aws_cdk import aws_sqs as sqs
from aws_cdk import aws_stepfunctions as sfn
from constructs import Construct

from lab_janitor.config import JanitorConfig
from lab_janitor.notifications import Notifications

ALARM_PERIOD = Duration.minutes(5)
# CloudWatch caps an alarm's evaluation range at 7 days: 7 periods of one day.
HEARTBEAT_PERIOD = Duration.days(1)
HEARTBEAT_DAYS = 7


class Alarms(Construct):
    """The janitor's alarms.

    - ``DlqNotEmpty``: the scheduler could not start a run, or SNS could not
      deliver a report or an alert email. Approval emails are never sent to
      the queue (ADR-0015).
    - ``ExecutionFailed``: a weekly execution failed or timed out. A backstop
      for failures that never reach the ``NotifyFailure`` state.
    - ``ReminderFailed``: an approval reminder execution failed.
    - ``Heartbeat``: no execution started for 7 days (schedule disabled, stack
      broken, account issue). Missing data is breaching, so a brand new stack
      alarms until its first run. Its actions follow ``janitorEnabled``, so a
      deliberate pause does not notify.

    No alarm watches email delivery. A publish that reaches nobody (a
    subscription pending confirmation, or removed) is refused by the notifier
    before it happens, which fails the run. Comparing the SNS publish and
    delivery metrics was tried first and raised false alarms: CloudWatch can
    receive the publish datapoint of a message before its delivery datapoint.
    """

    def __init__(  # noqa: PLR0913 - construct props are keyword-only dependencies
        self,
        scope: Construct,
        construct_id: str,
        *,
        config: JanitorConfig,
        state_machine: sfn.IStateMachine,
        reminder_machine: sfn.IStateMachine,
        notifications: Notifications,
        dlq: sqs.IQueue,
    ) -> None:
        super().__init__(scope, construct_id)

        self.alarms = [
            self._dlq_not_empty(dlq),
            self._execution_failed(state_machine),
            self._reminder_failed(reminder_machine),
            self._heartbeat(state_machine, config),
        ]
        action = cw_actions.SnsAction(notifications.alerts_topic)
        for alarm in self.alarms:
            alarm.add_alarm_action(action)
            alarm.add_ok_action(action)

    def _dlq_not_empty(self, dlq: sqs.IQueue) -> cloudwatch.Alarm:
        return cloudwatch.Alarm(
            self,
            "DlqNotEmpty",
            metric=dlq.metric_approximate_number_of_messages_visible(
                period=ALARM_PERIOD, statistic="Maximum"
            ),
            threshold=0,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
            evaluation_periods=1,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
            alarm_description=(
                "Lab janitor: a scheduler start or an email delivery failed and "
                "landed in the dead-letter queue"
            ),
        )

    def _execution_failed(self, state_machine: sfn.IStateMachine) -> cloudwatch.Alarm:
        return cloudwatch.Alarm(
            self,
            "ExecutionFailed",
            metric=cloudwatch.MathExpression(
                expression="failed + timedOut",
                using_metrics={
                    "failed": state_machine.metric_failed(period=ALARM_PERIOD, statistic="Sum"),
                    "timedOut": state_machine.metric_timed_out(
                        period=ALARM_PERIOD, statistic="Sum"
                    ),
                },
                period=ALARM_PERIOD,
            ),
            threshold=1,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            evaluation_periods=1,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
            alarm_description="Lab janitor: a weekly execution failed or timed out",
        )

    def _reminder_failed(self, reminder_machine: sfn.IStateMachine) -> cloudwatch.Alarm:
        return cloudwatch.Alarm(
            self,
            "ReminderFailed",
            metric=reminder_machine.metric_failed(period=ALARM_PERIOD, statistic="Sum"),
            threshold=1,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            evaluation_periods=1,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
            alarm_description="Lab janitor: an approval reminder failed",
        )

    def _heartbeat(
        self, state_machine: sfn.IStateMachine, config: JanitorConfig
    ) -> cloudwatch.Alarm:
        return cloudwatch.Alarm(
            self,
            "Heartbeat",
            metric=state_machine.metric_started(period=HEARTBEAT_PERIOD, statistic="Sum"),
            threshold=1,
            comparison_operator=cloudwatch.ComparisonOperator.LESS_THAN_THRESHOLD,
            evaluation_periods=HEARTBEAT_DAYS,
            datapoints_to_alarm=HEARTBEAT_DAYS,
            # A day without a datapoint is a day without a run.
            treat_missing_data=cloudwatch.TreatMissingData.BREACHING,
            actions_enabled=config.janitor_enabled,
            alarm_description="Lab janitor: no execution started in the last 7 days",
        )
