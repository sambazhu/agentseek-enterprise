"""M2.1 trusted Broker boundary, independent of transport and Cube credentials.

Only server composition may construct principals, register resources or select
the ownership repository. This module is not a remotely callable registration API.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass, field
from enum import Enum
from threading import RLock
from typing import Protocol

from .models import Code, ContractError, Scope, identifier, require


class Operation(str, Enum):
    CONNECT = "connect"
    EXECUTE = "execute"
    READ = "read"
    WRITE = "write"
    CANCEL = "cancel"
    DELETE = "delete"


@dataclass(frozen=True)
class Principal:
    principal_id: str
    scopes: frozenset[Scope]

    def __post_init__(self) -> None:
        identifier(self.principal_id)
        require(isinstance(self.scopes, frozenset) and bool(self.scopes))
        require(all(isinstance(scope, Scope) for scope in self.scopes))


@dataclass(frozen=True)
class Resource:
    """Opaque Broker ID mapped to a private backend handle, not a Cube ID API."""

    resource_id: str
    principal_id: str
    scope: Scope
    backend_handle: str = field(repr=False)

    def __post_init__(self) -> None:
        for value in (self.resource_id, self.principal_id, self.backend_handle):
            identifier(value)
        require(isinstance(self.scope, Scope))


class OwnershipStore(Protocol):
    def get(self, resource_id: str) -> Resource | None: ...

    def owned(self, principal_id: str) -> tuple[Resource, ...]: ...


class CredentialRegistry:
    """Small fixed deployment registry; no raw API keys retained after registration.

    High-entropy keys are provisioned out of band. SHA256 is a lookup digest,
    not password hashing. Service keys are never accepted as tenant IDs.
    """

    def __init__(self) -> None:
        self._entries: dict[bytes, Principal] = {}
        self._lock = RLock()

    @staticmethod
    def _digest(key: str) -> bytes:
        require(isinstance(key, str) and 32 <= len(key) <= 256 and key.isascii(), Code.DENIED)
        return hashlib.sha256(key.encode("ascii")).digest()

    def register(self, key: str, principal: Principal) -> None:
        """Trusted provisioning only; cannot overwrite an existing credential."""
        digest = self._digest(key)
        require(isinstance(principal, Principal))
        with self._lock:
            require(digest not in self._entries, Code.CONFLICT)
            self._entries[digest] = principal

    def revoke(self, key: str) -> None:
        with self._lock:
            self._entries.pop(self._digest(key), None)

    def authenticate(self, key: str) -> Principal:
        digest = self._digest(key)
        with self._lock:
            principal = None
            # Full scan avoids early exit by credential position. Fixed small registry.
            for expected, candidate in self._entries.items():
                if hmac.compare_digest(expected, digest):
                    principal = candidate
            if principal is None:
                raise ContractError(Code.DENIED)
            return principal


class AccessGate:
    """Every operation must pass this gate BEFORE contacting a provider.

    returned Resource remains private to the Broker; caller-facing endpoints must
    return only public IDs/results. There is no raw provider proxy escape hatch.
    """

    def __init__(self, credentials: CredentialRegistry, ownership: OwnershipStore):
        self.credentials = credentials
        self.ownership = ownership

    def creation_scope(self, key: str, requested: Scope) -> tuple[Principal, Scope]:
        principal = self.credentials.authenticate(key)
        require(isinstance(requested, Scope) and requested in principal.scopes, Code.DENIED)
        # Return the server-owned value, not a caller's object or body identity.
        scope = next(scope for scope in principal.scopes if scope == requested)
        return principal, scope

    def resource(self, key: str, resource_id: str, operation: Operation) -> Resource:
        principal = self.credentials.authenticate(key)
        require(isinstance(operation, Operation), Code.DENIED)
        require(isinstance(resource_id, str) and 0 < len(resource_id) <= 256, Code.DENIED)
        resource = self.ownership.get(resource_id)
        # Missing and foreign resources share the same externally safe error.
        if resource is None:
            raise ContractError(Code.DENIED)
        require(resource.principal_id == principal.principal_id and resource.scope in principal.scopes, Code.DENIED)
        return resource

    def list_ids(self, key: str) -> tuple[str, ...]:
        principal = self.credentials.authenticate(key)
        # Recheck repository results: a buggy list query must not widen visibility.
        return tuple(
            sorted(
                item.resource_id
                for item in self.ownership.owned(principal.principal_id)
                if item.principal_id == principal.principal_id and item.scope in principal.scopes
            )
        )

    def volume(self, key: str) -> None:
        """M2 initial API does not expose Volume operations; authenticated deny."""
        self.credentials.authenticate(key)
        require(False, Code.DENIED)
