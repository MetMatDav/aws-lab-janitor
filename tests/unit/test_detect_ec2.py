from datetime import timedelta

from botocore.exceptions import UnknownServiceError
from botocore.stub import Stubber

from detector.detectors.ec2 import detect_ec2
from tests.unit.conftest import NOW

REGION = "us-east-1"
LAB = [{"Key": "Purpose", "Value": "lab"}]
KEEP = [*LAB, {"Key": "janitor", "Value": "keep"}]
NOT_TERMINATED = ["pending", "running", "shutting-down", "stopping", "stopped"]

OPERATIONS = [
    (
        "describe_instances",
        {"Filters": [{"Name": "instance-state-name", "Values": NOT_TERMINATED}]},
    ),
    ("describe_nat_gateways", {"Filter": [{"Name": "state", "Values": ["available"]}]}),
    ("describe_addresses", {}),
    ("describe_volumes", {}),
    ("describe_images", {"Owners": ["self"]}),
    ("describe_snapshots", {"OwnerIds": ["self"]}),
    ("describe_vpc_endpoints", {}),
    ("describe_network_interfaces", {}),
]
EMPTY = {
    "describe_instances": {"Reservations": []},
    "describe_nat_gateways": {"NatGateways": []},
    "describe_addresses": {"Addresses": []},
    "describe_volumes": {"Volumes": []},
    "describe_images": {"Images": []},
    "describe_snapshots": {"Snapshots": []},
    "describe_vpc_endpoints": {"VpcEndpoints": []},
    "describe_network_interfaces": {"NetworkInterfaces": []},
}


def stub_ec2(
    stub: Stubber,
    responses: dict | None = None,
    failures: dict | None = None,
    custom: dict | None = None,
) -> None:
    """Queue one response per EC2 call, in detector order, with the exact expected parameters.

    ``custom`` maps an operation to ``fn(stub, params)`` that queues its own sequence (e.g. pages).
    """
    responses, failures, custom = responses or {}, failures or {}, custom or {}
    for op, params in OPERATIONS:
        if op in custom:
            custom[op](stub, params)
        elif op in failures:
            stub.add_client_error(
                op, service_error_code=failures[op], http_status_code=403, expected_params=params
            )
        else:
            stub.add_response(op, responses.get(op, EMPTY[op]), params)


def run(ctx, region: str = REGION):
    findings, errors = [], []
    detect_ec2(region, ctx, findings, errors)
    return findings, errors


def days_ago(days: int):
    return NOW - timedelta(days=days)


def by_id(findings, kind):
    return {f.resource_id: f for f in findings if f.kind == kind}


def volume(vol_id, *, days=42, state="available", tags=LAB, **extra):
    return {
        "VolumeId": vol_id,
        "Size": 10,
        "State": state,
        "VolumeType": "gp3",
        "CreateTime": days_ago(days),
        "Attachments": [],
        "Tags": tags,
        **extra,
    }


def snapshot(snap_id, *, days=42, size=8, tags=LAB):
    return {
        "SnapshotId": snap_id,
        "VolumeId": "vol-src",
        "VolumeSize": size,
        "StartTime": days_ago(days),
        "State": "completed",
        "Tags": tags,
    }


def test_access_denied_in_one_call_does_not_stop_the_others(ec2_stub, ctx):
    stub_ec2(
        ec2_stub,
        responses={"describe_volumes": {"Volumes": [volume("vol-1")]}},
        failures={"describe_instances": "UnauthorizedOperation"},
    )
    findings, errors = run(ctx)
    assert [e.service for e in errors] == ["ec2:DescribeInstances"]
    assert errors[0].classification == "access-denied"
    assert any(f.kind == "ebs-volume" for f in findings)
    ec2_stub.assert_no_pending_responses()


def test_every_call_failing_names_its_operation(ec2_stub, ctx):
    stub_ec2(ec2_stub, failures={op: "UnauthorizedOperation" for op, _ in OPERATIONS})
    findings, errors = run(ctx)
    assert findings == []
    assert [e.service for e in errors] == [
        "ec2:DescribeInstances",
        "ec2:DescribeNatGateways",
        "ec2:DescribeAddresses",
        "ec2:DescribeVolumes",
        "ec2:DescribeImages",
        "ec2:DescribeSnapshots",
        "ec2:DescribeVpcEndpoints",
        "ec2:DescribeNetworkInterfaces",
    ]
    ec2_stub.assert_no_pending_responses()


def test_snapshot_rules(ec2_stub, ctx):
    stub_ec2(
        ec2_stub,
        responses={
            "describe_images": {
                "Images": [
                    {
                        "ImageId": "ami-1",
                        "Name": "LAB-base",
                        "CreationDate": "2026-09-21T00:00:00.000Z",
                        "Tags": LAB,
                        "BlockDeviceMappings": [
                            {"DeviceName": "/dev/xvda", "Ebs": {"SnapshotId": "snap-ami"}}
                        ],
                    }
                ]
            },
            "describe_snapshots": {
                "Snapshots": [
                    snapshot("snap-old"),
                    snapshot("snap-young", days=2),
                    snapshot("snap-ami"),
                    snapshot("snap-keep", tags=KEEP),
                    snapshot("snap-other", tags=[]),
                ]
            },
        },
    )
    findings, _ = run(ctx)
    cands = [f for f in findings if f.kind == "ebs-snapshot" and f.candidate]
    assert [f.resource_id for f in cands] == ["snap-old"]
    reasons = {f.resource_id: f.reason for f in findings if f.kind == "ebs-snapshot"}
    assert reasons["snap-young"].startswith("below minimum age")
    assert reasons["snap-young"] == "below minimum age (2 days)"
    assert reasons["snap-ami"] == "backs AMI ami-1"
    assert reasons["snap-keep"] == "tagged janitor=keep"
    assert reasons["snap-other"] == ""
    assert reasons["snap-old"] == "tag Purpose=lab, 42 days old, not referenced by any AMI"
    snaps = by_id(findings, "ebs-snapshot")
    assert snaps["snap-ami"].detail.endswith("backs AMI ami-1")
    assert snaps["snap-old"].estimated_monthly_usd == 0.4
    assert snaps["snap-old"].age_days == 42
    ami = by_id(findings, "ami")["ami-1"]
    assert ami.candidate is False and ami.lab is True and ami.estimated_monthly_usd is None
    ec2_stub.assert_no_pending_responses()


def test_snapshots_fail_closed_when_images_fail(ec2_stub, ctx):
    stub_ec2(
        ec2_stub,
        responses={"describe_snapshots": {"Snapshots": [snapshot("snap-old")]}},
        failures={"describe_images": "UnauthorizedOperation"},
    )
    findings, errors = run(ctx)
    assert [e.service for e in errors] == ["ec2:DescribeImages"]
    snap = by_id(findings, "ebs-snapshot")["snap-old"]
    assert snap.lab is True
    assert snap.candidate is False
    assert snap.reason == "AMI references unknown"
    ec2_stub.assert_no_pending_responses()


def test_snapshots_fail_closed_when_second_images_page_fails(ec2_stub, ctx):
    def images_then_failure(stub, params):
        first_page = {
            "Images": [
                {
                    "ImageId": "ami-1",
                    "BlockDeviceMappings": [{"Ebs": {"SnapshotId": "snap-ami"}}],
                }
            ],
            "NextToken": "page2",
        }
        stub.add_response("describe_images", first_page, params)
        stub.add_client_error(
            "describe_images",
            service_error_code="RequestLimitExceeded",
            http_status_code=503,
            expected_params={**params, "NextToken": "page2"},
        )

    stub_ec2(
        ec2_stub,
        responses={
            "describe_snapshots": {"Snapshots": [snapshot("snap-ami"), snapshot("snap-old")]}
        },
        custom={"describe_images": images_then_failure},
    )
    findings, errors = run(ctx)
    assert [(e.service, e.classification) for e in errors] == [("ec2:DescribeImages", "throttled")]
    snaps = by_id(findings, "ebs-snapshot")
    assert [s for s, f in snaps.items() if f.candidate] == []
    assert snaps["snap-ami"].reason == "AMI references unknown"
    assert snaps["snap-old"].reason == "AMI references unknown"
    ec2_stub.assert_no_pending_responses()


def test_client_creation_failure_is_recorded_not_raised(ctx_factory):
    class BrokenClients:
        def get(self, service, region):
            raise UnknownServiceError(service_name=service, known_service_names="s3")

    findings, errors = run(ctx_factory(clients=BrokenClients()))
    assert findings == []
    assert len(errors) == len(OPERATIONS)
    assert {e.classification for e in errors} == {"service-unavailable-in-region"}


def test_volume_rules(ec2_stub, ctx):
    attached = [{"InstanceId": "i-1", "State": "attached", "VolumeId": "vol-att"}]
    stub_ec2(
        ec2_stub,
        responses={
            "describe_volumes": {
                "Volumes": [
                    volume("vol-old", Size=100),
                    volume("vol-young", days=3),
                    volume("vol-keep", tags=KEEP),
                    volume("vol-inuse", state="in-use"),
                    volume("vol-att", state="in-use", Attachments=attached),
                    volume("vol-other", tags=[{"Key": "Name", "Value": "prod-data"}]),
                ]
            }
        },
    )
    findings, _ = run(ctx)
    vols = by_id(findings, "ebs-volume")
    assert "vol-att" not in vols
    assert [v for v, f in vols.items() if f.candidate] == ["vol-old"]
    assert vols["vol-old"].reason == "tag Purpose=lab, available, 42 days old"
    assert vols["vol-old"].estimated_monthly_usd == 8.0
    assert vols["vol-young"].reason == "below minimum age (3 days)"
    assert vols["vol-keep"].reason == "tagged janitor=keep"
    assert vols["vol-inuse"].reason == "attached"
    assert vols["vol-other"].lab is False and vols["vol-other"].reason == ""
    ec2_stub.assert_no_pending_responses()


def _address(alloc_id, *, tags=LAB, association=None):
    address = {"PublicIp": "203.0.113.10", "AllocationId": alloc_id, "Domain": "vpc", "Tags": tags}
    if association:
        address["AssociationId"] = association
    return address


def test_eip_requires_previous_live_proposal(ec2_stub, ctx_factory):
    idle = {"describe_addresses": {"Addresses": [_address("eipalloc-1")]}}
    stub_ec2(ec2_stub, responses=idle)
    stub_ec2(ec2_stub, responses=idle)
    asked = []

    def proposed(answer):
        def check(resource_id):
            asked.append(resource_id)
            return answer

        return check

    first, _ = run(ctx_factory(was_proposed_live=proposed(False)))
    eip = by_id(first, "elastic-ip")["eipalloc-1"]
    assert eip.candidate is False
    assert eip.reason == "not proposed in a previous live run"

    second, _ = run(ctx_factory(was_proposed_live=proposed(True)))
    eip = by_id(second, "elastic-ip")["eipalloc-1"]
    assert eip.candidate is True
    assert eip.reason == "idle, tagged lab, proposed in a previous live run"
    assert eip.estimated_monthly_usd == 3.65
    assert asked == ["eipalloc-1", "eipalloc-1"]
    ec2_stub.assert_no_pending_responses()


def test_associated_eip_is_never_candidate_but_costs(ec2_stub, ctx_factory):
    stub_ec2(
        ec2_stub,
        responses={
            "describe_addresses": {
                "Addresses": [
                    _address("eipalloc-2", association="eipassoc-1"),
                    _address("eipalloc-3", tags=KEEP),
                    _address("eipalloc-4", tags=[]),
                ]
            }
        },
    )
    findings, _ = run(ctx_factory(was_proposed_live=lambda _id: True))
    eips = by_id(findings, "elastic-ip")
    f = eips["eipalloc-2"]
    assert f.candidate is False and f.estimated_monthly_usd == 3.65
    assert f.reason == "associated"
    assert eips["eipalloc-3"].candidate is False
    assert eips["eipalloc-3"].reason == "tagged janitor=keep"
    assert eips["eipalloc-4"].candidate is False and eips["eipalloc-4"].reason == ""
    ec2_stub.assert_no_pending_responses()


def test_legacy_address_without_allocation_id_is_keyed_by_ip(ec2_stub, ctx_factory):
    legacy = {"PublicIp": "198.51.100.7", "Domain": "standard", "Tags": LAB}
    stub_ec2(ec2_stub, responses={"describe_addresses": {"Addresses": [legacy]}})
    findings, _ = run(ctx_factory(was_proposed_live=lambda _id: True))
    f = by_id(findings, "elastic-ip")["198.51.100.7"]
    assert f.candidate is False and f.estimated_monthly_usd == 3.65
    ec2_stub.assert_no_pending_responses()


def test_instances_nat_endpoints_and_public_ipv4(ec2_stub, ctx):
    stub_ec2(
        ec2_stub,
        responses={
            "describe_instances": {
                "Reservations": [
                    {
                        "Instances": [
                            {
                                "InstanceId": "i-1",
                                "InstanceType": "t3.micro",
                                "State": {"Name": "running"},
                                "LaunchTime": days_ago(10),
                                "PublicIpAddress": "198.51.100.1",
                                "Tags": [{"Key": "Name", "Value": "LAB-web"}],
                            }
                        ]
                    }
                ]
            },
            "describe_nat_gateways": {
                "NatGateways": [
                    {
                        "NatGatewayId": "nat-1",
                        "VpcId": "vpc-1",
                        "State": "available",
                        "CreateTime": days_ago(5),
                        "Tags": LAB,
                    }
                ]
            },
            "describe_vpc_endpoints": {
                "VpcEndpoints": [
                    {
                        "VpcEndpointId": "vpce-if",
                        "VpcEndpointType": "Interface",
                        "ServiceName": "com.amazonaws.us-east-1.ssm",
                        "SubnetIds": ["subnet-a", "subnet-b"],
                    },
                    {
                        "VpcEndpointId": "vpce-gw",
                        "VpcEndpointType": "Gateway",
                        "ServiceName": "com.amazonaws.us-east-1.s3",
                    },
                ]
            },
            "describe_network_interfaces": {
                "NetworkInterfaces": [
                    {"NetworkInterfaceId": "eni-1", "Association": {"PublicIp": "198.51.100.1"}},
                    {"NetworkInterfaceId": "eni-2", "Association": {"PublicIp": "198.51.100.2"}},
                    {
                        "NetworkInterfaceId": "eni-3",
                        "Association": {"PublicIp": "203.0.113.10", "AllocationId": "eipalloc-1"},
                    },
                    {"NetworkInterfaceId": "eni-4"},
                ]
            },
        },
    )
    findings, errors = run(ctx)
    assert errors == []
    assert all(not f.candidate and f.arn is None for f in findings)

    instance = by_id(findings, "ec2-instance")["i-1"]
    assert instance.lab is True and instance.age_days == 10
    assert instance.estimated_monthly_usd is None
    for part in ("t3.micro", "running", "LAB-web", "198.51.100.1"):
        assert part in instance.detail

    nat = by_id(findings, "nat-gateway")["nat-1"]
    assert nat.estimated_monthly_usd == 32.85 and nat.age_days == 5 and nat.lab is True

    endpoints = by_id(findings, "vpc-endpoint")
    assert list(endpoints) == ["vpce-if"]
    assert endpoints["vpce-if"].estimated_monthly_usd == 14.6

    ipv4 = by_id(findings, "public-ipv4")["public-ipv4-us-east-1"]
    assert ipv4.detail == "2 auto-assigned public IPv4 addresses"
    assert ipv4.estimated_monthly_usd == 7.3
    ec2_stub.assert_no_pending_responses()


def test_public_ipv4_finding_omitted_when_none(ec2_stub, ctx):
    stub_ec2(ec2_stub)
    findings, errors = run(ctx)
    assert findings == [] and errors == []
    ec2_stub.assert_no_pending_responses()


def test_detect_ec2_past_deadline_makes_no_calls(ec2_stub, ctx_past_deadline):
    findings, errors = run(ctx_past_deadline)
    assert findings == []
    assert len(errors) == len(OPERATIONS)
    assert {e.classification for e in errors} == {"time-budget"}
    ec2_stub.assert_no_pending_responses()
