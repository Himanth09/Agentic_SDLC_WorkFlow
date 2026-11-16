"""Contract conformance: the implemented routes must match openapi.json exactly,
and every documented response status must be producible by the implementation."""
import json
from pathlib import Path

from shortener import errors
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


# Errors each operation can raise, read from its handler and the service methods it calls.
# Built from the generated defaults, so errors that only exist when a feature is on
# (API keys, custom aliases) are listed only in that case.
def _possible_errors(s: Settings) -> dict[str, set[type[ShortenerError]]]:
    # Raised by authenticate() only when keys are required.
    auth = {errors.Unauthorized} if s.require_api_key else set()
    return {
        "health": set(),
        # Rate limit, body parsing, validation, alias clash, collision storm.
        "createLink": {errors.RateLimited, errors.BadRequest, errors.PayloadTooLarge,
                       errors.InvalidUrl, errors.InvalidAlias, errors.InvalidTtl,
                       errors.CodeSpaceExhausted}
                      | ({errors.AliasTaken} if s.allow_custom_alias else set()) | auth,
        "getLink": {errors.LinkNotFound},
        "deleteLink": {errors.LinkNotFound} | auth,
        # Bad ?days= value, or unknown link.
        "getLinkStats": {errors.BadRequest, errors.LinkNotFound},
        "redirect": {errors.LinkNotFound, errors.LinkExpired},
    }


def test_every_possible_error_status_is_documented():
    """Covers: AC-CONTRACT"""
    ops = {op["operationId"]: op for op in _spec_operations().values()}
    possible = _possible_errors(Settings())
    # Every route has an entry, so a new operation can't skip this check.
    assert set(possible) == {r.operation_id for r in ROUTES}
    for op_id, errs in possible.items():
        documented = set(ops[op_id]["responses"])
        for err in errs:
            assert str(err.status) in documented, \
                f"{op_id} can raise {err.__name__} ({err.status}) but the contract omits it"


def test_every_error_class_is_mapped_to_an_operation():
    """Covers: AC-CONTRACT"""
    # A new error class must be added to _possible_errors (and so to the contract);
    # otherwise the check above could silently miss it. Internal and routing-level
    # errors are excluded: they aren't specific to one operation.
    def subclasses(cls):
        for sub in cls.__subclasses__():
            yield sub
            yield from subclasses(sub)

    every = {c for c in subclasses(ShortenerError) if not c.__name__.startswith("_")}
    # Map with every feature switched on, so feature-gated errors count as mapped.
    all_on = Settings(require_api_key=True, allow_custom_alias=True, api_keys=frozenset({"k"}))
    mapped = set().union(*_possible_errors(all_on).values())
    assert every <= mapped, f"unmapped error classes: {sorted(c.__name__ for c in every - mapped)}"


def test_secured_operations_match_auth_flags():
    """Covers: AC-CONTRACT, AC-ABUSE"""
    ops = _spec_operations()
    # The spec marks operations as secured only when the generated defaults require keys.
    keys_required = Settings().require_api_key
    for r in ROUTES:
        assert bool(ops[(r.method, r.template)].get("security")) == (r.auth and keys_required)
