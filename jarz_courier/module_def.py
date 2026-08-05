"""Module definition helper so the module appears in the Desk Module view."""

from frappe import _


def get_data():
    return [
        {
            "module_name": "Jarz Courier",
            "category": "Modules",
            "label": _("Jarz Courier"),
            "color": "#e67e22",
            "icon": "octicon octicon-rocket",
            "type": "module",
            "description": "Courier run sheet, proof of delivery, duty and statement",
        }
    ]
