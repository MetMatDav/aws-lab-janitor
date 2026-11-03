"""The weekly workflow: state machine, Monday schedule, alarms and outputs."""

from aws_cdk import CfnOutput, Duration, RemovalPolicy, TimeZone
from aws_cdk import aws_cloudwatch as cloudwatch
from aws_cdk import aws_cloudwatch_actions as cw_actions
from aws_cdk import aws_logs as logs
from aws_cdk import aws_scheduler as scheduler
from aws_cdk import aws_scheduler_targets as scheduler_targets
from aws_cdk import aws_sqs as sqs
from aws_cdk import aws_stepfunctions as sfn
from aws_cdk import aws_stepfunctions_tasks as tasks
from constructs import Construct

from lab_janitor.audit import AuditStore
from lab_janitor.config import JanitorConfig
from lab_janitor.detector import Detector
from lab_janitor.nag import OBSERVABILITY_REASON, acknowledge, wildcard_resource_finding
from lab_janitor.notifications import Notifications
from lab_janitor.notifier import Notifier

ALARM_PERIOD = Duration.minutes(5)
THROTTLE_ERRORS = ["Lambda.TooManyRequestsException"]


class Workflow(Construct):
    """Standard state machine that runs the weekly sweep, plus its schedule and alarms.

    Plan 1 only has the report path: ``Defaults`` and ``Normalize`` build the
    run configuration (dry run unless the input explicitly says otherwise),
    ``Detect`` sweeps and writes the plan, and ``NotifyReportOnly`` emails the
    report. A run that would need approval goes through the
    ``ApprovalNotYetImplemented`` placeholder and still only reports. Any task
    failure is published to the alerts topic before the execution fails.
    """

    def __init__(  # noqa: PLR0913 - construct props are keyword-only dependencies
        self,
        scope: Construct,
        construct_id: str,
        *,
        config: JanitorConfig,
        detector: Detector,
        notifier: Notifier,
        notifications: Notifications,
        audit: AuditStore,
        dlq: sqs.IQueue,
    ) -> None:
        super().__init__(scope, construct_id)

        self.state_machine = self._state_machine(config, detector, notifier, notifications)
        self.schedule = self._schedule(config, dlq)
        self.alarms = self._alarms(notifications, dlq)

        CfnOutput(self, "StateMachineArn", value=self.state_machine.state_machine_arn)
        CfnOutput(self, "PlansBucketName", value=audit.plans_bucket.bucket_name)
        CfnOutput(self, "AuditTableName", value=audit.table.table_name)

    def _state_machine(
        self,
        config: JanitorConfig,
        detector: Detector,
        notifier: Notifier,
        notifications: Notifications,
    ) -> sfn.StateMachine:
        notify_failure = tasks.SnsPublish(
            self,
            "NotifyFailure",
            topic=notifications.alerts_topic,
            subject="[Lab Janitor] Run failed",
            message=sfn.TaskInput.from_text(
                sfn.JsonPath.format(
                    "Execution {} failed with {}: {}",
                    sfn.JsonPath.execution_id,
                    sfn.JsonPath.string_at("$.error.Error"),
                    sfn.JsonPath.string_at("$.error.Cause"),
                )
            ),
        ).next(sfn.Fail(self, "RunFailed"))

        defaults = sfn.Pass(
            self,
            "Defaults",
            result=sfn.Result.from_object({"dryRun": True, "minAgeDays": config.min_age_days}),
            result_path="$.defaults",
        )
        # States.JsonMerge: a field in the execution input overrides the
        # default, a missing field keeps it. A string "false" stays a string,
        # which the BooleanEquals in HasCandidates never matches.
        normalize = sfn.Pass(
            self,
            "Normalize",
            parameters={
                "config": sfn.JsonPath.json_merge(
                    sfn.JsonPath.object_at("$.defaults"), sfn.JsonPath.execution_input
                )
            },
        )
        detect = tasks.LambdaInvoke(
            self,
            "Detect",
            lambda_function=detector.function,
            payload=sfn.TaskInput.from_object({"config": sfn.JsonPath.object_at("$.config")}),
            payload_response_only=True,
            result_path="$.detect",
            retry_on_service_exceptions=True,
        )
        notify_report_only = tasks.LambdaInvoke(
            self,
            "NotifyReportOnly",
            lambda_function=notifier.function,
            payload=sfn.TaskInput.from_object(
                {
                    "mode": "report",
                    "detect": sfn.JsonPath.object_at("$.detect"),
                    "config": sfn.JsonPath.object_at("$.config"),
                }
            ),
            payload_response_only=True,
            retry_on_service_exceptions=True,
        )
        for task in (detect, notify_report_only):
            task.add_retry(
                errors=THROTTLE_ERRORS,
                interval=Duration.seconds(30),
                backoff_rate=2,
                max_attempts=5,
            )
            task.add_catch(notify_failure, result_path="$.error")

        # Plan 2 replaces this placeholder with the approval request.
        approval_placeholder = sfn.Pass(self, "ApprovalNotYetImplemented").next(notify_report_only)
        has_candidates = (
            sfn.Choice(self, "HasCandidates")
            .when(
                sfn.Condition.and_(
                    sfn.Condition.is_present("$.config.dryRun"),
                    sfn.Condition.boolean_equals("$.config.dryRun", False),
                    sfn.Condition.number_greater_than("$.detect.candidateCount", 0),
                ),
                approval_placeholder,
            )
            .otherwise(notify_report_only)
        )

        definition = defaults.next(normalize).next(detect).next(has_candidates)

        log_group = logs.LogGroup(
            self,
            "StateMachineLogs",
            retention=logs.RetentionDays.THREE_MONTHS,
            removal_policy=RemovalPolicy.DESTROY,
        )
        state_machine = sfn.StateMachine(
            self,
            "StateMachine",
            definition_body=sfn.DefinitionBody.from_chainable(definition),
            state_machine_type=sfn.StateMachineType.STANDARD,
            logs=sfn.LogOptions(destination=log_group, level=sfn.LogLevel.ERROR),
            timeout=Duration.hours(2),
        )
        self._acknowledge_state_machine(state_machine, detector, notifier)
        return state_machine

    @staticmethod
    def _acknowledge_state_machine(
        state_machine: sfn.StateMachine, detector: Detector, notifier: Notifier
    ) -> None:
        invoke_versions = "CDK LambdaInvoke grants the function and its versions"
        acknowledge(
            state_machine.role,
            {
                wildcard_resource_finding(detector.function, ":*"): invoke_versions,
                wildcard_resource_finding(notifier.function, ":*"): invoke_versions,
                # Added by CDK for the logs option (see the Step Functions guide,
                # "IAM Policies for logging to CloudWatch Logs").
                "AwsSolutions::AwsSolutions-IAM5[Resource::*]": (
                    "Step Functions log delivery: CloudWatch Logs delivery actions such as "
                    "CreateLogDelivery and DescribeLogGroups do not support resource-level "
                    "permissions"
                ),
            },
        )
        acknowledge(
            state_machine,
            {
                "AwsSolutions::AwsSolutions-SF1": (
                    "ERROR-level execution logging is deliberate; the report and the audit "
                    "table hold the detail"
                ),
                "AwsSolutions::AwsSolutions-SF2": OBSERVABILITY_REASON,
                "Serverless::Serverless-StepFunctionStateMachineXray": OBSERVABILITY_REASON,
            },
        )

    def _schedule(self, config: JanitorConfig, dlq: sqs.IQueue) -> scheduler.Schedule:
        return scheduler.Schedule(
            self,
            "Schedule",
            schedule=scheduler.ScheduleExpression.cron(
                minute="0",
                hour=str(config.schedule_hour),
                week_day="MON",
                time_zone=TimeZone.of(config.schedule_timezone),
            ),
            target=scheduler_targets.StepFunctionsStartExecution(
                self.state_machine,
                input=scheduler.ScheduleTargetInput.from_object(
                    {"dryRun": config.dry_run, "minAgeDays": config.min_age_days}
                ),
                retry_attempts=2,
                max_event_age=Duration.hours(1),
                dead_letter_queue=dlq,
            ),
            enabled=config.janitor_enabled,
            description="Starts the lab janitor every Monday",
        )

    def _alarms(self, notifications: Notifications, dlq: sqs.IQueue) -> list[cloudwatch.Alarm]:
        dlq_not_empty = cloudwatch.Alarm(
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
        execution_failed = cloudwatch.Alarm(
            self,
            "ExecutionFailed",
            metric=cloudwatch.MathExpression(
                expression="failed + timedOut",
                using_metrics={
                    "failed": self.state_machine.metric_failed(
                        period=ALARM_PERIOD, statistic="Sum"
                    ),
                    "timedOut": self.state_machine.metric_timed_out(
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

        alarms = [dlq_not_empty, execution_failed]
        action = cw_actions.SnsAction(notifications.alerts_topic)
        for alarm in alarms:
            alarm.add_alarm_action(action)
            alarm.add_ok_action(action)
        return alarms
