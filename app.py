#!/usr/bin/env python3
"""CDK entry point for the lab janitor."""

import os

import aws_cdk as cdk

from lab_janitor.stack import LabJanitorStack

REQUIRED_ENV = ("CDK_DEFAULT_ACCOUNT", "CDK_DEFAULT_REGION")

missing = [name for name in REQUIRED_ENV if not os.environ.get(name)]
if missing:
    raise SystemExit(
        f"{' and '.join(missing)} {'is' if len(missing) == 1 else 'are'} not set: "
        "run through the CDK CLI with credentials and a default region, "
        "or export CDK_DEFAULT_ACCOUNT and CDK_DEFAULT_REGION"
    )

app = cdk.App()
LabJanitorStack(
    app,
    "LabJanitorStack",
    env=cdk.Environment(
        account=os.environ["CDK_DEFAULT_ACCOUNT"], region=os.environ["CDK_DEFAULT_REGION"]
    ),
)
app.synth()
