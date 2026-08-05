"""Jarz Courier — courier-facing execution layer on top of ``jarz_pos``.

Module path shim
----------------
Frappe resolves a module's on-disk directory through
``frappe.modules.utils.get_module_path`` → ``get_pymodule_path("<app>.<module>")``,
i.e. for the module ``Jarz Courier`` in the app ``jarz_courier`` it imports the
python module ``jarz_courier.jarz_courier`` and takes ``dirname(__file__)``.

This app (like ``jarz_pos`` and ``jarz_woocommerce_integration``) uses the flat
layout — ``doctype/``, ``api/``, ``services/`` sit directly inside the app
package rather than inside a second nested ``jarz_courier/`` directory. The
alias registered below makes ``jarz_courier.jarz_courier`` resolve to *this*
package, so ``get_module_path`` returns this directory and DocType JSON under
``jarz_courier/doctype/<slug>/`` is found by ``bench migrate``.

Remove the shim only together with a physical move of ``doctype/`` into a nested
``jarz_courier/jarz_courier/jarz_courier/`` directory — dropping one without the
other makes every DocType in this app invisible to migrate, silently.

Boundary (COURIER_CONTRACTS.md §9)
----------------------------------
* ``jarz_courier`` may import ``jarz_pos``. The reverse is forbidden.
* ``jarz_courier`` must NEVER import ``jarz_woocommerce_integration``.
* ``jarz_courier`` never writes GL, never creates a Journal Entry and never
  inserts a ``Courier Transaction``. See ``jarz_courier/tests/test_no_gl_writes.py``.
"""

from __future__ import annotations

__version__ = "0.0.1"
__all__ = ["__version__"]

import importlib as _importlib
import sys as _sys

_MODULE_ALIAS_ROOT = __name__ + ".jarz_courier"


class _ModuleDirAlias:
    """Module proxy that makes ``jarz_courier.jarz_courier.<x>`` resolve to ``jarz_courier.<x>``.

    ``__file__`` is mirrored from the canonical package so
    ``get_pymodule_path`` computes this directory as the module path.
    """

    def __init__(self, alias_pkg: str) -> None:
        self.__name__ = alias_pkg
        self.__package__ = alias_pkg
        base = _importlib.import_module(__name__)
        self._base_mod = base
        for _attr in ("__file__", "__path__", "__spec__", "__loader__"):
            try:
                setattr(self, _attr, getattr(base, _attr, None))
            except Exception:
                pass

    def __getattr__(self, name: str):
        if name.startswith("__"):
            try:
                return getattr(self._base_mod, name)
            except Exception as exc:
                raise AttributeError(name) from exc
        target_pkg = f"{__name__}.{name}"
        try:
            mod = _importlib.import_module(target_pkg)
        except Exception as exc:
            raise AttributeError(name) from exc
        _sys.modules[f"{_MODULE_ALIAS_ROOT}.{name}"] = mod
        return mod


if _MODULE_ALIAS_ROOT not in _sys.modules:
    _sys.modules[_MODULE_ALIAS_ROOT] = _ModuleDirAlias(_MODULE_ALIAS_ROOT)
