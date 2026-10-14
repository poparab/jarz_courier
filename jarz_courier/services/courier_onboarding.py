"""Courier account wiring — and a readable error when it is wrong.

A courier account only works when **two independent things** are true:

1. There is a ``POS Profile User`` row linking the login to a POS Profile.
   ``jarz_pos.utils.access_control.get_user_pos_profiles`` reads exactly this, and
   returns ``[]`` when it is missing. Every branch-scoped query then filters on an
   empty list, so the run sheet renders as "no stops" — indistinguishable from a
   genuinely empty day.
2. The courier's party record carries ``branch = <that same POS Profile name>``.
   ``jarz_pos.utils.courier_visibility.assert_courier_matches_pos_profile`` compares
   the Employee's ``branch`` string against the POS Profile name and throws
   ``"Courier X belongs to POS Profile A, not B"`` — or, when ``branch`` is blank,
   ``"Courier X has no branch and cannot be assigned"``. Neither message tells the
   person reading it *where* to fix it.

The two are set in different places by different people (a POS Profile's user
table vs. the HR Employee form), so they drift constantly, and the two failure
modes look nothing alike: (1) is silent, (2) is a cryptic 417. This module
collapses both into one diagnosis with the fix in it.

The diagnosis half (``diagnose_courier_setup`` and friends) is read-only: it inspects
configuration and reports. It never repairs anything — adding a user to a branch is a
permission decision, not an error-recovery step. The second half of the module
(``setup_courier`` and friends, contract F2) is that permission decision made on
purpose, by a manager, from the POS app's Users screen.
"""

from __future__ import annotations

import re
import secrets
from typing import Any, Dict, List, Optional

import frappe
from frappe import _
from frappe.utils import cint

from jarz_courier.constants import ROLES
from jarz_courier.services import pos_bridge


class CourierSetupError(frappe.ValidationError):
    """Raised when a courier login is not wired up to a branch correctly.

    Its own class so the client can show the setup instructions rather than a
    generic failure, and so a support engineer greping logs can count them.
    """


def diagnose_courier_setup(user: Optional[str] = None) -> Dict[str, Any]:
    """Describe how *user* is (or is not) wired up as a courier. Never throws.

    Returns::

        {
            "ok": bool,
            "user": str,
            "party_type": str, "party": str, "display_name": str,
            "employee_branch": str,        # Employee.branch
            "pos_profiles": [str],         # POS Profile User rows
            "branch": str,                 # the resolved, usable branch (or "")
            "problems": [str],             # machine codes, see below
            "message": str,                # one human sentence, already translated
        }

    Problem codes: ``no_identity``, ``no_pos_profile``, ``no_employee_branch``,
    ``branch_not_assigned``.
    """
    resolved_user = str(user or frappe.session.user or "").strip()

    result: Dict[str, Any] = {
        "ok": False,
        "user": resolved_user,
        "party_type": "",
        "party": "",
        "display_name": "",
        "employee_branch": "",
        "pos_profiles": [],
        "branch": "",
        "problems": [],
        "message": "",
    }

    identity: Dict[str, Any] = {}
    try:
        identity = pos_bridge.resolve_courier_identity(resolved_user) or {}
    except Exception:
        result["problems"].append("no_identity")

    result["party_type"] = str(identity.get("party_type") or "")
    result["party"] = str(identity.get("party") or "")
    result["display_name"] = str(identity.get("display_name") or result["party"])
    result["employee_branch"] = str(identity.get("branch") or "").strip()

    profiles: List[str] = []
    try:
        profiles = pos_bridge.get_user_pos_profiles(resolved_user)
    except Exception:
        profiles = []
    result["pos_profiles"] = profiles

    if "no_identity" not in result["problems"]:
        if not profiles:
            result["problems"].append("no_pos_profile")
        if not result["employee_branch"]:
            result["problems"].append("no_employee_branch")
        elif profiles and result["employee_branch"] not in profiles:
            # Only meaningful when the courier IS assigned somewhere. With no
            # assignments at all, "your branch is not in your (empty) list" is the
            # same fact as `no_pos_profile`, and reporting both makes the message
            # picker choose the vaguer of two true statements.
            result["problems"].append("branch_not_assigned")

    if not result["problems"]:
        result["ok"] = True
        result["branch"] = result["employee_branch"]

    result["message"] = _setup_message(result)
    return result


def _setup_message(diagnosis: Dict[str, Any]) -> str:
    """One sentence naming the record to open and the value to put in it."""
    problems = set(diagnosis.get("problems") or [])
    party = diagnosis.get("party") or diagnosis.get("user") or ""
    employee_branch = diagnosis.get("employee_branch") or ""
    profiles = diagnosis.get("pos_profiles") or []

    if not problems:
        return _("Courier account is set up correctly for branch {0}.").format(
            diagnosis.get("branch") or ""
        )

    if "no_identity" in problems:
        return _(
            "{0} is not linked to an Employee record. Open the courier's Employee, "
            "set User ID to this login and Status to Active."
        ).format(diagnosis.get("user") or "")

    if "no_pos_profile" in problems and "no_employee_branch" in problems:
        return _(
            "Courier {0} is not connected to a branch at all. Two records must be "
            "set, both to the same POS Profile name: add the login to the Users "
            "table of the POS Profile, and set Branch on the Employee."
        ).format(party)

    if "no_pos_profile" in problems:
        return _(
            "Courier {0} has Branch {1} on their Employee record but is not in that "
            "POS Profile's Users table. Open POS Profile {1} and add this login to "
            "Applicable for Users."
        ).format(party, employee_branch)

    if "no_employee_branch" in problems:
        return _(
            "Courier {0} is assigned to POS Profile {1} but their Employee record "
            "has no Branch. Open the Employee and set Branch to {1} — the two must "
            "match exactly."
        ).format(party, ", ".join(profiles) or _("a branch"))

    if "branch_not_assigned" in problems:
        return _(
            "Courier {0} has Branch {1} on their Employee record, but is only "
            "assigned to POS Profile(s) {2}. The Employee Branch and the POS "
            "Profile must be the same name."
        ).format(party, employee_branch, ", ".join(profiles) or _("none"))

    return _("Courier {0} is not set up correctly.").format(party)


def ensure_courier_setup(user: Optional[str] = None, *, action_label: str = "this action") -> Dict[str, Any]:
    """:func:`diagnose_courier_setup`, but throw ``CourierSetupError`` when broken.

    Returns the diagnosis (which carries ``party_type``/``party``/``branch``) so
    callers get identity resolution and validation in one round trip instead of
    asking the same questions twice.
    """
    diagnosis = diagnose_courier_setup(user)
    if diagnosis.get("ok"):
        return diagnosis

    frappe.throw(
        _("{0} is not available: {1}").format(action_label, diagnosis.get("message") or ""),
        CourierSetupError,
        title=_("Courier Account Not Set Up"),
    )
    return diagnosis  # unreachable


def resolve_active_branch(
    requested_branch: Optional[str] = None,
    *,
    user: Optional[str] = None,
    action_label: str = "this action",
) -> Dict[str, Any]:
    """Validate setup and pick the branch to operate on.

    A courier normally has exactly one branch. ``requested_branch`` exists for the
    rare multi-branch courier and is validated against the diagnosis rather than
    trusted — a client-supplied branch that the courier's Employee record does not
    carry is a scoping bypass, not a preference.
    """
    diagnosis = ensure_courier_setup(user, action_label=action_label)
    branch = str(requested_branch or "").strip() or str(diagnosis.get("branch") or "")

    allowed = set(diagnosis.get("pos_profiles") or [])
    if branch not in allowed or branch != diagnosis.get("employee_branch"):
        frappe.throw(
            _(
                "Branch {0} is not this courier's branch. {1} is limited to {2}."
            ).format(branch or _("(none)"), action_label, diagnosis.get("employee_branch") or _("none")),
            frappe.PermissionError,
            title=_("Wrong Branch"),
        )

    diagnosis["branch"] = branch
    return diagnosis


# ---------------------------------------------------------------------------
# F2 - courier setup from the POS app
#
# One call turns "this person is a courier" into a working login: a User holding
# only the Jarz Courier role, linked to an Employee in the Delivery group, with that
# Employee's branch and a POS Profile User row naming the same branch, and a fresh
# PIN. Everything that can be refused is refused BEFORE the first write, so a manager
# fixing a typo never leaves a half-built account behind (the request also rolls back
# as a whole on any throw).
#
# The door is the Users screen's: JARZ Manager, System Manager or Administrator, and
# the same protection rules (nobody can reset a manager's password through here).
# Nothing in this section touches money.
# ---------------------------------------------------------------------------

#: The Role Profile that carries the courier role. Roles are only ever granted through
#: a profile: Frappe v16 rebuilds ``User.roles`` from the profiles on every save and
#: drops any role granted directly.
ROLE_PROFILE = "Jarz Courier"

#: A courier's login on a site that has not turned on login by mobile number. The
#: mobile app always sends the normalized mobile as the username, so the User's email
#: only matters to Frappe, never to the courier.
GENERATED_EMAIL_DOMAIN = "orderjarz.com"

#: Upper bound on the Employee picker. The delivery team is dozens, not thousands.
MAX_CANDIDATES = 500

PIN_LENGTH = 6

_EGYPT_MOBILE = re.compile(r"^01\d{9}$")


def normalize_phone(value: Any) -> str:
    """The courier app's own phone normalization, so a stored number is the one typed.

    The app logs in with ``usr = normalizePhone(phone)``: spaces, dashes and brackets
    stripped, then ``+20xxxxxxxxxx`` / ``0020xxxxxxxxxx`` / ``20xxxxxxxxxx`` turned
    into ``0xxxxxxxxxx``. ``User.mobile_no`` must hold exactly that string for the
    login to find the account.
    """
    text = re.sub(r"[\s\-()]", "", str(value or ""))
    if text.startswith("+20"):
        text = "0" + text[3:]
    elif text.startswith("0020"):
        text = "0" + text[4:]
    elif len(text) == 12 and text.startswith("20"):
        text = "0" + text[2:]
    return text


def generate_pin() -> str:
    """A random six-digit PIN that is not an obvious one (111111, 123456, 654321).

    From ``secrets``; never logged, never stored anywhere but as the password hash.
    """
    while True:
        pin = f"{secrets.randbelow(10 ** PIN_LENGTH):0{PIN_LENGTH}d}"
        digits = [int(c) for c in pin]
        steps = {b - a for a, b in zip(digits, digits[1:])}
        if len(set(pin)) == 1 or steps == {1} or steps == {-1}:
            continue
        return pin


def _set_pin(user: str, pin: str) -> None:
    """Store *pin* as the User's password.

    ``update_password`` rather than ``User.new_password``: the latter runs the site's
    password-strength policy, which a six-digit PIN fails by design. Imported here so
    this module loads without a full Frappe.
    """
    from frappe.utils.password import update_password

    update_password(user, pin)


def courier_links() -> Dict[str, Optional[str]]:
    """Where a courier goes to use the app.

    ``apk_url`` is ``None``: there is no courier APK setting. (jarz_pos's
    ``mobile_apk_download_url`` points at the POS app, which a courier must not be
    given.) The client treats a missing link as "not published yet".
    """
    try:
        web_url: Optional[str] = frappe.utils.get_url("/courier/")
    except Exception:
        web_url = None
    return {"web_url": web_url, "apk_url": None}


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _mobile_login_enabled() -> bool:
    """True when System Settings lets a User log in by mobile number.

    This code never switches that on: it is a site-wide authentication setting, not
    something a courier-setup call may change.
    """
    try:
        return bool(cint(frappe.db.get_single_value("System Settings", "allow_login_using_mobile_number")))
    except Exception:
        return False


def _login_id(user: str, mobile: str, mobile_login: bool) -> str:
    return mobile if (mobile_login and mobile) else user


def _generated_email(mobile: str) -> str:
    return f"courier.{mobile}@{GENERATED_EMAIL_DOMAIN}"


def ensure_courier_role_profile() -> str:
    """The "Jarz Courier" Role Profile, created on first use.

    Throws when the Role itself is missing (a migrate has not run) or when an existing
    profile of that name does not grant it - a profile like that would create logins
    that can do nothing.
    """
    if not frappe.db.exists("Role", ROLES.COURIER):
        frappe.throw(
            _("The {0} role does not exist on this site. Run a migrate, then try again.").format(ROLES.COURIER)
        )

    if frappe.db.exists("Role Profile", ROLE_PROFILE):
        granted = frappe.get_all(
            "Has Role",
            filters={"parenttype": "Role Profile", "parent": ROLE_PROFILE, "role": ROLES.COURIER},
            pluck="role",
        )
        if not granted:
            frappe.throw(
                _("Role Profile {0} exists but does not include the {1} role. Add the role to it first.").format(
                    ROLE_PROFILE, ROLES.COURIER
                )
            )
        return ROLE_PROFILE

    profile = frappe.get_doc(
        {
            "doctype": "Role Profile",
            "role_profile": ROLE_PROFILE,
            "roles": [{"role": ROLES.COURIER}],
        }
    )
    profile.insert(ignore_permissions=True)
    return ROLE_PROFILE


def _roles_of(user: str) -> set:
    rows = frappe.get_all("Has Role", filters={"parenttype": "User", "parent": user}, pluck="role") or []
    return {str(r) for r in rows}


def _ensure_courier_only_account(user: str, *, require_role: bool = False) -> None:
    """Refuse to touch an account that is anything other than a courier login.

    Setting a six-digit PIN, or replacing the role profiles, on a cashier's, line
    manager's or manager's account would weaken or break it. Courier setup is for
    accounts that exist only to deliver.
    """
    roles = _roles_of(user)
    extra = sorted(roles - {ROLES.COURIER})
    if extra:
        frappe.throw(
            _("{0} also has the role(s) {1}, so it is not a courier-only login and cannot be set up here.").format(
                user, ", ".join(extra)
            )
        )
    if require_role and ROLES.COURIER not in roles:
        frappe.throw(_("{0} is not a courier login.").format(user))


def _resolve_profile(branch: Any, pos_profile: Any, employee_branch: str) -> str:
    """The POS Profile this courier works from; the Branch of the Employee has the same name."""
    chosen_branch = _clean(branch)
    chosen_profile = _clean(pos_profile)
    if chosen_branch and chosen_profile and chosen_branch != chosen_profile:
        frappe.throw(
            _("Branch {0} and POS Profile {1} must be the same name.").format(chosen_branch, chosen_profile)
        )
    chosen = chosen_profile or chosen_branch or _clean(employee_branch)
    if not chosen:
        frappe.throw(_("Choose the branch this courier works from."))

    row = frappe.db.get_value("POS Profile", chosen, ["name", "disabled"], as_dict=True)
    if not row:
        frappe.throw(_("POS Profile {0} does not exist.").format(chosen))
    if cint(row.get("disabled")):
        frappe.throw(_("POS Profile {0} is disabled.").format(chosen))
    return chosen


def _mobile_owner(mobile: str, exclude_user: str) -> Optional[str]:
    return frappe.db.get_value("User", {"mobile_no": mobile, "name": ["!=", exclude_user]}, "name")


def list_courier_candidates() -> Dict[str, Any]:
    """Active Employees a courier login could be made for, with how far each one is.

    ``setup_ok`` is True only when the Employee already has a working courier account;
    ``problems`` lists what is missing, using the same codes as
    :func:`diagnose_courier_setup` plus ``user_disabled`` and ``no_courier_role``.
    One query per table, not one per Employee.
    """
    pos_bridge.ensure_user_admin_access()

    employees = (
        frappe.get_all(
            "Employee",
            filters={"status": "Active"},
            fields=["name", "employee_name", "branch", "user_id"],
            order_by="employee_name asc",
            limit=MAX_CANDIDATES,
        )
        or []
    )
    branches = frappe.get_all("POS Profile", filters={"disabled": 0}, pluck="name", order_by="name asc") or []

    in_group = pos_bridge.delivery_group_employees()
    users = sorted({_clean(e.get("user_id")) for e in employees if _clean(e.get("user_id"))})

    enabled: Dict[str, bool] = {}
    courier_users: set = set()
    profiles_by_user: Dict[str, List[str]] = {}
    if users:
        for row in frappe.get_all("User", filters={"name": ["in", users]}, fields=["name", "enabled"]) or []:
            enabled[str(row.get("name"))] = bool(cint(row.get("enabled")))
        courier_users = {
            str(r)
            for r in frappe.get_all(
                "Has Role",
                filters={"parenttype": "User", "parent": ["in", users], "role": ROLES.COURIER},
                pluck="parent",
            )
            or []
        }
        live = set(branches)
        for row in (
            frappe.get_all(
                "POS Profile User",
                filters={"parenttype": "POS Profile", "user": ["in", users]},
                fields=["user", "parent"],
                limit_page_length=0,
            )
            or []
        ):
            if str(row.get("parent")) in live:
                profiles_by_user.setdefault(str(row.get("user")), []).append(str(row.get("parent")))

    out: List[Dict[str, Any]] = []
    for emp in employees:
        user = _clean(emp.get("user_id"))
        branch = _clean(emp.get("branch"))
        problems: List[str] = []
        if not user:
            problems.append("no_identity")
        else:
            if not enabled.get(user, False):
                problems.append("user_disabled")
            if user not in courier_users:
                problems.append("no_courier_role")
            profiles = profiles_by_user.get(user, [])
            if not profiles:
                problems.append("no_pos_profile")
            if not branch:
                problems.append("no_employee_branch")
            elif profiles and branch not in profiles:
                problems.append("branch_not_assigned")
        out.append(
            {
                "employee": emp.get("name"),
                "employee_name": emp.get("employee_name") or emp.get("name"),
                "branch": branch,
                "user": user or None,
                "is_courier": emp.get("name") in in_group,
                "setup_ok": not problems,
                "problems": problems,
            }
        )
    return {"employees": out, "branches": [str(b) for b in branches]}


def get_courier_setup(user: str) -> Dict[str, Any]:
    """How *user* is wired up as a courier, for the Users screen. Same door as setup."""
    pos_bridge.ensure_user_admin_access()
    return diagnose_courier_setup(_clean(user))


def reset_courier_pin(user: str) -> Dict[str, Any]:
    """A new PIN for an existing courier login. Returned once; not recoverable later."""
    pos_bridge.ensure_user_admin_access()
    user = _clean(user)
    if not user:
        frappe.throw(_("User is required."))

    row = pos_bridge.ensure_can_modify_user(user)
    if not cint(row.get("enabled")):
        frappe.throw(_("{0} is disabled. Enable the account first.").format(user))
    _ensure_courier_only_account(user, require_role=True)

    pin = generate_pin()
    _set_pin(user, pin)
    _sign_out_quietly(user)

    mobile = _clean(frappe.db.get_value("User", user, "mobile_no"))
    return {"user": user, "login_id": _login_id(user, mobile, _mobile_login_enabled()), "pin": pin}


def _sign_out_quietly(user: str) -> None:
    """End the courier's old sessions so a changed PIN takes effect now; never fails the call."""
    try:
        pos_bridge.sign_out_everywhere(user)
    except Exception:
        frappe.log_error(frappe.get_traceback(), "jarz_courier: courier sign-out after PIN change failed")


def setup_courier(
    employee: Optional[str] = None,
    full_name: Optional[str] = None,
    mobile_no: Optional[str] = None,
    branch: Optional[str] = None,
    pos_profile: Optional[str] = None,
) -> Dict[str, Any]:
    """Make a courier login work end to end, and hand back its login id and PIN.

    Give ``employee`` to use an existing Employee, or ``full_name`` to create one in
    the Delivery group. Safe to run again for the same courier: it re-uses the login,
    re-asserts the branch wiring and issues a new PIN.
    """
    pos_bridge.ensure_user_admin_access()

    employee_id = _clean(employee)
    name = _clean(full_name)
    if not employee_id and not name:
        frappe.throw(_("Choose an Employee or enter the courier's full name."))

    # ---- Read and validate everything first -------------------------------------
    emp: Dict[str, Any] = {}
    if employee_id:
        emp = (
            frappe.db.get_value(
                "Employee",
                employee_id,
                ["name", "employee_name", "status", "branch", "user_id", "cell_number"],
                as_dict=True,
            )
            or {}
        )
        if not emp:
            frappe.throw(_("Employee {0} does not exist.").format(employee_id))
        if _clean(emp.get("status")) != "Active":
            frappe.throw(_("Employee {0} is not Active.").format(employee_id))
        name = _clean(emp.get("employee_name")) or employee_id
    elif frappe.db.exists("Employee", {"employee_name": name}):
        # The POS courier picker would silently adopt a same-name Employee, which here
        # would put a login on somebody else's record.
        frappe.throw(
            _("An Employee named {0} already exists. Choose it from the list instead.").format(name)
        )

    existing_user = _clean(emp.get("user_id")) if emp else ""
    mobile = normalize_phone(
        mobile_no
        or (emp.get("cell_number") if emp else None)
        or (frappe.db.get_value("User", existing_user, "mobile_no") if existing_user else None)
    )
    if not _EGYPT_MOBILE.match(mobile):
        frappe.throw(_("Enter the courier's mobile number as 01XXXXXXXXX."))

    user = existing_user or _generated_email(mobile)
    user_exists = bool(frappe.db.exists("User", user))
    if user_exists:
        row = pos_bridge.ensure_can_modify_user(user)
        if not cint(row.get("enabled")):
            frappe.throw(_("{0} is disabled. Enable the account first.").format(user))
        _ensure_courier_only_account(user)
        owner = frappe.db.get_value("Employee", {"user_id": user}, "name")
        if owner and owner != employee_id:
            frappe.throw(_("{0} is already linked to Employee {1}.").format(user, owner))

    holder = _mobile_owner(mobile, user)
    if holder:
        frappe.throw(_("The mobile number {0} is already used by {1}.").format(mobile, holder))

    profile = _resolve_profile(branch, pos_profile, _clean(emp.get("branch")) if emp else "")
    role_profile = ensure_courier_role_profile()
    mobile_login = _mobile_login_enabled()

    # ---- Writes ------------------------------------------------------------------
    if not emp:
        created = pos_bridge.create_delivery_employee(name=name, phone=mobile, branch=profile)
        employee_id = _clean(created.get("party"))
        if not employee_id:
            frappe.throw(_("The Employee could not be created."))

    pos_bridge.ensure_branch(profile)
    if _clean(frappe.db.get_value("Employee", employee_id, "branch")) != profile:
        frappe.db.set_value("Employee", employee_id, "branch", profile)
    pos_bridge.ensure_employee_in_delivery_group(employee_id)

    if user_exists:
        doc = frappe.get_doc("User", user)
    else:
        doc = frappe.new_doc("User")
        doc.email = user
        doc.first_name = name
        doc.enabled = 1
        doc.send_welcome_email = 0
        if doc.meta.has_field("custom_require_pos_shift"):
            doc.custom_require_pos_shift = 0
    doc.mobile_no = mobile
    pos_bridge.apply_role_profiles(doc, [role_profile])
    doc.flags.ignore_permissions = True
    if user_exists:
        doc.save(ignore_permissions=True)
    else:
        doc.insert(ignore_permissions=True)
        user = doc.name

    if _clean(frappe.db.get_value("Employee", employee_id, "user_id")) != user:
        pos_bridge.link_employee_to_user(user, employee_id)

    if not pos_bridge.is_branch_member(user, profile):
        pos_bridge.grant_branch_access(user, profile, _("Courier setup"))

    pin = generate_pin()
    _set_pin(user, pin)
    _sign_out_quietly(user)

    warnings: List[str] = []
    if not mobile_login:
        # The courier app logs in with the mobile number. Without this System Settings
        # switch the site only knows the generated email, so the login id shown is the
        # email and the app's phone field will not find the account.
        warnings.append("mobile_login_disabled")

    return {
        "user": user,
        "employee": employee_id,
        "login_id": _login_id(user, mobile, mobile_login),
        "pin": pin,
        "diagnosis": diagnose_courier_setup(user),
        "links": courier_links(),
        "created_user": not user_exists,
        "warnings": warnings,
    }
