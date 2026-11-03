"""EC2-family detectors: instances, NAT gateways, addresses, EBS, AMIs, endpoints, public IPv4."""

from datetime import datetime

from detector.sweep import SweepContext, collect, safe
from shared.findings import ErrorFinding, Finding
from shared.marking import age_days, is_kept, is_lab, is_lab_tagged, tag_value
from shared.pricing import monthly_usd
from shared.settings import Settings

NOT_TERMINATED_STATES = ["pending", "running", "shutting-down", "stopping", "stopped"]


def detect_ec2(
    region: str, ctx: SweepContext, findings: list[Finding], errors: list[ErrorFinding]
) -> None:
    """Inspect the EC2 family in ``region``, isolating every API call in its own ``safe`` block."""
    ami_snapshots: dict[str, str] = {}
    images_complete = False

    def ec2():
        # Fetched inside each safe block so a client-creation failure is recorded, not raised.
        return ctx.clients.get("ec2", region)

    def run_instances() -> None:
        filters = [{"Name": "instance-state-name", "Values": NOT_TERMINATED_STATES}]
        for reservation in collect(ec2(), "describe_instances", "Reservations", Filters=filters):
            for instance in reservation.get("Instances", []):
                findings.append(_instance(instance, region, ctx))

    def run_nat_gateways() -> None:
        state = [{"Name": "state", "Values": ["available"]}]
        for nat in collect(ec2(), "describe_nat_gateways", "NatGateways", Filter=state):
            findings.append(_nat_gateway(nat, region, ctx))

    def run_addresses() -> None:
        for address in collect(ec2(), "describe_addresses", "Addresses"):
            findings.append(_address(address, region, ctx))

    def run_volumes() -> None:
        for volume in collect(ec2(), "describe_volumes", "Volumes"):
            if not volume.get("Attachments"):
                findings.append(_volume(volume, region, ctx))

    def run_images() -> None:
        nonlocal images_complete
        images = collect(ec2(), "describe_images", "Images", Owners=["self"])
        # Complete the map before building findings so one malformed image cannot truncate it.
        for image in images:
            for mapping in image.get("BlockDeviceMappings", []):
                snapshot_id = mapping.get("Ebs", {}).get("SnapshotId")
                if snapshot_id:
                    ami_snapshots[snapshot_id] = image["ImageId"]
        findings.extend(_image(image, region, ctx) for image in images)
        images_complete = True

    def run_snapshots() -> None:
        # Fail closed: without a complete AMI map no snapshot can be proven unreferenced.
        known = ami_snapshots if images_complete else None
        for snapshot in collect(ec2(), "describe_snapshots", "Snapshots", OwnerIds=["self"]):
            findings.append(_snapshot(snapshot, known, region, ctx))

    def run_vpc_endpoints() -> None:
        for endpoint in collect(ec2(), "describe_vpc_endpoints", "VpcEndpoints"):
            if endpoint.get("VpcEndpointType") == "Interface":
                findings.append(_vpc_endpoint(endpoint, region, ctx))

    def run_public_ipv4() -> None:
        # ENIs whose public IP carries an AllocationId are Elastic IPs, already reported.
        enis = collect(ec2(), "describe_network_interfaces", "NetworkInterfaces")
        count = sum(
            1
            for eni in enis
            if eni.get("Association", {}).get("PublicIp")
            and not eni["Association"].get("AllocationId")
        )
        if count:
            findings.append(
                _finding(
                    region,
                    "public-ipv4",
                    f"public-ipv4-{region}",
                    [],
                    ctx.settings,
                    detail=f"{count} auto-assigned public IPv4 addresses",
                    estimated_monthly_usd=monthly_usd("public-ipv4", count),
                )
            )

    safe(ctx, errors, region, "ec2:DescribeInstances", run_instances)
    safe(ctx, errors, region, "ec2:DescribeNatGateways", run_nat_gateways)
    safe(ctx, errors, region, "ec2:DescribeAddresses", run_addresses)
    safe(ctx, errors, region, "ec2:DescribeVolumes", run_volumes)
    safe(ctx, errors, region, "ec2:DescribeImages", run_images)
    safe(ctx, errors, region, "ec2:DescribeSnapshots", run_snapshots)
    safe(ctx, errors, region, "ec2:DescribeVpcEndpoints", run_vpc_endpoints)
    safe(ctx, errors, region, "ec2:DescribeNetworkInterfaces", run_public_ipv4)


def _finding(
    region: str, kind: str, resource_id: str, tags: list[dict], settings: Settings, **fields
) -> Finding:
    """Build a Finding; ``fields`` carries ``detail`` plus any non-default verdict fields."""
    defaults = {"candidate": False, "reason": "", "age_days": None, "estimated_monthly_usd": None}
    return Finding(
        region=region,
        kind=kind,
        resource_id=resource_id,
        arn=None,
        lab=is_lab(tag_value(tags, "Name"), tags, settings),
        tags=tags,
        **(defaults | fields),
    )


def _instance(instance: dict, region: str, ctx: SweepContext) -> Finding:
    """EC2 instance; reported for visibility, never a candidate."""
    tags = instance.get("Tags", [])
    parts = [instance.get("InstanceType", ""), instance["State"]["Name"]]
    parts += [
        value for value in (tag_value(tags, "Name"), instance.get("PublicIpAddress")) if value
    ]
    age = age_days(instance.get("LaunchTime"), ctx.now)
    return _finding(
        region,
        "ec2-instance",
        instance["InstanceId"],
        tags,
        ctx.settings,
        detail=" ".join(parts),
        age_days=age,
    )


def _nat_gateway(nat: dict, region: str, ctx: SweepContext) -> Finding:
    """Available NAT gateway with its fixed hourly price; never a candidate."""
    return _finding(
        region,
        "nat-gateway",
        nat["NatGatewayId"],
        nat.get("Tags", []),
        ctx.settings,
        detail=f"NAT gateway in {nat.get('VpcId', '?')}",
        age_days=age_days(nat.get("CreateTime"), ctx.now),
        estimated_monthly_usd=monthly_usd("nat-gateway"),
    )


def _address(address: dict, region: str, ctx: SweepContext) -> Finding:
    """Elastic IP; an idle lab EIP becomes a candidate only once proposed in a previous live run."""
    settings = ctx.settings
    tags = address.get("Tags", [])
    allocation_id = address.get("AllocationId")
    associated = bool(address.get("AssociationId"))
    candidate, reason = False, ""
    if is_lab_tagged(tags, settings):
        blocker = _blocker(tags, settings, "associated" if associated else None)
        if blocker is None and allocation_id and not ctx.was_proposed_live(allocation_id):
            blocker = "not proposed in a previous live run"
        # Legacy addresses without an AllocationId are never candidates.
        candidate = blocker is None and allocation_id is not None
        reason = "idle, tagged lab, proposed in a previous live run" if candidate else blocker or ""
    return _finding(
        region,
        "elastic-ip",
        allocation_id or address["PublicIp"],
        tags,
        settings,
        detail=f"{address.get('PublicIp', '?')} ({'associated' if associated else 'idle'})",
        candidate=candidate,
        reason=reason,
        estimated_monthly_usd=monthly_usd("public-ipv4"),
    )


def _volume(volume: dict, region: str, ctx: SweepContext) -> Finding:
    """Unattached EBS volume; candidate when lab-tagged, available, old enough and not kept."""
    settings = ctx.settings
    tags = volume.get("Tags", [])
    state = volume.get("State", "")
    size = volume.get("Size", 0)
    age = age_days(volume.get("CreateTime"), ctx.now)
    candidate, reason = _aged_verdict(
        tags,
        settings,
        age,
        None if state == "available" else _state_reason(state),
        f"{_lab_tag(settings)}, available, {age} days old",
    )
    return _finding(
        region,
        "ebs-volume",
        volume["VolumeId"],
        tags,
        settings,
        detail=f"{size} GiB {volume.get('VolumeType', '')} {state}",
        candidate=candidate,
        reason=reason,
        age_days=age,
        estimated_monthly_usd=monthly_usd("ebs-gp3-gib", size),
    )


def _image(image: dict, region: str, ctx: SweepContext) -> Finding:
    """Self-owned AMI; never a candidate."""
    return _finding(
        region,
        "ami",
        image["ImageId"],
        image.get("Tags", []),
        ctx.settings,
        detail=image.get("Name", ""),
        age_days=age_days(_parse_iso(image.get("CreationDate")), ctx.now),
    )


def _snapshot(
    snapshot: dict, ami_snapshots: dict[str, str] | None, region: str, ctx: SweepContext
) -> Finding:
    """Self-owned snapshot; never a candidate when it backs an AMI or AMI references are unknown.

    ``ami_snapshots`` is None when ``describe_images`` did not complete.
    """
    settings = ctx.settings
    tags = snapshot.get("Tags", [])
    size = snapshot.get("VolumeSize", 0)
    age = age_days(snapshot.get("StartTime"), ctx.now)
    detail = f"{size} GiB from {snapshot.get('VolumeId', '?')}"
    if ami_snapshots is None:
        ami_blocker = "AMI references unknown"
    else:
        image_id = ami_snapshots.get(snapshot["SnapshotId"])
        ami_blocker = f"backs AMI {image_id}" if image_id else None
        if ami_blocker:
            detail = f"{detail}, {ami_blocker}"
    candidate, reason = _aged_verdict(
        tags,
        settings,
        age,
        ami_blocker,
        f"{_lab_tag(settings)}, {age} days old, not referenced by any AMI",
    )
    return _finding(
        region,
        "ebs-snapshot",
        snapshot["SnapshotId"],
        tags,
        settings,
        detail=detail,
        candidate=candidate,
        reason=reason,
        age_days=age,
        estimated_monthly_usd=monthly_usd("ebs-snapshot-gib", size),
    )


def _vpc_endpoint(endpoint: dict, region: str, ctx: SweepContext) -> Finding:
    """Interface VPC endpoint, priced per AZ (one per subnet); never a candidate."""
    azs = len(endpoint.get("SubnetIds", []))
    return _finding(
        region,
        "vpc-endpoint",
        endpoint["VpcEndpointId"],
        endpoint.get("Tags", []),
        ctx.settings,
        detail=f"{endpoint.get('ServiceName', '?')} in {azs} AZ(s)",
        age_days=age_days(endpoint.get("CreationTimestamp"), ctx.now),
        estimated_monthly_usd=monthly_usd("vpc-endpoint-interface-az", azs),
    )


def _lab_tag(settings: Settings) -> str:
    """Human-readable lab tag, e.g. ``tag Purpose=lab``."""
    return f"tag {settings.tag_key}={settings.tag_value}"


def _blocker(tags: list[dict], settings: Settings, *checks: str | None) -> str | None:
    """First reason that blocks a lab-tagged resource: the keep tag, then ``checks`` in order."""
    if is_kept(tags, settings):
        return f"tagged {settings.keep_tag_key}={settings.keep_tag_value}"
    return next((check for check in checks if check), None)


def _aged_verdict(
    tags: list[dict],
    settings: Settings,
    age: int | None,
    blocker: str | None,
    candidate_reason: str,
) -> tuple[bool, str]:
    """(candidate, reason) for resources that also need the minimum age (volumes, snapshots)."""
    if not is_lab_tagged(tags, settings):
        return False, ""
    too_young = age is None or age < settings.min_age_days
    age_blocker = f"below minimum age ({age} days)" if too_young else None
    reason = _blocker(tags, settings, blocker, age_blocker)
    return (False, reason) if reason else (True, candidate_reason)


def _state_reason(state: str) -> str:
    """Reason for an unattached volume whose state is not ``available``."""
    return "attached" if state == "in-use" else f"state {state}"


def _parse_iso(value: str | None) -> datetime | None:
    """Parse an ISO-8601 timestamp string such as an AMI ``CreationDate``."""
    return datetime.fromisoformat(value) if value else None
