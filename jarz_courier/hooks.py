app_name = "jarz_courier"
app_title = "Jarz Courier"
app_publisher = "Abdelrahman Mamdouh"
app_description = "Courier execution layer for Jarz POS — run sheet, proof of delivery, duty sessions, courier statement."
app_email = "abdelrahmanmamdouh1996@gmail.com"
app_license = "mit"

# Apps
# ------------------
#
# COURIER_CONTRACTS.md §9 — the dependency is ONE WAY and it is declared here so
# bench refuses to install this app onto a site without jarz_pos. `jarz_pos` and
# `jarz_woocommerce_integration` must never import `jarz_courier`; the reverse is
# allowed and is how every money write in this app is performed.
required_apps = ["jarz_pos"]


# Fixtures
# ------------------
#
# DELIBERATELY EMPTY, and it must stay that way for Custom Fields.
#
# COURIER_CONTRACTS.md §9: this app declares ZERO Custom Fields on any doctype
# that jarz_pos touches (Sales Invoice, Address, Customer, Employee, POS Profile,
# User, Item, Lead, ...). jarz_pos runs
# `utils.cleanup.remove_colliding_custom_fields_for_fixtures` as a before_migrate
# hook, which deletes Custom Fields matching its fixture list by dt+fieldname
# whenever the record `name` differs. Two apps shipping the same field means
# whichever migrates second wins and the other's field disappears silently, with
# no error and no failing migrate — you find out when a courier's "Delivered" tap
# stops persisting.
#
# All shared schema on Sales Invoice / Address is owned by jarz_pos lane P1,
# exclusively. If this app needs a field on a shared doctype, the answer is a PR
# to jarz_pos's fixture file, not an entry here.
fixtures = []


# Installation
# ------------------

after_install = "jarz_courier.setup.courier_app_setup.ensure_courier_app_setup"


# Migration
# ------------------
#
# COURIER_CONTRACTS.md §9: "Every migrate hook swallows exceptions — a raising
# seeder aborts the shared `bench migrate`." `bench migrate` runs for the whole
# bench, so an unhandled error in a jarz_courier seeder takes jarz_pos's
# migration down with it (including its account seeding and workspace rebuild).
# `ensure_courier_app_setup` is create-only, idempotent, and catches everything.
after_migrate = [
    "jarz_courier.setup.courier_app_setup.ensure_courier_app_setup",
]


# Document Events
# ---------------
#
# None. This app hooks nothing on jarz_pos's or ERPNext's doctypes: a doc_event
# on Sales Invoice would be a second, untested writer of invoice state running
# inside jarz_pos's transactions. Courier-driven invoice changes go through
# `jarz_pos.services.courier_delivery.mark_invoice_*` instead.
doc_events = {}


# Scheduled Tasks
# ---------------
#
# None in P1. The consensus-pin clustering job (spec module B5) lands in P2 and
# will call `jarz_pos.services.geo_resolution` rather than writing Address
# fields itself.
scheduler_events = {}


# Ensure API modules are imported at startup so @frappe.whitelist() decorators
# register even when nothing has called into them yet. Each block is guarded:
# an import error here would otherwise break booting the whole site.
try:
    from jarz_courier.api import device as _device

    _device.register_device
    _device.unbind_device
    _device.get_my_device
except Exception:
    pass

try:
    from jarz_courier.api import duty as _duty

    _duty.start_duty
    _duty.end_duty
    _duty.get_duty_summary
except Exception:
    pass

try:
    from jarz_courier.api import run as _run

    _run.get_my_run
    _run.get_stop_detail
    _run.mark_arrived
    _run.mark_delivered
    _run.mark_failed
except Exception:
    pass

try:
    from jarz_courier.api import pod as _pod

    _pod.upload_proof
    _pod.get_proofs
except Exception:
    pass

try:
    from jarz_courier.api import statement as _statement

    _statement.get_statement
    _statement.declare_deposit
    _statement.confirm_deposit
    _statement.reject_deposit
    _statement.list_pending_deposits
except Exception:
    pass
