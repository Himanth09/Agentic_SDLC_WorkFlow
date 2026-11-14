"""Brownfield change recipes for existing link-shortener codebases.

A recipe never hard-codes file paths. It declares the architectural *roles* it
touches (model / store / service / http); the CodebaseAnalyst resolves roles
to concrete modules and symbols in the target repo. At execution time the
recipe re-reads the *current* workspace file (so serialized recipes compose),
locates symbols with `ast`, and applies transforms that must each match
exactly once - otherwise it raises NeedsHuman instead of guessing.
"""
# Store annotations as strings.
from __future__ import annotations

# ast: locate functions/classes in the target code precisely.
import ast
# re: single-match regex transforms.
import re
# dataclass/field: the Recipe definition.
from dataclasses import dataclass, field
# Path: project and module paths.
from pathlib import Path
# Callable: recipe build function and regex replacement callbacks.
from typing import Callable

# NeedsHuman: raised instead of guessing when code doesn't look as expected.
from ...errors import NeedsHuman
# Acceptance criterion (aliased to AC).
from ...models import AcceptanceCriterion as AC
# FileChange: each edit is an anchor-based change applied by the guardrails.
from ...models import FileChange


# --------------------------------------------------------------------------- #
# Editing helpers
# --------------------------------------------------------------------------- #
# Reads one module of the target project and produces exact, anchor-based edits for it.
class SourceEditor:
    # Load and parse the module.
    def __init__(self, project: Path, rel: str, workspace_prefix: str) -> None:
        # Module path relative to the project.
        self.rel = rel
        # Absolute path to read.
        self.path = project / rel
        # Prefix (e.g. "project/") that maps project paths to workspace paths.
        self.prefix = workspace_prefix
        # Current source text (re-read each time, so earlier recipes' edits are visible).
        self.text = self.path.read_text(encoding="utf-8")
        # Parsed AST.
        self.tree = ast.parse(self.text)

    # Exact source text of the single function/class named `name`.
    def segment(self, name: str, kind: type = ast.FunctionDef) -> str:
        # All nodes of the requested kind with that name.
        hits = [n for n in ast.walk(self.tree) if isinstance(n, kind) and getattr(n, "name", None) == name]
        # Zero or several matches is ambiguous: escalate rather than guess.
        if len(hits) != 1:
            raise NeedsHuman(f"expected exactly one {kind.__name__} '{name}' in {self.rel}, "
                             f"found {len(hits)}")
        # Original source text of that node.
        seg = ast.get_source_segment(self.text, hits[0])
        assert seg is not None
        return seg

    # Text of the last top-level import line (anchor for inserting new code after imports).
    def last_import_line(self) -> str:
        # Top-level import statements only.
        imports = [n for n in self.tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
        if not imports:
            raise NeedsHuman(f"no imports found in {self.rel}")
        # Last one; end_lineno covers multi-line imports.
        last = imports[-1]
        return self.text.splitlines()[(last.end_lineno or last.lineno) - 1] + "\n"

    # Build an edit FileChange, checking the anchor occurs exactly once.
    def change(self, anchor: str, replacement: str, reason: str) -> FileChange:
        if self.text.count(anchor) != 1:
            raise NeedsHuman(f"anchor not unique in {self.rel}")
        return FileChange(path=f"{self.prefix}{self.rel}", op="edit", anchor=anchor,
                          replacement=replacement, reason=reason)


# re.sub that must match exactly once (otherwise escalate to a human).
def sub_once(pattern: str, repl: str | Callable[[re.Match[str]], str], text: str, what: str,
             flags: int = 0) -> str:
    # count=0 replaces all matches so we can detect >1.
    new, n = re.subn(pattern, repl, text, count=0, flags=flags)
    if n != 1:
        raise NeedsHuman(f"transform '{what}' matched {n} times (expected 1)")
    return new


# Declarative description of one brownfield change.
@dataclass
class Recipe:
    # Recipe id.
    id: str
    # Human-readable title.
    title: str
    # Regexes that, found in the requirement, select this recipe.
    patterns: list[str]
    # Architectural roles touched (resolved to modules by the CodebaseAnalyst).
    roles: list[str]
    # Acceptance criteria the change must satisfy.
    criteria: list[AC]
    # build(project, role->module map, workspace prefix) -> list of edits.
    build: Callable[[Path, dict[str, str], str], list[FileChange]]
    # Test files to add: path -> source.
    tests: dict[str, str] = field(default_factory=dict)
    priority: int = 50          # lower runs first when recipes touch the same files
    concepts: list[str] = field(default_factory=list)   # "name" or "role:name" symbol hints


# --------------------------------------------------------------------------- #
# Recipe 1: alias overwrite bug
# --------------------------------------------------------------------------- #
# Make aliases fail with 409 instead of overwriting, and retry random-code collisions.
def _fix_alias_overwrite(project: Path, roles: dict[str, str], prefix: str) -> list[FileChange]:
    # Collected edits.
    changes: list[FileChange] = []

    # --- Store: add a DuplicateCode exception and stop INSERT OR REPLACE from overwriting.
    store = SourceEditor(project, roles["store"], prefix)
    # Insert the new exception class right after the imports.
    imp = store.last_import_line()
    changes.append(store.change(imp, imp + "\n\nclass DuplicateCode(Exception):\n"
                                "    \"\"\"Raised when a code/alias already exists.\"\"\"\n\n",
                                "add DuplicateCode signal"))
    # Current save() source.
    save = store.segment("save")
    # Plain INSERT so the primary key rejects duplicates.
    new_save = sub_once(r"INSERT OR REPLACE INTO", "INSERT INTO", save, "remove OR REPLACE")
    # Wrap the execute() call in try/except IntegrityError -> DuplicateCode (keeps original indentation).
    new_save = sub_once(
        r"(?P<indent>[ ]+)self\._conn\.execute\(\n(?P<body>.*?)\n"
        r"(?P=indent)\)\n(?P=indent)self\._conn\.commit\(\)",
        lambda m: (f"{m['indent']}try:\n{m['indent']}    self._conn.execute(\n"
                   + "\n".join("    " + ln for ln in m["body"].splitlines())
                   + f"\n{m['indent']}    )\n{m['indent']}except sqlite3.IntegrityError as exc:\n"
                   f"{m['indent']}    raise DuplicateCode(link.code) from exc\n"
                   f"{m['indent']}self._conn.commit()"),
        new_save, "wrap insert in IntegrityError handler", flags=re.S)
    # Replace the whole save() method.
    changes.append(store.change(save, new_save, "never overwrite an existing code (bug fix)"))

    # --- Service: raise AliasTaken for taken aliases; retry random codes.
    svc = SourceEditor(project, roles["service"], prefix)
    # Store module name, e.g. "store" from "links/store.py".
    store_mod = Path(roles["store"]).stem
    # The service must import LinkStore from the store module in this exact form.
    imp_line = f"from .{store_mod} import LinkStore\n"
    if imp_line not in svc.text:
        raise NeedsHuman(f"service does not import LinkStore from .{store_mod}")
    # Also import DuplicateCode.
    changes.append(svc.change(imp_line, f"from .{store_mod} import DuplicateCode, LinkStore\n",
                              "import DuplicateCode"))
    # Add an AliasTaken exception after the existing NotFound class.
    nf = svc.segment("NotFound", ast.ClassDef)
    changes.append(svc.change(nf, nf + "\n\n\nclass AliasTaken(Exception):\n"
                              "    \"\"\"The requested alias is already in use.\"\"\"",
                              "add AliasTaken error"))
    # Rewrite code allocation inside shorten().
    shorten = svc.segment("shorten")
    new_shorten = sub_once(
        r"        code = alias or .*?\n        link = Link\(code=code, target_url=url, created_at=self\.clock\(\)\)\n"
        r"        self\.store\.save\(link\)\n        return link",
        "        now = self.clock()\n"
        "        if alias:\n"
        "            link = Link(code=alias, target_url=url, created_at=now)\n"
        "            try:\n"
        "                self.store.save(link)\n"
        "            except DuplicateCode:\n"
        "                raise AliasTaken(alias) from None\n"
        "            return link\n"
        "        for _ in range(5):  # random codes: retry on the rare collision\n"
        "            code = \"\".join(secrets.choice(ALPHABET) for _ in range(6))\n"
        "            link = Link(code=code, target_url=url, created_at=now)\n"
        "            try:\n"
        "                self.store.save(link)\n"
        "                return link\n"
        "            except DuplicateCode:\n"
        "                continue\n"
        "        raise RuntimeError(\"could not allocate a unique code\")",
        shorten, "rewrite code allocation", flags=re.S)
    # Replace shorten().
    changes.append(svc.change(shorten, new_shorten, "aliases fail with AliasTaken; random codes retry"))

    # --- HTTP: map AliasTaken to 409 Conflict.
    http = SourceEditor(project, roles["http"], prefix)
    # Service module name.
    svc_mod = Path(roles["service"]).stem
    # Expected import line in the HTTP module.
    old_imp = f"from .{svc_mod} import LinkService, NotFound\n"
    if old_imp not in http.text:
        raise NeedsHuman("http module import of service not recognised")
    # Also import AliasTaken.
    changes.append(http.change(old_imp, f"from .{svc_mod} import AliasTaken, LinkService, NotFound\n",
                               "import AliasTaken"))
    # Add an `except AliasTaken` branch before the existing ValueError handler.
    create = http.segment("create")
    new_create = sub_once(
        r"(?P<i>[ ]+)except ValueError as exc:\n(?P=i)    return \"400 Bad Request\", \{\"error\": str\(exc\)\}, \[\]",
        lambda m: (f"{m['i']}except AliasTaken:\n{m['i']}    return \"409 Conflict\", "
                   f"{{\"error\": \"alias already in use\"}}, []\n"
                   f"{m['i']}except ValueError as exc:\n{m['i']}    return \"400 Bad Request\", "
                   f"{{\"error\": str(exc)}}, []"),
        create, "map AliasTaken to 409")
    # Replace create().
    changes.append(http.change(create, new_create, "409 Conflict for taken aliases"))
    return changes


# --------------------------------------------------------------------------- #
# Recipe 2: link expiration
# --------------------------------------------------------------------------- #
# Add an optional TTL to links: model field, DB column + migration, service checks, HTTP 410.
def _add_expiration(project: Path, roles: dict[str, str], prefix: str) -> list[FileChange]:
    # Collected edits.
    changes: list[FileChange] = []

    # --- Model: add `expires_at: float | None = None` to the Link dataclass.
    model = SourceEditor(project, roles["model"], prefix)
    cls = model.segment("Link", ast.ClassDef)
    changes.append(model.change(cls, cls.rstrip("\n") + "\n    expires_at: float | None = None\n",
                                "optional expiry timestamp (None = never)"))

    # --- Store: add the column to the schema DDL.
    store = SourceEditor(project, roles["store"], prefix)
    changes.append(store.change("    clicks INTEGER NOT NULL DEFAULT 0\n)",
                                "    clicks INTEGER NOT NULL DEFAULT 0,\n    expires_at REAL\n)",
                                "add expires_at column"))
    # Call a new _migrate() method from __init__ after creating the schema.
    init = store.segment("__init__")
    new_init = sub_once(r"(\n(?P<i>[ ]+)self\._conn\.execute\(SCHEMA\)\n)",
                        lambda m: (m[1] + f"{m['i']}self._migrate()\n"), init, "call migration")
    # Append the _migrate() method: adds the column to old databases if it's missing (idempotent).
    new_init += ("\n\n    def _migrate(self) -> None:\n"
                 "        \"\"\"Backward-compatible, idempotent migration for pre-existing databases.\"\"\"\n"
                 "        cols = {row[1] for row in self._conn.execute(\"PRAGMA table_info(links)\")}\n"
                 "        if \"expires_at\" not in cols:\n"
                 "            self._conn.execute(\"ALTER TABLE links ADD COLUMN expires_at REAL\")")
    changes.append(store.change(init, new_init, "online migration adds expires_at"))
    # save(): include expires_at in the column list, placeholders and bound values.
    save = store.segment("save")
    new_save = sub_once(r"\(code, target_url, created_at, clicks\)",
                        "(code, target_url, created_at, clicks, expires_at)", save, "insert columns")
    new_save = sub_once(r"VALUES \(\?, \?, \?, \?\)", "VALUES (?, ?, ?, ?, ?)", new_save, "placeholders")
    new_save = sub_once(r"link\.clicks\)", "link.clicks, link.expires_at)", new_save, "bound values")
    changes.append(store.change(save, new_save, "persist expires_at"))
    # get(): select expires_at and pass it to the Link constructor.
    get = store.segment("get")
    new_get = sub_once(r"SELECT code, target_url, created_at, clicks FROM",
                       "SELECT code, target_url, created_at, clicks, expires_at FROM", get, "select column")
    new_get = sub_once(r"clicks=row\[3\]\)", "clicks=row[3], expires_at=row[4])", new_get, "map column")
    changes.append(store.change(get, new_get, "load expires_at"))

    # --- Service: add Expired, accept ttl_seconds, and check expiry on resolve.
    svc = SourceEditor(project, roles["service"], prefix)
    nf = svc.segment("NotFound", ast.ClassDef)
    # Add an Expired exception after NotFound.
    changes.append(svc.change(nf, nf + "\n\n\nclass Expired(Exception):\n"
                              "    \"\"\"The link exists but its TTL has elapsed.\"\"\"",
                              "add Expired error"))
    # Extend the shorten() signature with ttl_seconds.
    shorten = svc.segment("shorten")
    new_shorten = sub_once(r"def shorten\(self, url: str, alias: str \| None = None\) -> Link:",
                           "def shorten(self, url: str, alias: str | None = None,\n"
                           "                ttl_seconds: int | None = None) -> Link:", shorten, "signature")
    # Find where to insert TTL handling: after `now = self.clock()` or before `code = alias or ...`.
    m = re.search(r"^(?P<i>[ ]+)(now = self\.clock\(\)|code = alias or)", new_shorten, re.M)
    if not m:
        raise NeedsHuman("shorten() structure not recognised; cannot place TTL handling")
    # Indentation of that line.
    i = m["i"]
    # Whether the method already computes `now`.
    has_now = m.group(2).startswith("now")
    # Base time expression for computing expires_at.
    base = "now" if has_now else "self.clock()"
    # Validate ttl_seconds (positive int, not bool) and compute expires_at.
    prelude = (f"{i}if ttl_seconds is not None and (\n"
               f"{i}        isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int)\n"
               f"{i}        or ttl_seconds <= 0):\n"
               f"{i}    raise ValueError(\"ttl_seconds must be a positive integer\")\n"
               f"{i}expires_at = {base} + ttl_seconds if ttl_seconds is not None else None\n")
    # Insert after the `now` line, or before the `code =` line.
    at = new_shorten.index("\n", m.end()) + 1 if has_now else m.start()
    new_shorten = new_shorten[:at] + prelude + new_shorten[at:]
    # Pass expires_at to every Link(...) construction.
    new_shorten, n = re.subn(r"created_at=(now|self\.clock\(\))\)",
                             r"created_at=\1, expires_at=expires_at)", new_shorten)
    if n == 0:
        raise NeedsHuman("no Link construction found in shorten()")
    changes.append(svc.change(shorten, new_shorten, "accept ttl_seconds"))
    # resolve(): after the NotFound check, raise Expired if the TTL has elapsed.
    resolve = svc.segment("resolve")
    new_resolve = sub_once(r"(?P<i>[ ]+)raise NotFound\(code\)\n",
                           lambda m: (f"{m['i']}raise NotFound(code)\n"
                                      f"{m['i'][:-4]}if link.expires_at is not None and "
                                      f"self.clock() >= link.expires_at:\n"
                                      f"{m['i']}raise Expired(code)\n"),
                           resolve, "expiry check")
    changes.append(svc.change(resolve, new_resolve, "expired links are not resolved"))

    # --- HTTP: pass ttl_seconds through, echo expires_at, and map Expired to 410.
    http = SourceEditor(project, roles["http"], prefix)
    svc_mod = Path(roles["service"]).stem
    # Existing `from .<service> import ...` line.
    imp = re.search(rf"from \.{svc_mod} import ([^\n]+)\n", http.text)
    if not imp:
        raise NeedsHuman("http module import of service not recognised")
    # Add Expired to the imported names (sorted, de-duplicated).
    names = sorted({*[n.strip() for n in imp.group(1).split(",")], "Expired"})
    changes.append(http.change(imp.group(0), f"from .{svc_mod} import {', '.join(names)}\n",
                               "import Expired"))
    # create(): forward ttl_seconds to the service.
    create = http.segment("create")
    new_create = sub_once(r"service\.shorten\(body\.get\(\"url\", \"\"\), body\.get\(\"alias\"\)\)",
                          "service.shorten(body.get(\"url\", \"\"), body.get(\"alias\"),\n"
                          "                                   body.get(\"ttl_seconds\"))",
                          create, "pass ttl")
    # create(): include expires_at in the JSON response.
    new_create = sub_once(r"\{\"code\": link\.code, \"target_url\": link\.target_url\}",
                          "{\"code\": link.code, \"target_url\": link.target_url,\n"
                          "                                 \"expires_at\": link.expires_at}",
                          new_create, "return expires_at")
    changes.append(http.change(create, new_create, "accept and echo ttl"))
    # redirect(): add an `except Expired` branch returning 410 after the NotFound branch.
    redirect = http.segment("redirect")
    new_redirect = sub_once(r"(?P<i>[ ]+)except NotFound:\n"
                            r"(?P=i)    return \"404 Not Found\", \{\"error\": \"not found\"\}, \[\]",
                            lambda m: (m[0] + f"\n{m['i']}except Expired:\n{m['i']}    return "
                                       f"\"410 Gone\", {{\"error\": \"link expired\"}}, []"),
                            redirect, "map Expired to 410")
    changes.append(http.change(redirect, new_redirect, "410 Gone for expired links"))
    return changes


# Generated regression tests for the alias fix (written into the target repo; docstrings carry 'Covers:').
_ALIAS_TESTS = '''"""Regression tests for the alias-overwrite bug (generated)."""
import io
import json

import pytest

from links.app import make_app
from links.service import AliasTaken, LinkService
from links.store import LinkStore


@pytest.fixture
def service():
    return LinkService(LinkStore())


def _post(app, body):
    raw = json.dumps(body).encode()
    env = {"REQUEST_METHOD": "POST", "PATH_INFO": "/links", "CONTENT_LENGTH": str(len(raw)),
           "wsgi.input": io.BytesIO(raw)}
    out = {}
    payload = b"".join(app(env, lambda s, h: out.update(status=s)))
    return out["status"], json.loads(payload or b"null")


def test_duplicate_alias_is_rejected_and_original_kept(service):
    """Covers: AC-BUG-ALIAS"""
    service.shorten("https://example.com/original", alias="promo")
    with pytest.raises(AliasTaken):
        service.shorten("https://attacker.example", alias="promo")
    assert service.resolve("promo") == "https://example.com/original"


def test_duplicate_alias_returns_409_over_http(service):
    """Covers: AC-BUG-ALIAS"""
    app = make_app(service)
    assert _post(app, {"url": "https://example.com/a", "alias": "launch"})[0].startswith("201")
    status, body = _post(app, {"url": "https://example.com/b", "alias": "launch"})
    assert status.startswith("409") and body["error"] == "alias already in use"


def test_random_code_collision_is_retried(service, monkeypatch):
    """Covers: AC-BUG-ALIAS"""
    import links.service as mod
    seq = iter("aaaaaa" "aaaaaa" "bbbbbb")
    monkeypatch.setattr(mod.secrets, "choice", lambda _alphabet: next(seq))
    first = service.shorten("https://example.com/1")
    second = service.shorten("https://example.com/2")
    assert (first.code, second.code) == ("aaaaaa", "bbbbbb")
    assert service.resolve("aaaaaa") == "https://example.com/1"
'''

# Generated tests for the expiration feature, including migration of an old-schema database.
_EXPIRY_TESTS = '''"""Tests for link expiration (generated)."""
import io
import sqlite3

import pytest

from links.app import make_app
from links.service import Expired, LinkService
from links.store import LinkStore


class Clock:
    def __init__(self):
        self.t = 1_000.0

    def __call__(self):
        return self.t


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def service(clock):
    return LinkService(LinkStore(), clock=clock)


def _get(app, path):
    env = {"REQUEST_METHOD": "GET", "PATH_INFO": path, "wsgi.input": io.BytesIO(b"")}
    out = {}
    b"".join(app(env, lambda s, h: out.update(status=s, headers=dict(h))))
    return out["status"], out["headers"]


def test_link_with_ttl_expires(service, clock):
    """Covers: AC-EXP-TTL"""
    link = service.shorten("https://example.com/flash", ttl_seconds=60)
    assert link.expires_at == 1_060.0
    assert service.resolve(link.code) == "https://example.com/flash"
    clock.t = 1_060.0
    with pytest.raises(Expired):
        service.resolve(link.code)


def test_expired_link_returns_410_over_http(service, clock):
    """Covers: AC-EXP-TTL"""
    link = service.shorten("https://example.com/flash", ttl_seconds=60)
    app = make_app(service)
    assert _get(app, f"/{link.code}")[0].startswith("302")
    clock.t += 61
    assert _get(app, f"/{link.code}")[0].startswith("410")


def test_links_without_ttl_never_expire(service, clock):
    """Covers: AC-EXP-COMPAT"""
    link = service.shorten("https://example.com/forever")
    clock.t += 10 * 365 * 86_400
    assert link.expires_at is None
    assert service.resolve(link.code) == "https://example.com/forever"


@pytest.mark.parametrize("ttl", [0, -1, "60", 1.5, True])
def test_invalid_ttl_rejected(service, ttl):
    """Covers: AC-EXP-TTL"""
    with pytest.raises(ValueError):
        service.shorten("https://example.com", ttl_seconds=ttl)


def test_existing_database_is_migrated(tmp_path):
    """Covers: AC-EXP-MIGRATION"""
    path = str(tmp_path / "old.db")
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE links (code TEXT PRIMARY KEY, target_url TEXT NOT NULL, "
                 "created_at REAL NOT NULL, clicks INTEGER NOT NULL DEFAULT 0)")
    conn.execute("INSERT INTO links VALUES ('old123', 'https://example.com/old', 1.0, 7)")
    conn.commit()
    conn.close()
    store = LinkStore(path)
    link = store.get("old123")
    assert link.clicks == 7 and link.expires_at is None
    LinkStore(path)  # idempotent: second open does not fail
'''

# Recipe registry used by the domain pack (id -> Recipe).
RECIPES: dict[str, Recipe] = {
    # Bug fix: runs first (priority 10) because it touches the same files as the feature.
    "fix_alias_overwrite": Recipe(
        id="fix_alias_overwrite",
        title="Fix: custom alias silently overwrites an existing link",
        # Requirement wording that selects this recipe.
        patterns=[r"overwrit", r"alias.*(exist|taken|duplicate|already)"],
        roles=["store", "service", "http"],
        criteria=[AC("AC-BUG-ALIAS", "Creating a link with an alias that already exists fails "
                                     "(AliasTaken / HTTP 409) and the original link is unchanged; "
                                     "random-code collisions are retried instead of overwriting",
                     "regression")],
        # Edit builder and its tests.
        build=_fix_alias_overwrite,
        tests={"tests/test_alias_regression.py": _ALIAS_TESTS},
        priority=10,
        # Symbols this change touches (role:function), used for impact analysis.
        concepts=["store:save", "service:shorten", "http:create"],
    ),
    # Feature: link expiration (priority 20, after the bug fix).
    "add_expiration": Recipe(
        id="add_expiration",
        title="Feature: optional link expiration (TTL)",
        # Requirement wording that selects this recipe.
        patterns=[r"expir", r"\bttl\b"],
        roles=["model", "store", "service", "http"],
        criteria=[AC("AC-EXP-TTL", "POST /links accepts an optional positive integer ttl_seconds; "
                                   "an expired link returns 410 Gone"),
                  AC("AC-EXP-COMPAT", "Links created without ttl_seconds never expire (backward "
                                      "compatible API and behaviour)"),
                  AC("AC-EXP-MIGRATION", "Existing databases without an expires_at column are "
                                         "migrated automatically and idempotently")],
        # Edit builder and its tests.
        build=_add_expiration,
        tests={"tests/test_expiration.py": _EXPIRY_TESTS},
        priority=20,
        # Symbols this change touches (role:function), used for impact analysis.
        concepts=["store:__init__", "store:save", "store:get", "service:shorten", "service:resolve",
                  "http:create", "http:redirect"],
    ),
}
