import dataclasses
import getpass

import platformdirs
import yaml
from inventree.api import InvenTreeAPI

CONFIG_DIR = platformdirs.user_config_path("agentydragon_inventree_utils", ensure_exists=True)


@dataclasses.dataclass
class InstanceConfig:
    server_url: str
    username: str
    password: str
    # TODO: ... or token


def prompt_for_config() -> InstanceConfig:
    server_url = input("InvenTree instance (e.g. https://inventree.mycompany.com): ").strip()
    if not server_url:
        raise ValueError("No server URL provided.")

    username = input("Username: ").strip()
    if not username:
        raise ValueError("No username provided.")

    password = getpass.getpass("Password (input hidden): ")
    if not password:
        raise ValueError("No password provided.")

    # Write to file
    return InstanceConfig(server_url=server_url, username=username, password=password)


def load_or_prompt_for_config() -> InstanceConfig:
    """
    Try to load config. If it doesn't exist, ask
    user for server_url, username, password, then write it.
    """
    instance_file = CONFIG_DIR / "instance.yaml"

    if instance_file.exists():
        with instance_file.open() as f:
            return InstanceConfig(**yaml.safe_load(f))

    print(f"{instance_file} not found. Let's create it.\n")
    # Write to file
    cfg = prompt_for_config()
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with instance_file.open("w") as f:
        yaml.safe_dump(dataclasses.asdict(cfg), f)

    print("\nConfiguration saved.\n")
    return cfg


def api_from_config(config: InstanceConfig | None = None, **kwargs):
    if config is None:
        config = load_or_prompt_for_config()
    return InvenTreeAPI(config.server_url, username=config.username, password=config.password, **kwargs)
