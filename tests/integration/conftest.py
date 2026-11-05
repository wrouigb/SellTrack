import pytest


@pytest.fixture(autouse=True)
def _every_test_cleans_up(clean_state):
    """Every integration test leaves the database and Redis empty for the next one."""
