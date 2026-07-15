"""
infrastructure/auth/jwt_auth.py
=================================
Sprint 3 — Auth Module.
JWT-based authentication with access + refresh tokens.
RBAC enforced via FastAPI dependencies.
Keycloak deferred to V2.

Token structure:
  Access token:  short-lived (1h), carries user_id + role
  Refresh token: long-lived (7d), used only to issue new access tokens
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

import structlog
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from pydantic import BaseModel

from core.config.settings import get_settings

log = structlog.get_logger(__name__)

_pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
_bearer      = HTTPBearer(auto_error=True)


# ── Schemas ───────────────────────────────────────────────────

class TokenPair(BaseModel):
    access_token:  str
    refresh_token: str
    token_type:    str = "bearer"
    expires_in:    int       # access token TTL in seconds


class TokenPayload(BaseModel):
    sub:  str            # user_id
    role: str
    type: str            # "access" | "refresh"
    exp:  int
    iat:  int


class RegisterRequest(BaseModel):
    email:     str
    password:  str
    full_name: Optional[str] = None
    role:      str = "developer"


class LoginRequest(BaseModel):
    email:    str
    password: str


# ── Password utilities ────────────────────────────────────────

def hash_password(plain: str) -> str:
    return _pwd_context.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    return _pwd_context.verify(plain, hashed)


# ── Token creation ────────────────────────────────────────────

def create_access_token(user_id: str, role: str) -> str:
    settings = get_settings()
    now = datetime.now(timezone.utc)
    payload = {
        "sub":  user_id,
        "role": role,
        "type": "access",
        "iat":  int(now.timestamp()),
        "exp":  int((now + timedelta(seconds=settings.jwt_access_token_ttl)).timestamp()),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def create_refresh_token(user_id: str, role: str) -> str:
    settings = get_settings()
    now = datetime.now(timezone.utc)
    payload = {
        "sub":  user_id,
        "role": role,
        "type": "refresh",
        "iat":  int(now.timestamp()),
        "exp":  int((now + timedelta(seconds=settings.jwt_refresh_token_ttl)).timestamp()),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def create_token_pair(user_id: str, role: str) -> TokenPair:
    settings = get_settings()
    return TokenPair(
        access_token=create_access_token(user_id, role),
        refresh_token=create_refresh_token(user_id, role),
        expires_in=settings.jwt_access_token_ttl,
    )


# ── Token verification ────────────────────────────────────────

def decode_token(token: str) -> TokenPayload:
    """
    Decodes and validates a JWT. Raises HTTPException on invalid/expired tokens.
    """
    settings = get_settings()
    try:
        raw = jwt.decode(token, settings.jwt_secret,
                         algorithms=[settings.jwt_algorithm])
        return TokenPayload(**raw)
    except JWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid or expired token: {exc}",
            headers={"WWW-Authenticate": "Bearer"},
        )


# ── FastAPI dependencies ───────────────────────────────────────

async def get_current_user_id(
    credentials: HTTPAuthorizationCredentials = Depends(_bearer),
) -> str:
    """
    FastAPI dependency. Returns the current user's ID from the access token.

    Usage:
        @router.get("/me")
        async def get_me(user_id: str = Depends(get_current_user_id)):
            ...
    """
    payload = decode_token(credentials.credentials)
    if payload.type != "access":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Expected access token",
        )
    return payload.sub


async def get_current_user_role(
    credentials: HTTPAuthorizationCredentials = Depends(_bearer),
) -> str:
    """FastAPI dependency. Returns the current user's role."""
    payload = decode_token(credentials.credentials)
    return payload.role


class RequireRole:
    """
    FastAPI dependency factory for RBAC.

    Usage:
        @router.post("/admin/action")
        async def admin_action(
            user_id: str = Depends(RequireRole("admin", "owner"))
        ):
            ...
    """
    def __init__(self, *allowed_roles: str):
        self.allowed = set(allowed_roles)

    async def __call__(
        self,
        credentials: HTTPAuthorizationCredentials = Depends(_bearer),
    ) -> str:
        payload = decode_token(credentials.credentials)
        if payload.type != "access":
            raise HTTPException(status_code=401, detail="Expected access token")
        if payload.role not in self.allowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Role '{payload.role}' is not permitted. Required: {self.allowed}",
            )
        return payload.sub
