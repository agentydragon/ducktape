#!/usr/bin/python
"""Ansible module to create or update a single XDG autostart entry.

Writes a .desktop file in ~/.config/autostart and returns its path.
"""

from pathlib import Path

from ansible.module_utils.basic import AnsibleModule


def build_desktop_content(exec: str, name: str, icon: str | None) -> str:
    """Compose the content of the desktop entry."""
    lines: list[str] = [
        "[Desktop Entry]",
        "Type=Application",
        f"Name={name}",
        f"Exec={exec}",
        "X-GNOME-Autostart-enabled=true",
    ]

    if icon:
        lines.append(f"Icon={icon}")

    return "\n".join(lines) + "\n"  # Ensure final newline


def run_module() -> None:
    module = AnsibleModule(
        argument_spec={
            "exec": {"type": "str", "required": True},
            "name": {"type": "str", "required": True},
            "icon": {"type": "str"},
        },
        supports_check_mode=True,
    )

    params = module.params

    exec: str = params["exec"]
    name: str = params["name"]
    icon: str | None = params.get("icon")

    desktop_path = Path.home() / ".config" / "autostart" / f"{name.lower()}.desktop"

    desired_content = build_desktop_content(exec, name, icon)

    changed = not desktop_path.exists() or (desktop_path.read_text() != desired_content)

    # Perform write if needed and not in check mode
    if changed and not module.check_mode:
        desktop_path.parent.mkdir(parents=True, mode=0o755, exist_ok=True)
        desktop_path.write_text(desired_content)
        desktop_path.chmod(0o644)

    module.exit_json(changed=changed, desktop_file_path=str(desktop_path))


def main() -> None:
    run_module()


if __name__ == "__main__":
    main()
