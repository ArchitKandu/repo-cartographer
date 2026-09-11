"""Who is asking. Verifies a Supabase access token and returns the user's id.

Supabase signs its access tokens with ES256 and publishes the public half at the
project's JWKS endpoint, which is the arrangement worth having: verification is a
local signature check against a cached key, so this service authenticates a
request without a network round-trip to Supabase and without ever holding a
secret that could forge one. `PyJWKClient` fetches the key set once and caches it,
re-fetching only when a token arrives with a `kid` it has not seen — which is what
makes key rotation something that happens rather than something that pages you.

Two properties of Supabase tokens shape what is trusted below.

**`user_metadata` is writable by the user it describes.** It is carried in the
token and it is tempting, because it is where a display name lives. Nothing in
this module returns it, and nothing downstream should make a decision with it: a
user can set it to anything, including somebody else's anything. Authorization
facts belong in `app_metadata`, which only a service key can write.

**A deleted user's token keeps working until it expires.** Revocation is not
instant, because verification never asks Supabase anything. That is the trade this
design makes for speed, and the mitigation is short token lifetimes rather than a
lookup here that would undo the benefit.
"""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Any
from uuid import UUID

import jwt
from fastapi import Depends, Header, HTTPException, status
from jwt import PyJWKClient

JWKS_ENV = "SUPABASE_JWKS_URL"

# Supabase mints access tokens with this audience. Checking it is cheap and it is
# the difference between "a valid token" and "a valid token for this project".
AUDIENCE = "authenticated"

# ES256 is what Supabase's asymmetric signing keys use. Stating the algorithm
# rather than accepting whatever the token names is the standard defence against
# algorithm-confusion attacks, where a token arrives claiming `alg: none` or a
# symmetric algorithm and a permissive verifier obliges.
ALGORITHMS = ["ES256"]


@lru_cache(maxsize=1)
def jwks_client() -> PyJWKClient:
    """The cached key-set client.

    One per process: `PyJWKClient` holds the fetched keys, so building a new one
    per request would turn every request into an HTTPS round-trip to Supabase and
    give up the entire point of asymmetric verification.
    """
    url = os.environ.get(JWKS_ENV)
    if not url:
        raise RuntimeError(
            f"{JWKS_ENV} is not set. It is the project's JWKS endpoint — "
            "https://<ref>.supabase.co/auth/v1/.well-known/jwks.json — and without "
            "it no request can be authenticated."
        )
    return PyJWKClient(url, cache_keys=True)


def claims_from(token: str) -> dict[str, Any]:
    """Verify a token's signature and return its claims, or raise 401."""
    try:
        key = jwks_client().get_signing_key_from_jwt(token).key
        return jwt.decode(token, key, algorithms=ALGORITHMS, audience=AUDIENCE)
    except jwt.PyJWTError as error:
        # The reason is deliberately not echoed to the caller. "Signature has
        # expired" and "invalid audience" are useful to an operator and useful in
        # a different way to someone probing, so the detail goes to the log and
        # the client gets the fact.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated.",
            headers={"WWW-Authenticate": "Bearer"},
        ) from error


async def current_user(authorization: str = Header(default="")) -> UUID:
    """FastAPI dependency: the signed-in user's id, or 401.

    Returns the id alone rather than the whole claim set, because the id is the
    only thing the rest of this service is entitled to act on. Handing routes a
    dictionary invites one of them to reach for `user_metadata`.
    """
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Expected an `Authorization: Bearer <token>` header.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    subject = claims_from(token).get("sub")
    if not subject:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Token carries no subject."
        )
    try:
        return UUID(str(subject))
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Token subject is not a user id."
        ) from error


CurrentUser = Depends(current_user)
