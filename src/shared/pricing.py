"""Fixed on-demand prices used to estimate the monthly cost of idle resources."""

PRICES_AS_OF = "2026-11"
PRICES_REGION = "us-east-1"

HOURS_PER_MONTH = 730

# USD per hour (per GiB-hour for storage kinds, per AZ-hour for interface endpoints).
FIXED_HOURLY_USD: dict[str, float] = {
    "nat-gateway": 0.045,
    "public-ipv4": 0.005,
    "alb": 0.0225,
    "nlb": 0.0225,
    "ebs-gp3-gib": 0.08 / 730,
    "ebs-snapshot-gib": 0.05 / 730,
    "vpc-endpoint-interface-az": 0.01,
}


def monthly_usd(kind: str, units: float = 1.0) -> float | None:
    """Estimated monthly cost of ``units`` of ``kind``, or None for unknown kinds."""
    hourly = FIXED_HOURLY_USD.get(kind)
    if hourly is None:
        return None
    return round(hourly * HOURS_PER_MONTH * units, 2)
