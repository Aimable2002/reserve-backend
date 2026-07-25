"""Verifies the Supabase Auth JWT sent by the frontend as
`Authorization: Bearer <access_token>`, and exposes the authenticated user
id to route handlers via `g.user_id`.

Your Supabase project signs tokens with an ECC (P-256) key, i.e. ES256 —
asymmetric signing. That means there is no shared secret to configure here
at all: verification happens against the project's public JWKS endpoint
(https://<project-ref>.supabase.co/auth/v1/.well-known/jwks.json), which is
not sensitive and doesn't need to be kept secret — it only publishes public
keys, never the private key Supabase itself uses to sign tokens.

PyJWKClient caches the fetched keys and handles Supabase's key rotation
(`kid` header lookup) automatically, so there's no manual key management
here either.
"""
import functools

import jwt
from flask import g, jsonify, request

from app.config import config

_jwks_client = None


def _get_jwks_client():
    global _jwks_client
    if _jwks_client is None:
        _jwks_client = jwt.PyJWKClient(config.JWKS_URL)
    return _jwks_client


def _decode_token(token: str) -> dict:
    signing_key = _get_jwks_client().get_signing_key_from_jwt(token)
    return jwt.decode(
        token,
        signing_key.key,
        algorithms=["ES256"],
        audience=config.SUPABASE_JWT_AUDIENCE,
    )


def require_auth(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        header = request.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            return jsonify(error="missing_token", message="Authorization: Bearer <token> header required"), 401

        token = header.split(" ", 1)[1].strip()
        try:
            claims = _decode_token(token)
        except jwt.ExpiredSignatureError:
            # The frontend should refresh the session and retry rather than
            # sending an expired access_token in the first place — see
            # README "Auth" section for the supabase-js call that does this.
            return jsonify(error="token_expired", message="Session expired, please sign in again"), 401
        except jwt.PyJWTError as exc:
            return jsonify(error="invalid_token", message=str(exc)), 401

        user_id = claims.get("sub")
        if not user_id:
            return jsonify(error="invalid_token", message="Token has no subject"), 401

        g.user_id = user_id
        g.user_claims = claims
        return fn(*args, **kwargs)

    return wrapper
