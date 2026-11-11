from botocore.stub import Stubber

from shared.aws_clients import ClientFactory


def test_client_config_and_cache():
    f = ClientFactory()
    c1, c2 = f.get("ec2", "us-east-1"), f.get("ec2", "us-east-1")
    assert c1 is c2
    assert c1.meta.config.connect_timeout == 5 and c1.meta.config.read_timeout == 20
    # botocore stores max_attempts=3 as total_max_attempts=4 on the built client
    assert c1.meta.config.retries == {"mode": "standard", "total_max_attempts": 4}


def test_enabled_regions_are_sorted_and_ask_only_for_enabled_ones():
    factory = ClientFactory()
    ec2 = factory.get("ec2", "us-east-1")
    with Stubber(ec2) as stubber:
        stubber.add_response(
            "describe_regions",
            {"Regions": [{"RegionName": "us-west-2"}, {"RegionName": "eu-west-1"}]},
            # AllRegions=False: a region that is not opted in cannot be swept.
            {"AllRegions": False},
        )

        assert factory.enabled_regions() == ["eu-west-1", "us-west-2"]
        stubber.assert_no_pending_responses()
