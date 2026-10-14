"""Contract F2 - courier setup from the POS app (``services/courier_onboarding`` + ``api/onboarding``).

The point of the feature is that one manager action leaves a courier who can log in,
sees their own branch and nobody else's, and whose account cannot be used to do
anything else. So the tests are about what must NOT happen as much as what must:

* nobody without the Users-screen door gets anywhere, and nothing is written for them;
* every refusal happens before the first write (no half-built accounts);
* only a courier-only account is touched - a cashier's or manager's PIN and roles are
  not ours to change;
* the login id is the string the courier app actually sends;
* the PIN is six digits, goes through ``update_password`` and appears in no log.

A small in-memory ``Site`` stands in for the database; the real jarz_pos calls are
patched at ``pos_bridge``, which is the seam this app has to jarz_pos.
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from jarz_courier.tests import _support

_support.install_stubs()

import frappe  # noqa: E402

from jarz_courier.api import onboarding as api  # noqa: E402
from jarz_courier.services import courier_onboarding as svc  # noqa: E402

ROLE = "Jarz Courier"


class Site:
    """Just enough database for the calls ``courier_onboarding`` makes."""

    def __init__(self) -> None:
        self.employees = {
            "HR-EMP-1": {
                "name": "HR-EMP-1",
                "employee_name": "Mahmoud Ali",
                "status": "Active",
                "branch": None,
                "user_id": None,
                "cell_number": "0101 234 5678",
            }
        }
        self.users = {}  # name -> {"mobile_no", "enabled"}
        self.profiles = {"Dokki": {"name": "Dokki", "disabled": 0}, "Maadi": {"name": "Maadi", "disabled": 0}}
        self.roles = {}  # user -> set(role)
        self.member_of = set()  # (user, profile)
        self.set_values = []

    # -- frappe.db ------------------------------------------------------------
    def get_value(self, doctype, filters, fieldname=None, as_dict=False, **kwargs):
        record = None
        if doctype == "Employee":
            if isinstance(filters, dict):
                for emp in self.employees.values():
                    if all(emp.get(k) == v for k, v in filters.items()):
                        record = emp
            else:
                record = self.employees.get(filters)
        elif doctype == "User":
            if isinstance(filters, dict):
                for name, user in self.users.items():
                    if user.get("mobile_no") == filters["mobile_no"] and name != filters["name"][1]:
                        record = {"name": name}
            else:
                record = self.users.get(filters)
                if record is not None:
                    record = {"name": filters, **record}
        elif doctype == "POS Profile":
            record = self.profiles.get(filters)
        if record is None:
            return None
        if as_dict:
            return dict(record)
        return record.get(fieldname)

    def exists(self, doctype, filters=None):
        if doctype == "Role":
            return filters == ROLE
        if doctype == "Role Profile":
            return None
        if doctype == "User":
            return filters if filters in self.users else None
        if doctype == "Employee" and isinstance(filters, dict):
            return any(e["employee_name"] == filters["employee_name"] for e in self.employees.values()) or None
        return None

    def set_value(self, doctype, name, field, value):
        self.set_values.append((doctype, name, field, value))
        if doctype == "Employee":
            self.employees[name][field] = value

    def get_all(self, doctype, filters=None, **kwargs):
        if doctype == "Has Role" and filters.get("parenttype") == "User":
            return sorted(self.roles.get(filters["parent"], set()))
        return []


class OnboardingTestCase(unittest.TestCase):
    mobile_login = False

    def setUp(self) -> None:
        self.site = Site()
        self.new_user = MagicMock()
        self.new_user.name = "courier.01012345678@orderjarz.com"
        self.new_user.meta.has_field.return_value = True
        self.existing_user = MagicMock()

        self.bridge = MagicMock()
        self.bridge.ensure_can_modify_user.side_effect = lambda user: {
            "name": user,
            "enabled": self.site.users.get(user, {}).get("enabled", 1),
        }
        self.bridge.is_branch_member.side_effect = lambda user, profile: (user, profile) in self.site.member_of
        self.bridge.create_delivery_employee.side_effect = self._create_employee
        self.set_pin = MagicMock()
        self.log_error = MagicMock()

        patches = [
            patch.object(svc, "pos_bridge", self.bridge),
            patch.object(svc.frappe.db, "get_value", self.site.get_value),
            patch.object(svc.frappe.db, "exists", self.site.exists),
            patch.object(svc.frappe.db, "set_value", self.site.set_value),
            patch.object(svc.frappe, "get_all", self.site.get_all),
            patch.object(svc.frappe, "new_doc", return_value=self.new_user),
            patch.object(svc.frappe, "get_doc", return_value=self.existing_user),
            patch.object(svc.frappe, "log_error", self.log_error),
            patch.object(svc, "ensure_courier_role_profile", return_value="Jarz Courier"),
            patch.object(svc, "_set_pin", self.set_pin),
            patch.object(svc, "_mobile_login_enabled", return_value=self.mobile_login),
            patch.object(svc, "courier_links", return_value={"web_url": "https://erp.example/courier/", "apk_url": None}),
            patch.object(svc, "diagnose_courier_setup", return_value={"ok": True, "problems": []}),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def _create_employee(self, *, name, phone=None, branch=None):
        self.site.employees["HR-EMP-9"] = {
            "name": "HR-EMP-9",
            "employee_name": name,
            "status": "Active",
            "branch": branch,
            "user_id": None,
            "cell_number": phone,
        }
        return {"party_type": "Employee", "party": "HR-EMP-9", "display_name": name}

    def assertNoWrites(self) -> None:
        self.new_user.insert.assert_not_called()
        self.existing_user.save.assert_not_called()
        self.set_pin.assert_not_called()
        self.bridge.create_delivery_employee.assert_not_called()
        self.bridge.link_employee_to_user.assert_not_called()
        self.bridge.grant_branch_access.assert_not_called()
        self.assertEqual([], self.site.set_values)

    def add_existing_courier(self, roles=(ROLE,), enabled=1, linked=True):
        user = "ali@example.com"
        self.site.users[user] = {"mobile_no": "01012345678", "enabled": enabled}
        self.site.roles[user] = set(roles)
        if linked:
            self.site.employees["HR-EMP-1"]["user_id"] = user
        return user


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


class TestNormalizePhone(unittest.TestCase):
    def test_it_matches_the_courier_apps_normalization(self) -> None:
        cases = {
            "01012345678": "01012345678",
            "0101 234-5678": "01012345678",
            "(0101) 2345678": "01012345678",
            "+201012345678": "01012345678",
            "00201012345678": "01012345678",
            "201012345678": "01012345678",
            "": "",
            None: "",
        }
        for raw, expected in cases.items():
            self.assertEqual(expected, svc.normalize_phone(raw), raw)


class TestGeneratePin(unittest.TestCase):
    def test_six_digits_and_never_an_obvious_one(self) -> None:
        seen = set()
        for _ in range(3000):
            pin = svc.generate_pin()
            seen.add(pin)
            self.assertRegex(pin, r"^\d{6}$")
            self.assertNotEqual(1, len(set(pin)), pin)
            self.assertNotIn(pin, ("123456", "234567", "345678", "456789", "654321", "987654"))
        self.assertGreater(len(seen), 2000)  # actually random, not a constant

    def test_leading_zeros_survive(self) -> None:
        with patch.object(svc.secrets, "randbelow", return_value=42):
            self.assertEqual("000042", svc.generate_pin())

    def test_an_obvious_draw_is_redrawn(self) -> None:
        with patch.object(svc.secrets, "randbelow", side_effect=[111111, 123456, 654321, 482910]):
            self.assertEqual("482910", svc.generate_pin())


_real_ensure_role_profile = svc.ensure_courier_role_profile


# ---------------------------------------------------------------------------
# setup_courier
# ---------------------------------------------------------------------------


class TestSetupCourierNewLogin(OnboardingTestCase):
    def test_it_builds_the_whole_login_for_an_existing_employee(self) -> None:
        result = svc.setup_courier(employee="HR-EMP-1", branch="Dokki")

        email = "courier.01012345678@orderjarz.com"
        self.assertEqual(email, result["user"])
        self.assertEqual("HR-EMP-1", result["employee"])
        self.assertTrue(result["created_user"])

        # The User: generated email, the normalized mobile, no welcome mail, enabled.
        self.assertEqual(email, self.new_user.email)
        self.assertEqual("Mahmoud Ali", self.new_user.first_name)
        self.assertEqual("01012345678", self.new_user.mobile_no)
        self.assertEqual(1, self.new_user.enabled)
        self.assertEqual(0, self.new_user.send_welcome_email)
        self.assertEqual(0, self.new_user.custom_require_pos_shift)
        self.new_user.insert.assert_called_once()

        # Roles only through the Role Profile.
        self.bridge.apply_role_profiles.assert_called_once_with(self.new_user, ["Jarz Courier"])

        # Employee wiring: delivery group, branch, user link; branch access.
        self.bridge.ensure_employee_in_delivery_group.assert_called_once_with("HR-EMP-1")
        self.assertIn(("Employee", "HR-EMP-1", "branch", "Dokki"), self.site.set_values)
        self.bridge.link_employee_to_user.assert_called_once_with(email, "HR-EMP-1")
        self.bridge.grant_branch_access.assert_called_once()
        self.assertEqual((email, "Dokki"), self.bridge.grant_branch_access.call_args.args[:2])

        self.assertEqual({"web_url": "https://erp.example/courier/", "apk_url": None}, result["links"])
        self.assertEqual({"ok": True, "problems": []}, result["diagnosis"])

    def test_the_pin_is_six_digits_and_is_what_is_stored(self) -> None:
        result = svc.setup_courier(employee="HR-EMP-1", branch="Dokki")
        self.assertRegex(result["pin"], r"^\d{6}$")
        self.set_pin.assert_called_once_with(result["user"], result["pin"])

    def test_the_door_is_checked_first_and_a_refusal_writes_nothing(self) -> None:
        self.bridge.ensure_user_admin_access.side_effect = frappe.PermissionError("no")
        with self.assertRaises(frappe.PermissionError):
            svc.setup_courier(employee="HR-EMP-1", branch="Dokki")
        self.assertNoWrites()

    def test_login_id_is_the_email_when_mobile_login_is_off_and_it_says_so(self) -> None:
        result = svc.setup_courier(employee="HR-EMP-1", branch="Dokki")
        self.assertEqual(result["user"], result["login_id"])
        self.assertIn("mobile_login_disabled", result["warnings"])

    def test_mobile_arg_wins_and_is_normalized(self) -> None:
        self.new_user.name = "courier.01099998877@orderjarz.com"  # Frappe names a User by its email
        result = svc.setup_courier(employee="HR-EMP-1", branch="Dokki", mobile_no="+20 109 999 8877")
        self.assertEqual("01099998877", self.new_user.mobile_no)
        self.assertEqual("courier.01099998877@orderjarz.com", result["user"])

    def test_the_employees_cell_number_is_the_fallback(self) -> None:
        svc.setup_courier(employee="HR-EMP-1", branch="Dokki")
        self.assertEqual("01012345678", self.new_user.mobile_no)

    def test_the_employees_branch_is_the_default_profile(self) -> None:
        self.site.employees["HR-EMP-1"]["branch"] = "Maadi"
        svc.setup_courier(employee="HR-EMP-1")
        self.assertEqual("Maadi", self.bridge.grant_branch_access.call_args.args[1])
        # Already right, so the Employee row is not rewritten.
        self.assertEqual([], self.site.set_values)

    def test_pos_profile_alone_is_enough(self) -> None:
        svc.setup_courier(employee="HR-EMP-1", pos_profile="Maadi")
        self.assertEqual("Maadi", self.bridge.grant_branch_access.call_args.args[1])

    def test_an_existing_membership_is_not_granted_again(self) -> None:
        self.site.member_of.add(("courier.01012345678@orderjarz.com", "Dokki"))
        svc.setup_courier(employee="HR-EMP-1", branch="Dokki")
        self.bridge.grant_branch_access.assert_not_called()

    def test_an_open_branch_refusal_from_jarz_pos_surfaces(self) -> None:
        self.bridge.grant_branch_access.side_effect = frappe.ValidationError("Branch has an open shift")
        with self.assertRaises(frappe.ValidationError):
            svc.setup_courier(employee="HR-EMP-1", branch="Dokki")
        self.set_pin.assert_not_called()  # no PIN is issued for an account that is not usable

    def test_sessions_are_signed_out_and_a_failure_there_does_not_fail_setup(self) -> None:
        self.bridge.sign_out_everywhere.side_effect = RuntimeError("redis down")
        result = svc.setup_courier(employee="HR-EMP-1", branch="Dokki")
        self.assertTrue(result["pin"])
        self.assertNotIn(result["pin"], repr(self.log_error.call_args_list))


class TestSetupCourierMobileLogin(OnboardingTestCase):
    mobile_login = True

    def test_login_id_is_the_mobile_when_the_site_allows_it(self) -> None:
        result = svc.setup_courier(employee="HR-EMP-1", branch="Dokki")
        self.assertEqual("01012345678", result["login_id"])
        self.assertEqual([], result["warnings"])


class TestSetupCourierNewEmployee(OnboardingTestCase):
    def test_a_name_creates_the_employee_in_the_delivery_group(self) -> None:
        result = svc.setup_courier(full_name="Sayed Hassan", mobile_no="01155556666", branch="Maadi")

        self.bridge.create_delivery_employee.assert_called_once_with(
            name="Sayed Hassan", phone="01155556666", branch="Maadi"
        )
        self.assertEqual("HR-EMP-9", result["employee"])
        self.bridge.ensure_employee_in_delivery_group.assert_called_once_with("HR-EMP-9")
        self.bridge.link_employee_to_user.assert_called_once()
        self.assertEqual("Sayed Hassan", self.new_user.first_name)

    def test_a_same_name_employee_is_not_adopted(self) -> None:
        with self.assertRaises(frappe.ValidationError):
            svc.setup_courier(full_name="Mahmoud Ali", mobile_no="01155556666", branch="Maadi")
        self.assertNoWrites()

    def test_a_new_employee_needs_a_mobile_number(self) -> None:
        with self.assertRaises(frappe.ValidationError):
            svc.setup_courier(full_name="Sayed Hassan", branch="Maadi")
        self.assertNoWrites()


class TestSetupCourierRefusals(OnboardingTestCase):
    def test_nothing_to_set_up(self) -> None:
        with self.assertRaises(frappe.ValidationError):
            svc.setup_courier()
        self.assertNoWrites()

    def test_unknown_employee(self) -> None:
        with self.assertRaises(frappe.ValidationError):
            svc.setup_courier(employee="HR-EMP-404", branch="Dokki")
        self.assertNoWrites()

    def test_inactive_employee(self) -> None:
        self.site.employees["HR-EMP-1"]["status"] = "Left"
        with self.assertRaises(frappe.ValidationError):
            svc.setup_courier(employee="HR-EMP-1", branch="Dokki")
        self.assertNoWrites()

    def test_a_mobile_that_is_not_an_egyptian_mobile(self) -> None:
        for bad in ("12345", "0222345678", "01012345", "abc"):
            with self.assertRaises(frappe.ValidationError, msg=bad):
                svc.setup_courier(employee="HR-EMP-1", branch="Dokki", mobile_no=bad)
        self.assertNoWrites()

    def test_a_mobile_another_user_already_has(self) -> None:
        self.site.users["someone@example.com"] = {"mobile_no": "01012345678", "enabled": 1}
        with self.assertRaises(frappe.ValidationError):
            svc.setup_courier(employee="HR-EMP-1", branch="Dokki")
        self.assertNoWrites()

    def test_branch_and_profile_that_disagree(self) -> None:
        with self.assertRaises(frappe.ValidationError):
            svc.setup_courier(employee="HR-EMP-1", branch="Dokki", pos_profile="Maadi")
        self.assertNoWrites()

    def test_no_branch_anywhere(self) -> None:
        with self.assertRaises(frappe.ValidationError):
            svc.setup_courier(employee="HR-EMP-1")
        self.assertNoWrites()

    def test_unknown_or_disabled_pos_profile(self) -> None:
        self.site.profiles["Old"] = {"name": "Old", "disabled": 1}
        for profile in ("Nowhere", "Old"):
            with self.assertRaises(frappe.ValidationError, msg=profile):
                svc.setup_courier(employee="HR-EMP-1", pos_profile=profile)
        self.assertNoWrites()

    def test_an_account_with_other_roles_is_not_touched(self) -> None:
        self.add_existing_courier(roles=(ROLE, "JARZ line manager"))
        with self.assertRaises(frappe.ValidationError):
            svc.setup_courier(employee="HR-EMP-1", branch="Dokki")
        self.assertNoWrites()
        self.bridge.apply_role_profiles.assert_not_called()

    def test_the_users_screen_protection_rules_apply(self) -> None:
        self.add_existing_courier()
        self.bridge.ensure_can_modify_user.side_effect = frappe.PermissionError("manager account")
        with self.assertRaises(frappe.PermissionError):
            svc.setup_courier(employee="HR-EMP-1", branch="Dokki")
        self.assertNoWrites()

    def test_a_disabled_login_is_not_silently_revived(self) -> None:
        self.add_existing_courier(enabled=0)
        with self.assertRaises(frappe.ValidationError):
            svc.setup_courier(employee="HR-EMP-1", branch="Dokki")
        self.assertNoWrites()

    def test_a_generated_email_that_belongs_to_another_employee(self) -> None:
        email = "courier.01012345678@orderjarz.com"
        self.site.users[email] = {"mobile_no": "01012345678", "enabled": 1}
        self.site.roles[email] = {ROLE}
        self.site.employees["HR-EMP-2"] = {
            "name": "HR-EMP-2",
            "employee_name": "Other",
            "status": "Active",
            "branch": None,
            "user_id": email,
            "cell_number": None,
        }
        with self.assertRaises(frappe.ValidationError):
            svc.setup_courier(employee="HR-EMP-1", branch="Dokki")
        self.assertNoWrites()


class TestSetupCourierRerun(OnboardingTestCase):
    def test_an_existing_courier_login_is_reused_and_gets_a_new_pin(self) -> None:
        user = self.add_existing_courier()
        self.site.employees["HR-EMP-1"]["branch"] = "Dokki"
        self.site.member_of.add((user, "Dokki"))

        result = svc.setup_courier(employee="HR-EMP-1")

        self.assertEqual(user, result["user"])
        self.assertFalse(result["created_user"])
        self.new_user.insert.assert_not_called()
        self.existing_user.save.assert_called_once()
        self.bridge.apply_role_profiles.assert_called_once_with(self.existing_user, ["Jarz Courier"])
        self.bridge.link_employee_to_user.assert_not_called()  # already linked
        self.bridge.grant_branch_access.assert_not_called()  # already a member
        self.set_pin.assert_called_once_with(user, result["pin"])

    def test_the_users_own_mobile_is_used_when_nothing_else_is_given(self) -> None:
        self.add_existing_courier()
        self.site.employees["HR-EMP-1"]["cell_number"] = None
        self.site.employees["HR-EMP-1"]["branch"] = "Dokki"
        svc.setup_courier(employee="HR-EMP-1")
        self.assertEqual("01012345678", self.existing_user.mobile_no)


# ---------------------------------------------------------------------------
# reset_courier_pin
# ---------------------------------------------------------------------------


class TestResetCourierPin(OnboardingTestCase):
    def test_it_issues_a_new_pin_for_a_courier_login(self) -> None:
        user = self.add_existing_courier()
        result = svc.reset_courier_pin(user)

        self.assertEqual({"user", "login_id", "pin"}, set(result))
        self.assertEqual(user, result["user"])
        self.assertRegex(result["pin"], r"^\d{6}$")
        self.set_pin.assert_called_once_with(user, result["pin"])
        self.bridge.sign_out_everywhere.assert_called_once_with(user)
        self.assertEqual(user, result["login_id"])  # mobile login is off here

    def test_the_login_id_is_the_mobile_when_the_site_allows_it(self) -> None:
        user = self.add_existing_courier()
        with patch.object(svc, "_mobile_login_enabled", return_value=True):
            self.assertEqual("01012345678", svc.reset_courier_pin(user)["login_id"])

    def test_the_door_is_checked(self) -> None:
        user = self.add_existing_courier()
        self.bridge.ensure_user_admin_access.side_effect = frappe.PermissionError("no")
        with self.assertRaises(frappe.PermissionError):
            svc.reset_courier_pin(user)
        self.set_pin.assert_not_called()

    def test_a_manager_or_cashier_account_is_never_given_a_six_digit_pin(self) -> None:
        user = self.add_existing_courier(roles=(ROLE, "JARZ Manager"))
        with self.assertRaises(frappe.ValidationError):
            svc.reset_courier_pin(user)
        self.set_pin.assert_not_called()

    def test_an_account_that_is_not_a_courier_is_refused(self) -> None:
        user = self.add_existing_courier(roles=())
        with self.assertRaises(frappe.ValidationError):
            svc.reset_courier_pin(user)
        self.set_pin.assert_not_called()

    def test_a_protected_account_is_refused_by_the_users_screen_rules(self) -> None:
        user = self.add_existing_courier()
        self.bridge.ensure_can_modify_user.side_effect = frappe.PermissionError("protected")
        with self.assertRaises(frappe.PermissionError):
            svc.reset_courier_pin(user)
        self.set_pin.assert_not_called()

    def test_a_disabled_account_is_refused(self) -> None:
        user = self.add_existing_courier(enabled=0)
        with self.assertRaises(frappe.ValidationError):
            svc.reset_courier_pin(user)
        self.set_pin.assert_not_called()

    def test_a_blank_user_is_refused(self) -> None:
        with self.assertRaises(frappe.ValidationError):
            svc.reset_courier_pin("  ")


# ---------------------------------------------------------------------------
# get_courier_setup / list_courier_candidates
# ---------------------------------------------------------------------------


class TestGetCourierSetup(OnboardingTestCase):
    def test_it_returns_the_diagnosis_behind_the_door(self) -> None:
        with patch.object(svc, "diagnose_courier_setup", return_value={"ok": False, "problems": ["no_pos_profile"]}) as d:
            result = svc.get_courier_setup(" ali@example.com ")
        self.bridge.ensure_user_admin_access.assert_called_once()
        d.assert_called_once_with("ali@example.com")
        self.assertEqual(["no_pos_profile"], result["problems"])

    def test_a_refused_door_returns_nothing(self) -> None:
        self.bridge.ensure_user_admin_access.side_effect = frappe.PermissionError("no")
        with patch.object(svc, "diagnose_courier_setup") as d:
            with self.assertRaises(frappe.PermissionError):
                svc.get_courier_setup("ali@example.com")
        d.assert_not_called()


class TestListCandidates(OnboardingTestCase):
    def _lists(self, tables):
        def get_all(doctype, **kwargs):
            data = tables.get(doctype, [])
            if kwargs.get("pluck"):
                return [r[kwargs["pluck"]] if isinstance(r, dict) else r for r in data]
            return data

        return patch.object(svc.frappe, "get_all", side_effect=get_all)

    def test_each_employee_gets_its_own_problems(self) -> None:
        tables = {
            "Employee": [
                {"name": "E1", "employee_name": "Ahmed", "branch": "Dokki", "user_id": "a@x.com"},  # fine
                {"name": "E2", "employee_name": "Bassem", "branch": "Dokki", "user_id": None},  # no login
                {"name": "E3", "employee_name": "Chady", "branch": None, "user_id": "c@x.com"},  # no branch
                {"name": "E4", "employee_name": "Dina", "branch": "Dokki", "user_id": "d@x.com"},  # wrong branch row
                {"name": "E5", "employee_name": "Eman", "branch": "Dokki", "user_id": "e@x.com"},  # disabled, no role
            ],
            "POS Profile": ["Dokki", "Maadi"],
            "User": [
                {"name": "a@x.com", "enabled": 1},
                {"name": "c@x.com", "enabled": 1},
                {"name": "d@x.com", "enabled": 1},
                {"name": "e@x.com", "enabled": 0},
            ],
            "Has Role": ["a@x.com", "c@x.com", "d@x.com"],
            "POS Profile User": [
                {"user": "a@x.com", "parent": "Dokki"},
                {"user": "c@x.com", "parent": "Dokki"},
                {"user": "d@x.com", "parent": "Maadi"},
                {"user": "e@x.com", "parent": "Gone"},  # a deleted/disabled profile does not count
            ],
        }
        self.bridge.delivery_group_employees.return_value = {"E1", "E3"}
        with self._lists(tables):
            result = svc.list_courier_candidates()

        self.assertEqual(["Dokki", "Maadi"], result["branches"])
        by_id = {e["employee"]: e for e in result["employees"]}
        self.assertEqual(
            {"employee", "employee_name", "branch", "user", "is_courier", "setup_ok", "problems"},
            set(by_id["E1"]),
        )
        self.assertTrue(by_id["E1"]["setup_ok"])
        self.assertTrue(by_id["E1"]["is_courier"])
        self.assertEqual(["no_identity"], by_id["E2"]["problems"])
        self.assertFalse(by_id["E2"]["is_courier"])
        self.assertIsNone(by_id["E2"]["user"])
        self.assertEqual(["no_employee_branch"], by_id["E3"]["problems"])
        self.assertEqual(["branch_not_assigned"], by_id["E4"]["problems"])
        self.assertEqual(
            ["user_disabled", "no_courier_role", "no_pos_profile"], by_id["E5"]["problems"]
        )
        self.assertFalse(by_id["E5"]["setup_ok"])

    def test_no_employees_is_an_empty_list_not_an_error(self) -> None:
        with self._lists({"POS Profile": ["Dokki"]}):
            self.assertEqual({"employees": [], "branches": ["Dokki"]}, svc.list_courier_candidates())

    def test_the_door_is_checked(self) -> None:
        self.bridge.ensure_user_admin_access.side_effect = frappe.PermissionError("no")
        with self.assertRaises(frappe.PermissionError):
            svc.list_courier_candidates()


# ---------------------------------------------------------------------------
# The Role Profile
# ---------------------------------------------------------------------------


class TestEnsureRoleProfile(unittest.TestCase):
    def _run(self, *, role_exists=True, profile_exists=False, profile_has_role=True):
        def exists(doctype, filters=None):
            if doctype == "Role":
                return role_exists
            if doctype == "Role Profile":
                return profile_exists
            return None

        profile = MagicMock()
        with patch.object(svc.frappe.db, "exists", side_effect=exists), patch.object(
            svc.frappe, "get_all", return_value=[ROLE] if profile_has_role else []
        ), patch.object(svc.frappe, "get_doc", return_value=profile) as get_doc:
            name = _real_ensure_role_profile()
        return name, get_doc, profile

    def test_it_is_created_with_only_the_courier_role(self) -> None:
        name, get_doc, profile = self._run()
        self.assertEqual("Jarz Courier", name)
        spec = get_doc.call_args.args[0]
        self.assertEqual("Role Profile", spec["doctype"])
        self.assertEqual("Jarz Courier", spec["role_profile"])
        self.assertEqual([{"role": ROLE}], spec["roles"])
        profile.insert.assert_called_once()

    def test_an_existing_profile_is_left_alone(self) -> None:
        name, get_doc, _ = self._run(profile_exists=True)
        self.assertEqual("Jarz Courier", name)
        get_doc.assert_not_called()

    def test_a_missing_role_means_run_migrate(self) -> None:
        with self.assertRaises(frappe.ValidationError):
            self._run(role_exists=False)

    def test_an_existing_profile_without_the_role_is_refused(self) -> None:
        with self.assertRaises(frappe.ValidationError):
            self._run(profile_exists=True, profile_has_role=False)


# ---------------------------------------------------------------------------
# api/onboarding - thin, and never leaks a PIN into the error log
# ---------------------------------------------------------------------------


class TestApi(unittest.TestCase):
    def test_every_endpoint_is_whitelisted(self) -> None:
        for fn in (api.list_courier_candidates, api.setup_courier, api.reset_courier_pin, api.get_courier_setup):
            self.assertTrue(getattr(fn, "whitelisted", True))

    def test_setup_delegates_with_every_argument(self) -> None:
        with patch.object(api.courier_onboarding, "setup_courier", return_value={"pin": "135790"}) as s:
            result = api.setup_courier("HR-EMP-1", "Name", "0101", "Dokki", "Dokki")
        s.assert_called_once_with(
            employee="HR-EMP-1", full_name="Name", mobile_no="0101", branch="Dokki", pos_profile="Dokki"
        )
        self.assertEqual({"pin": "135790"}, result)

    def test_reset_and_get_and_list_delegate(self) -> None:
        with patch.object(api.courier_onboarding, "reset_courier_pin", return_value={"pin": "1"}) as r, patch.object(
            api.courier_onboarding, "get_courier_setup", return_value={"ok": True}
        ) as g, patch.object(
            api.courier_onboarding, "list_courier_candidates", return_value={"employees": [], "branches": []}
        ) as lst:
            api.reset_courier_pin("a@x.com")
            api.get_courier_setup("a@x.com")
            api.list_courier_candidates()
        r.assert_called_once_with("a@x.com")
        g.assert_called_once_with("a@x.com")
        lst.assert_called_once_with()

    def test_permission_and_validation_errors_pass_through(self) -> None:
        with patch.object(api.courier_onboarding, "setup_courier", side_effect=frappe.PermissionError("no")):
            with self.assertRaises(frappe.PermissionError):
                api.setup_courier(employee="HR-EMP-1")
        with patch.object(api.courier_onboarding, "reset_courier_pin", side_effect=frappe.ValidationError("bad")):
            with self.assertRaises(frappe.ValidationError):
                api.reset_courier_pin("a@x.com")

    def test_an_unexpected_failure_is_logged_without_the_arguments_or_a_pin(self) -> None:
        with patch.object(
            api.courier_onboarding, "setup_courier", side_effect=RuntimeError("db gone")
        ), patch.object(api.frappe, "log_error") as log_error, patch.object(
            api.frappe, "get_traceback", return_value="Traceback ... RuntimeError: db gone"
        ):
            with self.assertRaises(frappe.ValidationError):
                api.setup_courier(employee="HR-EMP-1", mobile_no="01012345678")
        logged = repr(log_error.call_args_list)
        self.assertNotIn("01012345678", logged)

    def test_the_candidate_list_degrades_to_empty(self) -> None:
        with patch.object(api.courier_onboarding, "list_courier_candidates", side_effect=RuntimeError("x")), patch.object(
            api.frappe, "log_error"
        ):
            result = api.list_courier_candidates()
        self.assertEqual([], result["employees"])
        self.assertEqual([], result["branches"])


if __name__ == "__main__":
    unittest.main()
