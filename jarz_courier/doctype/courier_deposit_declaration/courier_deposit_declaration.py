"""Controller for Courier Deposit Declaration.

This is a **claim**, not a posting. A courier declares "I handed over 4,200"; a
manager confirms it; only then does jarz_pos move any money, and only jarz_pos
records which entry it created (``journal_entry``, read-only here).

The controller guards the two ways that separation gets broken by accident:

* **A settled declaration is immutable.** Once ``journal_entry`` is populated the
  row is the human-readable half of a posted entry. Editing the amount afterwards
  produces a record that disagrees with the ledger it points at — and the ledger
  is the one that is audited.
* **Terminal statuses are terminal.** Confirmed and Rejected never go back to
  Pending. Re-confirming a Confirmed declaration is how the same hand-over gets
  posted twice.

The controller does NOT enforce "a courier may not confirm their own
declaration" — that is a role question and it lives in ``api/statement.py``,
where the caller's roles are known. Duplicating it here would be a second answer
to the same question, which is exactly the drift jarz_pos's access_control module
was written to end.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt, now_datetime

DOCTYPE = "Courier Deposit Declaration"

STATUS_PENDING = "Pending"
STATUS_CONFIRMED = "Confirmed"
STATUS_REJECTED = "Rejected"
_TERMINAL = (STATUS_CONFIRMED, STATUS_REJECTED)

#: Fields that describe what was handed over. Frozen once money has been posted.
_DECLARATION_FIELDS = ("amount", "method", "reference", "party_type", "party", "branch")


class CourierDepositDeclaration(Document):
    def validate(self) -> None:
        if not self.declared_on:
            self.declared_on = now_datetime()

        self.reference = str(self.reference or "").strip() or None
        self.request_id = str(self.request_id or "").strip() or None

        if flt(self.amount) <= 0:
            frappe.throw(_("Declared amount must be greater than zero"))

        self._validate_status_transition()
        self._validate_immutable_after_posting()

        if self.status == STATUS_CONFIRMED and not self.confirmed_on:
            self.confirmed_on = now_datetime()
        if self.status == STATUS_REJECTED and not str(self.rejection_reason or "").strip():
            frappe.throw(_("A rejected declaration must say why"))

    def _validate_status_transition(self) -> None:
        if self.is_new():
            if self.status != STATUS_PENDING:
                frappe.throw(_("A new declaration must start as Pending"))
            return

        previous = self.get_doc_before_save()
        if previous is None:
            return

        was, now = previous.status, self.status
        if was == now:
            return
        if was in _TERMINAL:
            frappe.throw(
                _("Declaration {0} is already {1} and cannot be changed to {2}.").format(
                    self.name, was, now
                )
            )

    def _validate_immutable_after_posting(self) -> None:
        if self.is_new() or not self.journal_entry:
            return

        previous = self.get_doc_before_save()
        if previous is None or not previous.journal_entry:
            return

        changed = [f for f in _DECLARATION_FIELDS if previous.get(f) != self.get(f)]
        if changed:
            frappe.throw(
                _(
                    "Declaration {0} has already been posted (entry {1}); {2} cannot be "
                    "changed. Reverse the entry in the ledger instead."
                ).format(self.name, previous.journal_entry, ", ".join(changed))
            )

    def on_trash(self) -> None:
        if self.journal_entry:
            frappe.throw(
                _(
                    "Declaration {0} has been posted (entry {1}) and cannot be deleted."
                ).format(self.name, self.journal_entry)
            )
