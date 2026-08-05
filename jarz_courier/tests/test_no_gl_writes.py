"""The machine-checkable form of the ``jarz_courier`` boundary (COURIER_CONTRACTS.md §9).

Four rules, all enforced by walking the app's own source with ``ast``. No frappe,
no site, no database — this runs anywhere and it is listed in the ``MODULES`` array
of ``.github/workflows/backend-tests.yml``, which §9 requires.

**Rule 2 — never write money.** The GL audit suite covers ``jarz_pos`` only, so any
posting logic that lands here is untested money logic *and* a second source of
truth for a courier's balance. Two checks enforce it:

* ``make_gl_entries`` and ``frappe.publish_realtime`` may not be referenced
  anywhere in the app. (The second is a §5.7 rule, not a money rule: a bare
  ``publish_realtime`` either broadcasts site-wide or addresses a room nobody
  joined. All realtime goes through ``jarz_pos.utils.realtime``.)
* The doctype names ``Journal Entry``, ``GL Entry`` and ``Courier Transaction`` may
  appear only in ``services/ledger_read.py`` — and *that* file may not contain a
  single mutating call, to any doctype at all. It is structurally read-only rather
  than read-only by review.

The allowlist exists because the courier statement genuinely has to read the
ledger; §9 forbids *inserting* a ``Courier Transaction``, not looking at one. What
the allowlist must never become is a place where writes hide, which is what the
mutating-call ban prevents.

**Rule 3 — zero Custom Fields.** jarz_pos's ``remove_colliding_custom_fields_for_fixtures``
deletes Custom Fields whose record ``name`` differs from its own fixture's, so two
apps shipping a field on the same doctype means whichever migrates second wins and
the other's field vanishes with no error. The app may not even name the doctype.

**Rule 5 — no WooCommerce import.** ``jarz_courier`` may import ``jarz_pos``; it may
never import ``jarz_woocommerce_integration``.

Plus rule 1: ``required_apps = ["jarz_pos"]``.
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path
from typing import Dict, Iterable, List, Set, Tuple

#: Root of the app package (the directory holding hooks.py).
APP_ROOT = Path(__file__).resolve().parents[1]

#: Test code is excluded: it posts nothing on a server, and this very module has
#: to contain the forbidden names in order to search for them.
_EXCLUDED_DIRS = {"tests", "__pycache__"}

#: Identifiers that must not be referenced anywhere in the app.
FORBIDDEN_IDENTIFIERS: Set[str] = {
    "make_gl_entries",
    "publish_realtime",
}

#: DocTypes whose names may appear only in the read-only allowlist below.
FORBIDDEN_DOCTYPES: Set[str] = {
    "Journal Entry",
    "GL Entry",
    "Courier Transaction",
}

#: Rule 3 — the app may not so much as name this doctype.
FORBIDDEN_ANYWHERE: Set[str] = {
    "Custom Field",
}

#: Paths (relative to APP_ROOT, posix separators) permitted to name a ledger
#: doctype. Deliberately a single file. Adding a second entry here is a design
#: decision that should be argued for in review, which is the point of making it
#: a code change rather than a convention.
READ_ONLY_LEDGER_MODULES: Set[str] = {
    "services/ledger_read.py",
}

#: Calls that write. Banned outright inside READ_ONLY_LEDGER_MODULES — for every
#: doctype, not only the ledger ones, so the file cannot mutate anything at all.
#: ``get_doc`` is included because its dict form creates a document.
MUTATING_CALLS: Set[str] = {
    "new_doc",
    "get_doc",
    "insert",
    "save",
    "submit",
    "cancel",
    "delete",
    "delete_doc",
    "set_value",
    "set_values",
    "db_set",
    "sql",
    "multisql",
    "commit",
    "rename_doc",
    "bulk_insert",
    "bulk_update",
    "truncate",
}

#: A path bug that finds no files would make every check below pass vacuously.
MIN_EXPECTED_FILES = 15


# ---------------------------------------------------------------------------
# Source walking
# ---------------------------------------------------------------------------

def _app_python_files() -> List[Path]:
    files: List[Path] = []
    for path in sorted(APP_ROOT.rglob("*.py")):
        rel_parts = path.relative_to(APP_ROOT).parts
        if any(part in _EXCLUDED_DIRS for part in rel_parts):
            continue
        files.append(path)
    return files


def _rel(path: Path) -> str:
    return path.relative_to(APP_ROOT).as_posix()


def _parse(path: Path) -> ast.AST:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _docstring_constant_ids(tree: ast.AST) -> Set[int]:
    """Ids of every bare string expression — docstrings and prose blocks.

    Excluded from the literal scan so this app can *document* the rules it obeys.
    A docstring is not a reference; ``"never inserts a Courier Transaction"`` is a
    promise, not a call.
    """
    ids: Set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            if isinstance(node.value.value, str):
                ids.add(id(node.value))
    return ids


def _string_constants(tree: ast.AST) -> Iterable[Tuple[str, int]]:
    """Every non-docstring string literal, with its line number."""
    skip = _docstring_constant_ids(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) in skip:
                continue
            yield node.value, getattr(node, "lineno", 0)


def _called_names(tree: ast.AST) -> Iterable[Tuple[str, int]]:
    """The attribute/name being *called* at each call site."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute):
            yield func.attr, getattr(node, "lineno", 0)
        elif isinstance(func, ast.Name):
            yield func.id, getattr(node, "lineno", 0)


def _referenced_identifiers(tree: ast.AST) -> Iterable[Tuple[str, int]]:
    """Every bare name and attribute access, called or not."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            yield node.attr, getattr(node, "lineno", 0)
        elif isinstance(node, ast.Name):
            yield node.id, getattr(node, "lineno", 0)


def _imported_roots(tree: ast.AST) -> Iterable[Tuple[str, int]]:
    """Top-level package of every import in the file."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name.split(".")[0], getattr(node, "lineno", 0)
        elif isinstance(node, ast.ImportFrom):
            if node.module and not node.level:
                yield node.module.split(".")[0], getattr(node, "lineno", 0)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestAppSourceIsDiscoverable(unittest.TestCase):
    def test_app_root_is_the_package(self) -> None:
        self.assertTrue(
            (APP_ROOT / "hooks.py").is_file(),
            f"APP_ROOT {APP_ROOT} does not look like the jarz_courier package",
        )

    def test_enough_files_are_scanned(self) -> None:
        files = _app_python_files()
        self.assertGreaterEqual(
            len(files),
            MIN_EXPECTED_FILES,
            "Only "
            f"{len(files)} python files were scanned — the walk is broken and every "
            "boundary check below would pass vacuously.",
        )


class TestNoGlWrites(unittest.TestCase):
    """Rule 2 — never write GL, never create a Journal Entry, never insert a ledger row."""

    def test_no_forbidden_identifiers(self) -> None:
        violations: List[str] = []
        for path in _app_python_files():
            tree = _parse(path)
            for name, lineno in _referenced_identifiers(tree):
                if name in FORBIDDEN_IDENTIFIERS:
                    violations.append(f"{_rel(path)}:{lineno} references {name}")
            for value, lineno in _string_constants(tree):
                for forbidden in FORBIDDEN_IDENTIFIERS:
                    if forbidden in value:
                        violations.append(f"{_rel(path)}:{lineno} string names {forbidden}")

        self.assertEqual(
            [],
            violations,
            "jarz_courier must never post GL entries and must never call "
            "publish_realtime directly (route realtime through "
            "jarz_pos.utils.realtime). Violations:\n  " + "\n  ".join(violations),
        )

    def test_ledger_doctypes_only_named_in_the_allowlist(self) -> None:
        violations: List[str] = []
        for path in _app_python_files():
            rel = _rel(path)
            if rel in READ_ONLY_LEDGER_MODULES:
                continue
            tree = _parse(path)
            for value, lineno in _string_constants(tree):
                for doctype in FORBIDDEN_DOCTYPES:
                    if doctype in value:
                        violations.append(f"{rel}:{lineno} names {doctype!r}")

        self.assertEqual(
            [],
            violations,
            "Only "
            + ", ".join(sorted(READ_ONLY_LEDGER_MODULES))
            + " may name a ledger doctype, and only to read it. Every money write "
            "goes through a jarz_pos service. Violations:\n  " + "\n  ".join(violations),
        )

    def test_allowlisted_modules_contain_no_mutating_call(self) -> None:
        violations: List[str] = []
        for rel in sorted(READ_ONLY_LEDGER_MODULES):
            path = APP_ROOT / rel
            tree = _parse(path)
            for name, lineno in _called_names(tree):
                if name in MUTATING_CALLS:
                    violations.append(f"{rel}:{lineno} calls {name}()")

        self.assertEqual(
            [],
            violations,
            "A module allowed to read the ledger must be structurally incapable of "
            "writing anything. Move the write into services/deposits.py (which owns "
            "only jarz_courier doctypes) or into a jarz_pos service. Violations:\n  "
            + "\n  ".join(violations),
        )

    def test_allowlisted_modules_exist(self) -> None:
        """A renamed allowlist entry must fail loudly, not silently widen the ban."""
        for rel in sorted(READ_ONLY_LEDGER_MODULES):
            self.assertTrue(
                (APP_ROOT / rel).is_file(),
                f"Allowlisted module {rel} does not exist. Update "
                "READ_ONLY_LEDGER_MODULES together with the rename.",
            )


class TestNoSharedCustomFields(unittest.TestCase):
    """Rule 3 — declare zero Custom Fields on any doctype jarz_pos touches."""

    def test_custom_field_is_never_named(self) -> None:
        violations: List[str] = []
        for path in _app_python_files():
            tree = _parse(path)
            for value, lineno in _string_constants(tree):
                for forbidden in FORBIDDEN_ANYWHERE:
                    if forbidden in value:
                        violations.append(f"{_rel(path)}:{lineno} names {forbidden!r}")

        self.assertEqual(
            [],
            violations,
            "jarz_courier declares zero Custom Fields. jarz_pos's collision cleanup "
            "deletes fields whose record name differs from its own fixture's, so "
            "whichever app migrates second wins and the other's field disappears "
            "silently. Shared schema is owned by jarz_pos. Violations:\n  "
            + "\n  ".join(violations),
        )

    def test_hooks_declares_no_fixtures(self) -> None:
        tree = _parse(APP_ROOT / "hooks.py")
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            targets = {t.id for t in node.targets if isinstance(t, ast.Name)}
            if "fixtures" not in targets:
                continue
            self.assertIsInstance(
                node.value,
                ast.List,
                "hooks.fixtures must be a literal list",
            )
            self.assertEqual(
                [],
                node.value.elts,
                "hooks.fixtures must stay empty — a Custom Field fixture here "
                "collides with jarz_pos's on the next migrate.",
            )


class TestDependencyDirection(unittest.TestCase):
    """Rules 1 and 5 — depend on jarz_pos, never on jarz_woocommerce_integration."""

    def test_never_imports_woocommerce(self) -> None:
        violations: List[str] = []
        for path in _app_python_files():
            tree = _parse(path)
            for root, lineno in _imported_roots(tree):
                if root == "jarz_woocommerce_integration":
                    violations.append(f"{_rel(path)}:{lineno}")

        self.assertEqual(
            [],
            violations,
            "jarz_courier must never import jarz_woocommerce_integration. The two "
            "apps are independent and contract only on standard Sales Invoice "
            "fields. Violations:\n  " + "\n  ".join(violations),
        )

    def test_required_apps_declares_jarz_pos(self) -> None:
        tree = _parse(APP_ROOT / "hooks.py")
        found: Dict[str, List[str]] = {}
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            targets = {t.id for t in node.targets if isinstance(t, ast.Name)}
            if "required_apps" not in targets:
                continue
            self.assertIsInstance(node.value, ast.List)
            found["required_apps"] = [
                elt.value for elt in node.value.elts if isinstance(elt, ast.Constant)
            ]

        self.assertIn(
            "required_apps",
            found,
            "hooks.py must declare required_apps so bench refuses to install this "
            "app onto a site without jarz_pos.",
        )
        self.assertEqual(["jarz_pos"], found["required_apps"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
