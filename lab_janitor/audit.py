"""Audit trail storage: the append-only event table and the plans bucket."""

from aws_cdk import Duration, RemovalPolicy, Stack
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_iam as iam
from aws_cdk import aws_s3 as s3
from constructs import Construct

from lab_janitor.nag import acknowledge

RESOURCE_INDEX = "by-resource"
PLANS_PREFIX = "plans/"
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
                    "Append-only versioned bucket; CloudTrail data events are added in Plan 3"
                )
            },
        )
        self._plans_arn = self.plans_bucket.arn_for_objects(f"{PLANS_PREFIX}*")

        # Plans are evidence: nobody but the account root may delete them or
        # their versions. The root is the exception so a human can still clean
        # up by hand (lifecycle expiration is not affected by bucket policies).
        # A Deny with "Principal": "*" also binds roles in this account that
        # hold s3:* through their identity policies.
        root_arn = f"arn:aws:iam::{Stack.of(self).account}:root"
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

    def grant_resource_queries(self, grantee: iam.IGrantable) -> iam.Grant:
        """Allow ``grantee`` to query the table and the ``by-resource`` index.

        Intentionally without a ``dynamodb:LeadingKeys`` condition: the GSI is
        keyed by ``resourceId``, not ``pk``, so a ``RUN#*`` restriction would
        block every lookup of a resource's history. Query is read-only, so the
        broader scope does not let the grantee alter the trail.
        """
        return iam.Grant.add_to_principal(
            grantee=grantee,
            actions=["dynamodb:Query"],
            resource_arns=[
                self.table.table_arn,
                f"{self.table.table_arn}/index/{RESOURCE_INDEX}",
            ],
        )
