"""Contract conformance: the implemented routes must match openapi.json exactly,
and every documented response status must be producible by the implementation."""
import json
from pathlib import Path

from shortener.config import Settings
from shortener.errors import ShortenerError
from shortener.http import ROUTES

# The generated OpenAPI contract at the project root (two folders above this file).
SPEC = json.loads((Path(__file__).resolve().parents[2] / "openapi.json").read_text(encoding="utf-8"))


# (METHOD, path template) -> operation object, for every operation in the spec.
def _spec_operations():
    return {(m.upper(), path): op for path, item in SPEC["paths"].items()
            for m, op in item.items() if m in ("get", "post", "put", "delete", "patch")}


def test_every_contract_operation_is_implemented_and_vice_versa():
    """Covers: AC-CONTRACT"""
    # Set equality catches both a missing route and an undocumented one.
    implemented = {(r.method, r.template) for r in ROUTES}
    assert implemented == set(_spec_operations())


def test_operation_ids_match():
    """Covers: AC-CONTRACT"""
    ops = _spec_operations()
    for r in ROUTES:
        assert ops[(r.method, r.template)]["operationId"] == r.operation_id


def test_documented_error_statuses_exist_in_error_model():
    """Covers: AC-CONTRACT"""
    # Every error class, at any depth of inheritance.
    def subclasses(cls):
        for sub in cls.__subclasses__():
            yield sub
            yield from subclasses(sub)

    # Statuses the code can return: error classes plus the success, 405 and 500 paths.
    producible = {c.status for c in subclasses(ShortenerError)} | {200, 201, 204, 302, 405, 500}
    # The spec may only document statuses the implementation can actually produce.
    for (_method, _path), op in _spec_operations().items():
        for status in op["responses"]:
            assert int(status) in producible, f"{status} documented but never produced"


def test_secured_operations_match_auth_flags():
    """Covers: AC-CONTRACT, AC-ABUSE"""
    ops = _spec_operations()
    # The spec marks operations as secured only when the generated defaults require keys.
    keys_required = Settings().require_api_key
    for r in ROUTES:
        assert bool(ops[(r.method, r.template)].get("security")) == (r.auth and keys_required)
