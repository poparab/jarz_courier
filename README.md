# Jarz Courier

Courier-facing backend for Jarz: run sheet, proof of delivery, duty sessions and
the courier's own statement. Layered on `jarz_pos` — never a peer to it.

Normative specs live in the `jarz_pos` repo:
`COURIER_CONTRACTS.md` (frozen contracts) and `COURIER_APP_SPEC.md` (scope).

## The boundary (COURIER_CONTRACTS.md §9)

1. `required_apps = ["jarz_pos"]`. One way: this app may import `jarz_pos`;
   `jarz_pos` and `jarz_woocommerce_integration` must never import this app.
2. **Never writes GL, never creates a Journal Entry, never inserts a
   `Courier Transaction`.** It reads the ledger freely and writes *through*
   `jarz_pos` service functions. The GL audit suite covers `jarz_pos` only, so
   money logic here would be untested and would create a second source of truth.
3. **Zero Custom Fields** on any doctype `jarz_pos` touches. `jarz_pos`'s
   collision cleanup deletes Custom Fields whose record `name` differs from its
   own fixture's, so whichever app migrates second wins and the other's field
   disappears with no error. All shared schema is `jarz_pos`'s, exclusively.
4. Every migrate hook swallows its exceptions — a raising seeder aborts the
   shared `bench migrate` that `jarz_pos` also depends on.
5. Never imports `jarz_woocommerce_integration`.

Rules 2, 3 and 5 are machine-checked by `jarz_courier/tests/test_no_gl_writes.py`,
which is listed in the `MODULES` array of `.github/workflows/backend-tests.yml`.

## Layout

```
jarz_courier/
├─ hooks.py                 required_apps, empty fixtures, after_migrate seeder
├─ constants.py             doctype names, roles, statuses; WS_EVENTS re-exported
├─ api/                     thin transport, {"success": bool} envelope
│  ├─ device.py             register / unbind / get_my_device
│  ├─ duty.py               start / end duty, duty summary
│  ├─ run.py                get_my_run, get_stop_detail, mark_arrived/delivered/failed
│  ├─ pod.py                upload_proof, get_proofs
│  └─ statement.py          get_statement (read-only), declare/confirm/reject deposit
├─ services/
│  ├─ pos_bridge.py         THE ONLY module that calls into jarz_pos
│  ├─ courier_onboarding.py POS Profile User + Employee.branch validator
│  ├─ device_registry.py    binding resolution
│  ├─ duty_session.py       duty lifecycle + reconciliation summary
│  ├─ run_sheet.py          the run-sheet QUERY over Sales Invoice
│  ├─ proof_of_delivery.py  offline-tolerant POD capture
│  ├─ ledger_read.py        READ-ONLY over the ledger (test-enforced)
│  └─ deposits.py           the only money-path write: the declaration
├─ doctype/                 Courier Device, Courier Duty, Delivery Proof,
│                           Courier Deposit Declaration
├─ setup/courier_app_setup.py   idempotent, exception-swallowing after_migrate seeder
└─ tests/                   pure unittest + mocks, no site required
```

## Two design points worth knowing before editing

**The run sheet is a query, not a document.** The courier is assigned on
`Sales Invoice.custom_courier_party`, so today's stops are the submitted invoices
carrying that party in state `Out for Delivery`, branch-scoped. There is no
`Courier Run` doctype in P1 and `Delivery Trip` is not touched at all.

**A courier needs two records, not one.** A `POS Profile User` row linking the
login to a POS Profile, *and* `Employee.branch` set to that same profile name.
Miss the first and the run sheet silently renders empty; miss the second and
`jarz_pos` throws an opaque error. `services/courier_onboarding.py` collapses both
into one message that names the form to open.

## Running the tests

No site, no database, no `jarz_pos` on disk:

```bash
python -m unittest discover -s jarz_courier/tests -t .
```

Inside the bench container (as CI runs them):

```bash
bench --site frontend run-tests --app jarz_courier \
  --module jarz_courier.tests.test_no_gl_writes --skip-before-tests
```

## Deployment note

`deploy_backend.ps1` has **no `bench install-app` anywhere** — only backup,
migrate and clear-cache — and `bench migrate` will not create a new app's
DocTypes. `jarz_courier` must be installed on each site once, by hand or through
an idempotent bootstrap, before it deploys like the others. Adding it to
`$deployedApps` before that bootstrap exists breaks *every* backend deploy,
including POS hotfixes, because `Resolve-GitTarget` hard-throws on a missing
clone. See COURIER_APP_SPEC.md §7.
