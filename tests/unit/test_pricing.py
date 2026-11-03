from shared.pricing import monthly_usd


def test_monthly_usd():
    assert monthly_usd("nat-gateway") == 32.85
    assert monthly_usd("public-ipv4", 3) == 10.95
    assert monthly_usd("unknown") is None
