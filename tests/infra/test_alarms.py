"""Every CloudWatch alarm of the stack: what it watches and whom it tells."""

import json

from aws_cdk import assertions

from tests.infra.helpers import reminder_machine_id, state_machine_id

FIVE_MINUTES = 300
ONE_HOUR = 3600
ONE_DAY = 86400


def _topic_id(template: assertions.Template, display_name: str) -> str:
    topics = template.find_resources(
        "AWS::SNS::Topic", {"Properties": {"DisplayName": display_name}}
    )
    (topic_id,) = topics
    return topic_id


def _alarm(template: assertions.Template, needle: str, references: str | None = None) -> dict:
    """Properties of the one alarm whose JSON holds ``needle`` (a metric name or expression).

    ``references`` narrows the match to the alarm that mentions that logical
    id, for metrics several alarms share.
    """
    alarms = template.find_resources("AWS::CloudWatch::Alarm")
    matches = [
        alarm["Properties"]
        for alarm in alarms.values()
        if needle in json.dumps(alarm["Properties"])
        and (references is None or references in json.dumps(alarm["Properties"]))
    ]
    assert len(matches) == 1, (needle, references)
    return matches[0]


def test_alarm_count(template: assertions.Template):
    template.resource_count_is("AWS::CloudWatch::Alarm", 6)


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
        assert stat["Metric"]["Dimensions"] == [
            {"Name": "StateMachineArn", "Value": {"Ref": state_machine_id(template)}}
        ]
        assert stat["Stat"] == "Sum"
        assert stat["Period"] == FIVE_MINUTES


def test_reminder_failed_alarm(template: assertions.Template):
    reminder = reminder_machine_id(template)
    alarm = _alarm(template, "ExecutionsFailed", references=reminder)
    assert alarm["Namespace"] == "AWS/States"
    assert alarm["MetricName"] == "ExecutionsFailed"
    assert alarm["Dimensions"] == [{"Name": "StateMachineArn", "Value": {"Ref": reminder}}]
    assert (alarm["Statistic"], alarm["Period"]) == ("Sum", FIVE_MINUTES)
    assert alarm["EvaluationPeriods"] == 1
    assert (alarm["Threshold"], alarm["ComparisonOperator"]) == (
        1,
        "GreaterThanOrEqualToThreshold",
    )
    assert alarm["TreatMissingData"] == "notBreaching"


def test_heartbeat_alarm_is_seven_daily_periods_breaching(template: assertions.Template):
    alarm = _alarm(template, "ExecutionsStarted")
    assert alarm["Namespace"] == "AWS/States"
    assert alarm["Dimensions"] == [
        {"Name": "StateMachineArn", "Value": {"Ref": state_machine_id(template)}}
    ]
    assert (alarm["Statistic"], alarm["Period"]) == ("Sum", ONE_DAY)
    # CloudWatch caps an alarm's evaluation range at 7 days.
    assert (alarm["EvaluationPeriods"], alarm["DatapointsToAlarm"]) == (7, 7)
    assert (alarm["Threshold"], alarm["ComparisonOperator"]) == (1, "LessThanThreshold")
    # No datapoint at all means nobody started the janitor: that is the alarm.
    assert alarm["TreatMissingData"] == "breaching"


def test_heartbeat_actions_follow_janitor_enabled(
    template: assertions.Template, live_template: assertions.Template
):
    assert _alarm(template, "ExecutionsStarted").get("ActionsEnabled", True) is True
    # live_template is synthesized with janitorEnabled=false: a deliberate
    # pause must not page anyone.
    assert _alarm(live_template, "ExecutionsStarted")["ActionsEnabled"] is False


def test_only_the_heartbeat_can_have_its_actions_disabled(live_template: assertions.Template):
    disabled = [
        alarm["Properties"]["AlarmDescription"]
        for alarm in live_template.find_resources("AWS::CloudWatch::Alarm").values()
        if alarm["Properties"].get("ActionsEnabled", True) is False
    ]
    assert disabled == ["Lab janitor: no execution started in the last 7 days"]


def test_undelivered_alarms_cover_reports_and_approvals_only(template: assertions.Template):
    watched = {}
    for alarm in template.find_resources("AWS::CloudWatch::Alarm").values():
        properties = alarm["Properties"]
        metrics = {m["Id"]: m for m in properties.get("Metrics", [])}
        expressions = [m["Expression"] for m in metrics.values() if "Expression" in m]
        if expressions != ["published - delivered - failed"]:
            continue
        topics = set()
        for metric_id, metric_name in (
            ("published", "NumberOfMessagesPublished"),
            ("delivered", "NumberOfNotificationsDelivered"),
            ("failed", "NumberOfNotificationsFailed"),
        ):
            stat = metrics[metric_id]["MetricStat"]
            assert stat["Metric"]["Namespace"] == "AWS/SNS"
            assert stat["Metric"]["MetricName"] == metric_name
            assert (stat["Stat"], stat["Period"]) == ("Sum", ONE_HOUR)
            (dimension,) = stat["Metric"]["Dimensions"]
            assert dimension["Name"] == "TopicName"
            topics.add(dimension["Value"]["Fn::GetAtt"][0])
        (topic_id,) = topics
        assert (properties["Threshold"], properties["ComparisonOperator"]) == (
            0,
            "GreaterThanThreshold",
        )
        assert properties["EvaluationPeriods"] == 1
        assert properties["TreatMissingData"] == "notBreaching"
        watched[topic_id] = properties["AlarmDescription"]

    # The alerts topic cannot report on itself: its own alarm would have nowhere to go.
    assert watched == {
        _topic_id(template, "Lab Janitor Reports"): (
            "Lab janitor: a report was published but reached no subscriber"
        ),
        _topic_id(template, "Lab Janitor Approvals"): (
            "Lab janitor: an approval request was published but reached no subscriber"
        ),
    }


def test_alarms_notify_alerts_on_alarm_and_ok(template: assertions.Template):
    alerts = [{"Ref": _topic_id(template, "Lab Janitor Alerts")}]
    for alarm in template.find_resources("AWS::CloudWatch::Alarm").values():
        assert alarm["Properties"]["AlarmActions"] == alerts
        assert alarm["Properties"]["OKActions"] == alerts
        assert "\u2014" not in alarm["Properties"]["AlarmDescription"]
