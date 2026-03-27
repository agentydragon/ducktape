"""Ensure a dconf array key contains and/or omits the requested items.

PARAMETERS
  key (str): dconf array key
  add (str | list[str]): item(s) to add if not present (optional)
  remove (str | list[str]): item(s) to remove if present (optional)

EXAMPLES
  # Ensure an item is present
  - dconf_array_edit:
      key: /org/gnome/shell/favorite-apps
      add: firefox.desktop

  # Ensure multiple items are present
  - dconf_array_edit:
      key: /org/gnome/shell/favorite-apps
      add:
        - org.gnome.Terminal.desktop
        - firefox.desktop

  # Remove one item and add another in a single call
  - dconf_array_edit:
      key: /org/gnome/shell/favorite-apps
      add: firefox.desktop
      remove: org.gnome.Nautilus.desktop

Implementation is idempotent: *changed* is True only if array was changed.
"""

from __future__ import annotations

from collections.abc import Iterable

from ansible.errors import AnsibleError
from ansible.plugins.action import ActionBase
from gi.repository import GLib


def _array_to_list(raw: str | None) -> list[str]:
    """Parse raw dconf array representation (e.g. "['a', 'b']") to list.

    The ansible.dconf `state=read` result is the *printed* form of a GLib
    variant which may itself be wrapped in a variant of type `v`. This helper
    unwraps such an outer variant and returns a plain Python list.
    """

    if not raw:
        return []

    v = GLib.Variant.parse(None, raw, None, None)  # parses "@as []" OK
    if v.get_type_string() == "v":  # 'v' = variant wrapper
        v = v.get_child_value(0)  # unwrap once

    return list(v.unpack())


def _list_to_array(lst: Iterable[str]) -> str:
    """Return textual representation of *lst* for ansible.dconf.

    ansible.builtin.dconf expects the *printed* form of a GLib variant that
    itself contains a variant of type ``as`` (array of strings).
    """
    return GLib.Variant("v", GLib.Variant("as", list(lst))).print_(False)


class ActionModule(ActionBase):
    def run(self, tmp=None, task_vars=None):
        result = super().run(tmp, task_vars)

        args = self._task.args
        if "key" not in args:
            raise AnsibleError("'key' is required")

        if "add" not in args and "remove" not in args:
            raise AnsibleError("'add' and/or 'remove' parameter must be supplied")

        def _normalise(v) -> list[str]:
            if v is None:
                return []
            if isinstance(v, list | tuple | set):
                return list(v)
            return [v]

        add: list[str] = _normalise(args.get("add"))
        remove: list[str] = _normalise(args.get("remove"))

        def _dconf(**kwargs):
            return self._execute_module(
                module_name="ansible.builtin.dconf",
                module_args=kwargs | {"key": args["key"]},
                task_vars=task_vars,
                tmp=tmp,
            )

        # 1. Read current array
        before_raw: str | None = _dconf(state="read").get("value")
        result["before"] = before_raw
        current: list[str] = _array_to_list(before_raw)

        # 2. Calculate desired state
        desired: list[str] = [item for item in current if item not in remove]

        for item in add:
            if item not in desired:
                desired.append(item)

        # 3. Compare / maybe write
        if desired == current:
            return result | {"changed": False, "after": before_raw}

        after_raw = _list_to_array(desired)

        if not self._play_context.check_mode:
            _dconf(value=after_raw)

        return result | {"changed": True, "after": after_raw}
