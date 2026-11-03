#!/usr/bin/env python3
"""CDK entry point for the lab janitor."""

import os

import aws_cdk as cdk

from lab_janitor.stack import LabJanitorStack

account = os.environ.get("CDK_DEFAULT_ACCOUNT")
region = os.environ.get("CDK_DEFAULT_REGION")
if not account:
    raise SystemExit(
        "CDK_DEFAULT_ACCOUNT is not set: run through the CDK CLI with credentials "
        "or export CDK_DEFAULT_ACCOUNT and CDK_DEFAULT_REGION"
    )

app = cdk.App()
LabJanitorStack(app, "LabJanitorStack", env=cdk.Environment(account=account, region=region))
app.synth()
