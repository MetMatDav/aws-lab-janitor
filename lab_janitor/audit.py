"""Audit trail storage: the append-only event table and the plans bucket."""

from aws_cdk import Duration, RemovalPolicy, Stack
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_iam as iam
from aws_cdk import aws_s3 as s3
from constructs import Construct

from lab_janitor.nag import acknowledge

RESOURCE_INDEX = "by-resource"
PLANS_PREFIX = "plans/"
APPROVAL_KEYS = {"ForAllValues:StringLike": {"dynamodb:LeadingKeys": ["APPROVAL#*"]}}
PLAN_EXPIRATION_DAYS = 400
NONCURRENT_PLAN_EXPIRATION_DAYS = 30


class AuditStore(Construct):
    """DynamoDB audit table and versioned S3 bucket for sweep plans.

    Both resources are retained when the stack is deleted: the audit trail
    must outlive the janitor that produced it.
    """

    def __init__(self, scope: Construct, construct_id: str) -> None:
        super().__init__(scope, construct_id)

        # No explicit table name, so two copies of the stack can coexist in
        # one account (for example a test deployment next to the real one).
        self.table = dynamodb.TableV2(
            self,
            "Table",
            partition_key=dynamodb.Attribute(name="pk", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="sk", type=dynamodb.AttributeType.STRING),
            billing=dynamodb.Billing.on_demand(),
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True
            ),
            global_secondary_indexes=[
                dynamodb.GlobalSecondaryIndexPropsV2(
                    index_name=RESOURCE_INDEX,
                    partition_key=dynamodb.Attribute(
                        name="resourceId", type=dynamodb.AttributeType.STRING
                    ),
                    sort_key=dynamodb.Attribute(name="at", type=dynamodb.AttributeType.STRING),
                    projection_type=dynamodb.ProjectionType.ALL,
                )
            ],
            time_to_live_attribute="ttl",
            removal_policy=RemovalPolicy.RETAIN,
        )

        self.plans_bucket = s3.Bucket(
            self,
            "PlansBucket",
            versioned=True,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            object_ownership=s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
            enforce_ssl=True,
            removal_policy=RemovalPolicy.RETAIN,
            lifecycle_rules=[
                s3.LifecycleRule(
                    prefix=PLANS_PREFIX,
                    expiration=Duration.days(PLAN_EXPIRATION_DAYS),
                    noncurrent_version_expiration=Duration.days(NONCURRENT_PLAN_EXPIRATION_DAYS),
                )
            ],
        )
        acknowledge(
            self.plans_bucket,
            {
                "AwsSolutions::AwsSolutions-S1": (
                    "Append-only versioned bucket; object-level access is recorded by "
                    "CloudTrail data events unless createTrail is none"
                )
            },
        )
        self._plans_arn = self.plans_bucket.arn_for_objects(f"{PLANS_PREFIX}*")

        # Plans are evidence: nobody but the account root may delete them or
        # their versions. The root is the exception so a human can still clean
        # up by hand (lifecycle expiration is not affected by bucket policies).
        # A Deny with "Principal": "*" also binds roles in this account that
        # hold s3:* through their identity policies.
        stack = Stack.of(self)
        root_arn = f"arn:{stack.partition}:iam::{stack.account}:root"
        self.plans_bucket.add_to_resource_policy(
            iam.PolicyStatement(
                effect=iam.Effect.DENY,
                principals=[iam.StarPrincipal()],
                actions=["s3:DeleteObject", "s3:DeleteObjectVersion"],
                resources=[self._plans_arn],
                conditions={"StringNotEquals": {"aws:PrincipalArn": root_arn}},
            )
        )

    def allow_plan_writes(self, grantee_role_arn: str) -> None:
        """Make ``grantee_role_arn`` the only principal that can write plans.

        This is a Deny for everyone else, not an Allow: the writer still needs
        an identity policy granting ``s3:PutObject``.
        """
        self.plans_bucket.add_to_resource_policy(
            iam.PolicyStatement(
                effect=iam.Effect.DENY,
                principals=[iam.StarPrincipal()],
                actions=["s3:PutObject"],
                resources=[self._plans_arn],
                conditions={"StringNotEquals": {"aws:PrincipalArn": grantee_role_arn}},
            )
        )

    def grant_run_event_writes(self, grantee: iam.IGrantable) -> iam.Grant:
        """Allow ``grantee`` to put audit items only under run partitions.

        ``dynamodb:LeadingKeys`` restricts the partition key of written items,
        so the grantee can append ``RUN#...`` events but cannot forge items in
        any other partition.
        """
        return iam.Grant.add_to_principal(
            grantee=grantee,
            actions=["dynamodb:PutItem"],
            resource_arns=[self.table.table_arn],
            conditions={"ForAllValues:StringLike": {"dynamodb:LeadingKeys": ["RUN#*"]}},
        )

    def grant_approval_writes(self, grantee: iam.IGrantable) -> iam.Grant:
        """Allow ``grantee`` to create approval records and refresh their task token.

        ``PutItem`` creates the record; ``UpdateItem`` stores the new task
        token when a retried request finds the record already there. Both are
        limited to ``APPROVAL#...`` partitions.
        """
        return self._grant_approval_items(grantee, ["dynamodb:PutItem", "dynamodb:UpdateItem"])

    def grant_approval_reads(self, grantee: iam.IGrantable) -> iam.Grant:
        """Allow ``grantee`` to read single approval records."""
        return self._grant_approval_items(grantee, ["dynamodb:GetItem"])

    def grant_approval_updates(self, grantee: iam.IGrantable) -> iam.Grant:
        """Allow ``grantee`` to update existing approval records (claim, decide, expire)."""
        return self._grant_approval_items(grantee, ["dynamodb:UpdateItem"])

    def _grant_approval_items(self, grantee: iam.IGrantable, actions: list[str]) -> iam.Grant:
        return iam.Grant.add_to_principal(
            grantee=grantee,
            actions=actions,
            resource_arns=[self.table.table_arn],
            conditions=APPROVAL_KEYS,
        )

    def grant_plan_reads(self, grantee: iam.IGrantable) -> iam.Grant:
        """Allow ``grantee`` to read plans and their versions under ``plans/``.

        ``GetObjectVersion`` because consumers read the exact version the
        detector reported, so what they act on always matches that run.
        """
        return iam.Grant.add_to_principal(
            grantee=grantee,
            actions=["s3:GetObject", "s3:GetObjectVersion"],
            resource_arns=[self._plans_arn],
        )

    def grant_resource_queries(self, grantee: iam.IGrantable) -> iam.Grant:
        """Allow ``grantee`` to query the ``by-resource`` index, and only the index.

        Intentionally without a ``dynamodb:LeadingKeys`` condition: the GSI is
        keyed by ``resourceId``, not ``pk``, so a ``RUN#*`` restriction would
        block every lookup of a resource's history. The table itself is left
        out: a Query on the table could read an ``APPROVAL#`` partition, task
        token included, and approval records have no ``resourceId``, so they
        are not in the index. IAM authorizes an index query on the index ARN.
        """
        return iam.Grant.add_to_principal(
            grantee=grantee,
            actions=["dynamodb:Query"],
            resource_arns=[f"{self.table.table_arn}/index/{RESOURCE_INDEX}"],
        )
