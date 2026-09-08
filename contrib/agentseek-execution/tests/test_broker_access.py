from dataclasses import replace

import pytest
from agentseek_execution.broker_access import AccessGate, CredentialRegistry, Operation, Principal, Resource
from agentseek_execution.models import Code, ContractError, Scope

KEY_A = "test-only-a-" + "0" * 32
KEY_B = "test-only-b-" + "1" * 32


class MemoryOwnership:
    """Deliberately overbroad list results exercise defensive filtering."""

    def __init__(self, resources):
        self.resources = {item.resource_id: item for item in resources}
        self.lookups = 0

    def get(self, resource_id):
        self.lookups += 1
        return self.resources.get(resource_id)

    def owned(self, principal_id):
        return tuple(self.resources.values())


@pytest.fixture
def gate():
    a = Scope("tenant", "de", "chat-a", "alice")
    b = Scope("tenant", "de", "chat-b", "bob")
    credentials = CredentialRegistry()
    credentials.register(KEY_A, Principal("service-a", frozenset({a})))
    credentials.register(KEY_B, Principal("service-b", frozenset({b})))
    ownership = MemoryOwnership([
        Resource("public-a", "service-a", a, "private-cube-a"),
        Resource("public-b", "service-b", b, "private-cube-b"),
    ])
    return AccessGate(credentials, ownership)


@pytest.mark.parametrize("key", ["", "bad", "z" * 32, "界" * 32, "a" * 257])
def test_invalid_credentials_do_not_query_resource_store(gate, key):
    with pytest.raises(ContractError) as error:
        gate.resource(key, "public-a", Operation.EXECUTE)
    assert error.value.code == Code.DENIED
    assert gate.ownership.lookups == 0
    assert str(error.value) == "authorization_denied"


@pytest.mark.parametrize("operation", list(Operation))
@pytest.mark.parametrize("key,own,foreign", [(KEY_A, "public-a", "public-b"), (KEY_B, "public-b", "public-a")])
def test_cross_identity_operations_denied(gate, operation, key, own, foreign):
    assert gate.resource(key, own, operation).resource_id == own
    for identifier in (foreign, "missing", "private-cube-a", "private-cube-b"):
        with pytest.raises(ContractError) as error:
            gate.resource(key, identifier, operation)
        assert str(error.value) == "authorization_denied"


def test_lists_are_filtered_even_if_repository_returns_other_owners(gate):
    assert gate.list_ids(KEY_A) == ("public-a",)
    assert gate.list_ids(KEY_B) == ("public-b",)


@pytest.mark.parametrize("dimension", ["tenant", "digital_employee", "conversation", "requester"])
def test_create_scope_cannot_be_forged(gate, dimension):
    original = gate.ownership.resources["public-a"].scope
    principal, authorized = gate.creation_scope(KEY_A, original)
    assert principal.principal_id == "service-a" and authorized == original
    with pytest.raises(ContractError):
        gate.creation_scope(KEY_A, replace(original, **{dimension: "forged"}))


def test_revocation_applies_to_each_operation(gate):
    gate.resource(KEY_A, "public-a", Operation.READ)
    gate.credentials.revoke(KEY_A)
    with pytest.raises(ContractError):
        gate.resource(KEY_A, "public-a", Operation.READ)
    with pytest.raises(ContractError):
        gate.list_ids(KEY_A)
    assert gate.list_ids(KEY_B) == ("public-b",)


def test_principal_id_alone_does_not_grant_another_scope(gate):
    old = gate.ownership.resources["public-a"]
    gate.ownership.resources[old.resource_id] = replace(old, scope=replace(old.scope, conversation="other"))
    with pytest.raises(ContractError):
        gate.resource(KEY_A, old.resource_id, Operation.DELETE)
    assert gate.list_ids(KEY_A) == ()


def test_credential_cannot_be_reassigned(gate):
    principal = gate.credentials.authenticate(KEY_B)
    with pytest.raises(ContractError) as error:
        gate.credentials.register(KEY_A, principal)
    assert error.value.code == Code.CONFLICT


def test_private_handles_not_in_repr_and_unsupported_operations_deny(gate):
    resource = gate.resource(KEY_A, "public-a", Operation.READ)
    assert "private-cube-a" not in repr(resource)
    assert KEY_A not in repr(gate.credentials)
    with pytest.raises(ContractError):
        gate.resource(KEY_A, "public-a", "raw_proxy")
    with pytest.raises(ContractError):
        gate.volume(KEY_A)
