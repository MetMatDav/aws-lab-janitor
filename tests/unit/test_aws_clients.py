from shared.aws_clients import ClientFactory


def test_client_config_and_cache():
    f = ClientFactory()
    c1, c2 = f.get("ec2", "us-east-1"), f.get("ec2", "us-east-1")
    assert c1 is c2
    assert c1.meta.config.connect_timeout == 5 and c1.meta.config.read_timeout == 20
    # botocore stores max_attempts=3 as total_max_attempts=4 on the built client
    assert c1.meta.config.retries == {"mode": "standard", "total_max_attempts": 4}
