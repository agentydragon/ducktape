from dataclasses import dataclass

from ansible.errors import AnsibleError
from ansible.plugins.action import ActionBase


@dataclass
class AptInstall:
    name: str

    @classmethod
    def parse(cls, val):
        if not isinstance(val, str):
            raise AnsibleError("Invalid apt value")
        return cls(name=val)

    def module_args(self, installed):
        return {"name": self.name, "state": "present" if installed else "absent"}

    @property
    def module_name(self):
        return "ansible.builtin.apt"


@dataclass
class SnapInstall:
    name: str
    kwargs: dict

    @property
    def module_name(self):
        return "community.general.snap"

    @classmethod
    def parse(cls, val):
        match val:
            case str():
                return cls(name=val, kwargs={})
            case {"name": str() as name, **kwargs}:
                return cls(name=name, kwargs=kwargs)
            case _:
                raise AnsibleError("Invalid snap value")

    def module_args(self, installed):
        args = {"name": self.name}
        if installed:
            return args | {"state": "present", **self.kwargs}
        return args | {"state": "absent"}


@dataclass
class PipInstall:
    name: str
    kwargs: dict

    @property
    def module_name(self):
        return "ansible.builtin.pip"

    @classmethod
    def parse(cls, val):
        def validate(pkg_name: str):
            if set(pkg_name) & set("<=>"):
                raise AnsibleError(f"Version specifiers not allowed: {pkg_name}")

        match val:
            case str():
                validate(val)
                return cls(name=val, kwargs={})
            case {"name": str() as name, **kwargs}:
                validate(name)
                return cls(name=name, kwargs=kwargs)
            case _:
                raise AnsibleError("Invalid pip value")

    def module_args(self, installed):
        args = {"name": self.name}
        if installed:
            return {**args, "state": "present", **self.kwargs}
        return {**args, "state": "absent"}


@dataclass
class PipxInstall:
    name: str
    kwargs: dict

    @property
    def module_name(self):
        return "community.general.pipx"

    @classmethod
    def parse(cls, val):
        def validate(pkg_name: str):
            if set(pkg_name) & set("<=>"):
                raise AnsibleError(f"Version specifiers not allowed: {pkg_name}")

        match val:
            case str():
                validate(val)
                return cls(name=val, kwargs={})
            case {"name": str() as name, **kwargs}:
                validate(name)
                return cls(name=name, kwargs=kwargs)
            case _:
                raise AnsibleError("Invalid pipx value")

    def module_args(self, installed):
        args = {"name": self.name}
        if installed:
            return {**args, "state": "present", **self.kwargs}
        return {**args, "state": "absent"}


@dataclass
class FlatpakInstall:
    """Handle installing applications with Flatpak via community.general.flatpak.

    The syntax accepted mirrors the *snap* handler in this module:

        flatpak: app-id-or-ref

    or::

        flatpak:
          name: app-id-or-ref
          remote: flathub
          method: user

    Additional keyword arguments are passed through to the
    *community.general.flatpak* module.
    """

    name: str
    kwargs: dict

    @property
    def module_name(self):
        return "community.general.flatpak"

    @classmethod
    def parse(cls, val):
        """Parse the YAML value given for the *flatpak* key.

        Accept a string (treated as the application id/reference) or a mapping with at
        least a *name* key.  Mapping variant allows the caller to specify additional
        arguments such as *remote* or *method*.
        """

        match val:
            case str():
                return cls(name=val, kwargs={})

            case {"name": str() as name, **kwargs}:
                return cls(name=name, kwargs=kwargs)

            case _:
                raise AnsibleError("Invalid flatpak value")

    def module_args(self, installed):
        """Return the argument dict for *community.general.flatpak*."""

        args = {"name": self.name}

        if installed:
            # Install or ensure present.
            return args | {"state": "present", **self.kwargs}
        # Ensure absent - additional kwargs are typically not needed when
        # uninstalling but they *are* accepted by the underlying module
        # (they will just be ignored).  Keep the behaviour consistent with
        # the *snap* handler and drop them for clarity.
        return args | {"state": "absent"}


METHODS = {"apt": AptInstall, "snap": SnapInstall, "pip": PipInstall, "pipx": PipxInstall, "flatpak": FlatpakInstall}


class ActionModule(ActionBase):
    def run(self, tmp=None, task_vars=None):
        args = self._task.args
        debug = args.get("debug", False)
        if "use" not in args:
            raise AnsibleError("Missing required argument: use")
        use = args["use"]

        changed = False
        result = {}

        # parse all defined install methods
        parsed = {}
        for arg, value in args.items():
            if arg == "use":
                continue
            if arg not in METHODS:
                raise AnsibleError(f"Unknown argument: {arg}. Expected: {list(METHODS.keys())}")

            assert arg not in parsed
            try:
                parsed[arg] = METHODS[arg].parse(value)
            except Exception as e:
                raise AnsibleError(f"Error parsing {arg}: {e}") from e
            if debug:
                result[f"{arg}_parsed"] = parsed[arg].__dict__

        if use not in parsed and use != "none":
            raise AnsibleError(f"Invalid use value: {use}. Expected one of {list(parsed.keys())} or 'none'")

        # run selected install method
        for method, impl in parsed.items():
            r = self._execute_module(
                module_name=impl.module_name, module_args=impl.module_args(method == use), task_vars=task_vars, tmp=tmp
            )
            changed |= r.get("changed", False)

        return {"changed": changed, **result}
