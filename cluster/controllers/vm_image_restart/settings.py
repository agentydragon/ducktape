"""Environment-backed settings for the VM image restart controller."""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """The one target VM and ConfigMap this controller is allowed to manage."""

    model_config = SettingsConfigDict(case_sensitive=False, extra="ignore", frozen=True, populate_by_name=True)

    target_namespace: str = Field(min_length=1)
    target_vm_name: str = Field(min_length=1)
    state_configmap_name: str = Field(min_length=1)
