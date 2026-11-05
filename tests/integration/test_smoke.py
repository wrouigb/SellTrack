import pytest

from tests.factories import UserFactory

pytestmark = pytest.mark.integration


def test_healthz(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json == {"status": "ok"}


def test_factory_creates_a_user(ctx):
    assert UserFactory().id > 0
