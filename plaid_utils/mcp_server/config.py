"""Settings for the Plaid MCP server.

Scalars (Plaid env, client creds, bind address) come from `PLAID_MCP_*` env vars. The item
registry comes from a YAML config file (`PLAID_MCP_ITEMS_CONFIG_PATH`, default
`/etc/plaid-mcp/items.yaml`) mounted from a ConfigMap — a plain YAML file, not an inline
JSON blob. Each item names the env var holding its access token, so secrets stay in Secrets
and the registry stays declarative config.
"""

import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class PlaidItem(BaseModel):
    key: str = Field(description="Selector used in tool `item` args, e.g. 'chase'.")
    institution: str = Field(description="Human-readable institution name.")
    products: list[str] = Field(
        description="Plaid products enabled for the item, e.g. ['transactions', 'liabilities']."
    )
    access_token_env: str = Field(description="Name of the env var holding this item's Plaid access_token.")


class ItemsConfig(BaseModel):
    """Top-level shape of the item-registry YAML file (`PLAID_MCP_ITEMS_CONFIG_PATH`)."""

    items: list[PlaidItem]


class ResolvedItem(BaseModel):
    key: str
    institution: str
    products: list[str]
    access_token: str


class ServerSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PLAID_MCP_")

    plaid_env: Literal["sandbox", "production"] = Field(description="Plaid environment.")
    client_id: str = Field(description="Plaid client_id.")
    client_secret: str = Field(description="Plaid client secret.")
    items_config_path: Path = Field(
        default=Path("/etc/plaid-mcp/items.yaml"), description="Path to the item-registry YAML file."
    )
    host: str = "0.0.0.0"
    port: int = 8080

    def resolved_items(self) -> dict[str, ResolvedItem]:
        """Load the item registry from the YAML config and resolve each access token from its env var."""
        config = ItemsConfig.model_validate(yaml.safe_load(self.items_config_path.read_text()))
        return {
            item.key: ResolvedItem(
                key=item.key,
                institution=item.institution,
                products=item.products,
                access_token=os.environ[item.access_token_env],
            )
            for item in config.items
        }
