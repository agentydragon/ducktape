"""Settings for the Plaid MCP server (environment-driven).

Item metadata (non-secret) comes from `PLAID_MCP_ITEMS_META` as JSON; each item's
access token is read from the env var it names, so secrets stay in Secrets and
metadata in a ConfigMap.
"""

import os
from typing import Literal

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class PlaidItem(BaseModel):
    key: str = Field(description="Selector used in tool `item` args, e.g. 'chase'.")
    institution: str = Field(description="Human-readable institution name.")
    products: list[str] = Field(
        description="Plaid products enabled for the item, e.g. ['transactions', 'liabilities']."
    )
    access_token_env: str = Field(description="Name of the env var holding this item's Plaid access_token.")


class ResolvedItem(BaseModel):
    key: str
    institution: str
    products: list[str]
    access_token: str


class ItemSummary(BaseModel):
    """list_items output: a configured item without its access token."""

    key: str
    institution: str
    products: list[str]


class ServerSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PLAID_MCP_")

    plaid_env: Literal["sandbox", "production"] = Field(description="Plaid environment.")
    client_id: str = Field(description="Plaid client_id.")
    client_secret: str = Field(description="Plaid client secret.")
    items_meta: list[PlaidItem] = Field(description="Configured items as JSON in PLAID_MCP_ITEMS_META.")
    host: str = "0.0.0.0"
    port: int = 8080

    def resolved_items(self) -> dict[str, ResolvedItem]:
        """Resolve each item's access token from its env var (raises KeyError if unset)."""
        resolved: dict[str, ResolvedItem] = {}
        for item in self.items_meta:
            resolved[item.key] = ResolvedItem(
                key=item.key,
                institution=item.institution,
                products=item.products,
                access_token=os.environ[item.access_token_env],
            )
        return resolved
