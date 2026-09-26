"""Stubs that let this app's tests run as plain unittest, with no site.

``install_stubs()`` fills in ``frappe`` and ``jarz_pos`` **only when they are not
importable**. Inside the bench container both are real, so the same test files run
unchanged under ``bench run-tests`` — every test patches the module under test
rather than relying on the stub's behaviour, which is what makes that work.

Mirrors the pattern used across ``jarz_pos/tests`` (see
``test_purchase_warehouse_utils.py``): a fake ``frappe`` injected into
``sys.modules`` before the module under test is imported.
"""

from __future__ import annotations

import logging
import sys
import types
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from typing import Any


def install_stubs() -> None:
    _install_frappe()
    _install_jarz_pos()


# ---------------------------------------------------------------------------
# frappe
# ---------------------------------------------------------------------------

def _install_frappe() -> None:
    try:  # pragma: no cover - real frappe inside the bench container
        import frappe  # noqa: F401

        return
    except Exception:
        pass

    fake = types.ModuleType("frappe")

    class ValidationError(Exception):
        pass

    class PermissionError_(Exception):
        pass

    class DuplicateEntryError(Exception):
        pass

    class DoesNotExistError(Exception):
        pass

    def throw(message: Any, exc: type = ValidationError, **kwargs: Any):
        raise exc(str(message))

    def whitelist(*args: Any, **kwargs: Any):
        def decorator(func):
            func.whitelisted = True
            return func

        if args and callable(args[0]) and len(args) == 1 and not kwargs:
            return args[0]
        return decorator

    def _unstubbed(name: str):
        def _raise(*args: Any, **kwargs: Any):
            raise AssertionError(
                f"frappe.{name} was called for real in a unit test — patch it on the "
                "module under test instead."
            )

        return _raise

    fake._ = lambda message: message
    fake.ValidationError = ValidationError
    fake.PermissionError = PermissionError_
    fake.DuplicateEntryError = DuplicateEntryError
    fake.DoesNotExistError = DoesNotExistError
    fake.throw = throw
    fake.whitelist = whitelist
    fake.session = SimpleNamespace(user="courier@example.com")
    fake.flags = SimpleNamespace()
    fake.local = SimpleNamespace(conf={}, cache={})
    fake.log_error = lambda *a, **k: None
    fake.get_traceback = lambda *a, **k: ""
    fake.get_roles = lambda *a, **k: []
    fake.get_all = _unstubbed("get_all")
    fake.get_list = _unstubbed("get_list")
    fake.get_doc = _unstubbed("get_doc")
    fake.new_doc = _unstubbed("new_doc")
    fake.get_meta = _unstubbed("get_meta")
    fake.get_cached_value = _unstubbed("get_cached_value")
    # A real logger, silenced. The tracking/anomaly modules call `.setLevel()` on
    # whatever this returns and then log through it, so a SimpleNamespace would fail
    # on the first attribute the logging API needs. Silencing it keeps a passing suite
    # quiet without hiding the calls from a test that wants to assert on them.
    _test_logger = logging.getLogger("jarz_courier.tests")
    _test_logger.addHandler(logging.NullHandler())
    _test_logger.propagate = False
    fake.logger = lambda *a, **k: _test_logger
    # Deliberately unstubbed: every Redis interaction must be patched by the test that
    # needs it. A permissive fake cache would let a test pass while the real key shape
    # — which crosses an app boundary — was wrong.
    fake.cache = _unstubbed("cache")
    fake.db = SimpleNamespace(
        get_value=_unstubbed("db.get_value"),
        set_value=_unstubbed("db.set_value"),
        exists=_unstubbed("db.exists"),
        count=_unstubbed("db.count"),
        get_single_value=_unstubbed("db.get_single_value"),
        sql=_unstubbed("db.sql"),
        commit=lambda *a, **k: None,
    )
    fake.defaults = SimpleNamespace(get_global_default=lambda *a, **k: None)
    sys.modules["frappe"] = fake

    # frappe.utils
    utils = types.ModuleType("frappe.utils")

    def flt(value: Any, precision: int | None = None) -> float:
        try:
            result = float(value or 0)
        except (TypeError, ValueError):
            return 0.0
        return round(result, precision) if precision is not None else result

    def cint(value: Any) -> int:
        try:
            return int(float(value or 0))
        except (TypeError, ValueError):
            return 0

    def get_datetime(value: Any = None):
        if value in (None, ""):
            return datetime.now()
        if isinstance(value, datetime):
            return value
        return datetime.fromisoformat(str(value))

    def getdate(value: Any = None):
        if value in (None, ""):
            return date.today()
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        return date.fromisoformat(str(value)[:10])

    def add_to_date(value: Any = None, **kwargs: Any):
        base = get_datetime(value) if value not in (None, "") else datetime.now()
        allowed = {"days", "hours", "minutes", "seconds", "weeks"}
        delta = timedelta(**{k: v for k, v in kwargs.items() if k in allowed})
        return base + delta

    def time_diff_in_seconds(later: Any, earlier: Any) -> float:
        return (get_datetime(later) - get_datetime(earlier)).total_seconds()

    def convert_utc_to_system_timezone(utc_timestamp):
        """Identity, i.e. "the site runs in UTC".

        The real helper reads the timezone out of System Settings, which this
        harness has no database for. Identity keeps the assertion honest: a test
        can verify that a fix's `ts` and `epoch` describe the SAME instant — which
        is the invariant that matters and the one that broke — without hard-coding
        a timezone that is a per-site setting.
        """
        if getattr(utc_timestamp, "tzinfo", None) is None:
            return utc_timestamp
        return utc_timestamp.replace(tzinfo=None)

    utils.flt = flt
    utils.cint = cint
    utils.now_datetime = lambda: datetime.now()
    utils.now = lambda: datetime.now().isoformat(sep=" ")
    utils.nowdate = lambda: date.today().isoformat()
    utils.get_datetime = get_datetime
    utils.getdate = getdate
    utils.add_to_date = add_to_date
    utils.time_diff_in_seconds = time_diff_in_seconds
    utils.convert_utc_to_system_timezone = convert_utc_to_system_timezone
    sys.modules["frappe.utils"] = utils
    fake.utils = utils

    # frappe.model.document
    model = types.ModuleType("frappe.model")
    document = types.ModuleType("frappe.model.document")

    class Document:  # minimal stand-in; controllers are exercised via mocks
        def get(self, key: str, default: Any = None) -> Any:
            return getattr(self, key, default)

        def get_doc_before_save(self):
            return None

        def is_new(self) -> bool:
            return not getattr(self, "name", None)

    document.Document = Document
    model.document = document
    sys.modules["frappe.model"] = model
    sys.modules["frappe.model.document"] = document
    fake.model = model


# ---------------------------------------------------------------------------
# jarz_pos
# ---------------------------------------------------------------------------

def _install_jarz_pos() -> None:
    """Stub only ``jarz_pos.constants`` — the one thing imported at module load.

    Everything else this app uses from jarz_pos is imported lazily inside
    ``services/pos_bridge``, which the tests patch directly. That is the whole
    reason those imports are lazy: a unit test never needs jarz_pos on disk.
    """
    try:  # pragma: no cover - real jarz_pos inside the bench container
        import jarz_pos.constants  # noqa: F401

        return
    except Exception:
        pass

    pkg = types.ModuleType("jarz_pos")
    pkg.__path__ = []  # type: ignore[attr-defined]
    constants = types.ModuleType("jarz_pos.constants")

    class ROLES:
        ADMINISTRATOR = "Administrator"
        SYSTEM_MANAGER = "System Manager"
        JARZ_MANAGER = "JARZ Manager"
        JARZ_LINE_MANAGER = "jarz line manager"
        JARZ_LINE_MANAGER_ALT = "JARZ line manager"

    class WS_EVENTS:
        COURIER_STOP_ARRIVED = "jarz_pos_courier_stop_arrived"
        COURIER_STOP_DELIVERED = "jarz_pos_courier_stop_delivered"
        COURIER_STOP_FAILED = "jarz_pos_courier_stop_failed"
        COURIER_DUTY_CHANGED = "jarz_pos_courier_duty_changed"
        COURIER_DEPOSIT_DECLARED = "jarz_pos_courier_deposit_declared"
        ADDRESS_PIN_UPDATED = "jarz_pos_address_pin_updated"

    constants.ROLES = ROLES
    constants.WS_EVENTS = WS_EVENTS
    pkg.constants = constants
    sys.modules["jarz_pos"] = pkg
    sys.modules["jarz_pos.constants"] = constants


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

#: A correctly wired courier, as ``courier_onboarding`` returns one.
COURIER_IDENTITY = {
    "ok": True,
    "user": "courier@example.com",
    "party_type": "Employee",
    "party": "HR-EMP-00042",
    "display_name": "Mahmoud",
    "employee_branch": "Dokki",
    "pos_profiles": ["Dokki"],
    "branch": "Dokki",
    "problems": [],
    "message": "",
}

COURIER_ROLES = ["Jarz Courier"]
SUPERVISOR_ROLES = ["JARZ Manager"]
NO_ROLES: list[str] = []
