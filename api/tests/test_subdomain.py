import pytest

from open_worlds.common.subdomain import InvalidSubdomain, validate_subdomain


def test_valid_subdomain_is_normalized():
    assert validate_subdomain("  My-World  ") == "my-world"
    assert validate_subdomain("world123") == "world123"


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "-leading",
        "trailing-",
        "under_score",
        "space bar",
        "UPPER!",
        "a" * 64,
        "world.dotted",
    ],
)
def test_invalid_subdomains_rejected(bad):
    with pytest.raises(InvalidSubdomain):
        validate_subdomain(bad)


@pytest.mark.parametrize(
    "reserved", ["www", "api", "admin", "servers", "palworld", "pal", "worlds", "waker"]
)
def test_reserved_subdomains_rejected(reserved):
    with pytest.raises(InvalidSubdomain):
        validate_subdomain(reserved)


def test_max_length_boundary():
    assert validate_subdomain("a" * 63) == "a" * 63
