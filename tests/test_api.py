"""The HTTP surface, checked without a database, a model or a network.

These deliberately stop at the edge of the application. Everything here is true
of the app before any run exists: which routes are mounted, what an unauthenticated
request gets, what the validator rejects. The moment a test needs a row it needs
Postgres, an `auth.users` entry and a real token, and that belongs in
`scripts/prove_api.py` rather than in a suite that has to stay fast and offline.

The dividing line is worth stating because it is easy to get backwards. What is
checked below is the part most likely to be wrong and cheapest to check — a route
renamed, a dependency accidentally dropped off an endpoint so it stops requiring a
token. What is *not* checked is whether a real run works, which no amount of
mocking would establish anyway.

`ASGITransport` is used rather than `TestClient` for one specific reason: it does
not run the lifespan. That is what keeps these tests offline, since the lifespan's
first act is to open a connection pool to Supabase.
"""

from __future__ import annotations

import httpx
import jwt
import pytest

from repo_cartographer.api import brief, create_app
from repo_cartographer.auth import ALGORITHMS, AUDIENCE

# Every route that requires a signed-in user, as (method, path). A token-less
# request to each must be refused. Written out rather than derived from the app,
# because deriving it from the same object under test would make the assertion
# vacuous — if a dependency fell off an endpoint, a derived list would simply stop
# including it and the test would still pass.
PROTECTED = [
    ("GET", "/runs"),
    ("GET", "/runs/8ac1e6e0-0000-4000-8000-000000000000"),
    ("GET", "/runs/8ac1e6e0-0000-4000-8000-000000000000/guide"),
    ("POST", "/runs"),
    ("POST", "/runs/8ac1e6e0-0000-4000-8000-000000000000/approve"),
]


@pytest.fixture
def anyio_backend():
    """Run the async tests on asyncio alone, not the whole anyio matrix."""
    return "asyncio"


@pytest.fixture
def app():
    return create_app()


@pytest.fixture
async def client(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest.mark.anyio
async def test_health_needs_no_token(client):
    """A load balancer has no credentials, so this one must be open."""
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.anyio
@pytest.mark.parametrize(("method", "path"), PROTECTED)
async def test_every_other_route_requires_a_token(client, method: str, path: str):
    response = await client.request(method, path, json={})
    assert response.status_code == 401, f"{method} {path} answered {response.status_code}"


@pytest.mark.anyio
@pytest.mark.parametrize(
    "header",
    ["", "Bearer", "Basic abc", "Bearer ", "not-a-scheme token"],
)
async def test_a_malformed_authorization_header_is_refused(client, header: str):
    response = await client.get("/runs", headers={"Authorization": header})
    assert response.status_code == 401


def test_algorithm_pinning_rejects_an_unsigned_token():
    """The algorithm-confusion case, checked without the key set.

    A token whose header says `alg: none` carries no signature at all. A verifier
    that trusts the token's own claim about how it was signed will accept it and
    every claim inside it. `auth.py` passes a fixed `algorithms=["ES256"]`, which
    is what makes that impossible — and that is a property of the decode call
    rather than of the network, so it is checkable here rather than only against
    a live JWKS endpoint.
    """
    # header {"alg":"none","typ":"JWT"} . payload {"sub":..., "aud":"authenticated"} . (no sig)
    forged = (
        "eyJhbGciOiJub25lIiwidHlwIjoiSldUIn0."
        "eyJzdWIiOiI4YWMxZTZlMC0wMDAwLTQwMDAtODAwMC0wMDAwMDAwMDAwMDAiLCJhdWQiOiJhdXRoZW50aWNhdGVkIn0."
    )
    assert "none" not in ALGORITHMS
    with pytest.raises(jwt.PyJWTError):
        jwt.decode(forged, key="", algorithms=ALGORITHMS, audience=AUDIENCE)


def test_the_expected_routes_are_mounted(app):
    paths = {route.path for route in app.routes}
    assert {
        "/health",
        "/runs",
        "/runs/{run_id}",
        "/runs/{run_id}/guide",
        "/runs/{run_id}/approve",
    } <= paths


def test_the_brief_names_the_repository():
    """The agent must never be left inferring which repository it is asked about."""
    composed = brief("pallets/flask", "How does routing work?")
    assert "pallets/flask" in composed
    assert "How does routing work?" in composed
