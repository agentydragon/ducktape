"""Generates agentplane-testing's `agentplane-app-config` ConfigMap's `config.yaml`
content -- agentplane/app/main.py's `Settings`, mounted by the Deployment. See
model_catalog/catalog.py for the model-name scheme.
"""

from __future__ import annotations

from agentplane.app.action_federation import ActionFederationSettings
from agentplane.app.main import AppSettingsConfig
from cluster.cdk8s.agentplane.app_settings import settings
from cluster.cdk8s.model_selections import TESTING_APP_MODELS
from model_catalog.catalog import GPT6_LUNA_RESPONSES

_NAMESPACE = "agentplane-testing"


def config(
    action_federation: ActionFederationSettings | None = None,
    sandbox_service_grpc_channel_options: dict[str, int | str] | None = None,
) -> AppSettingsConfig:
    return settings(
        namespace=_NAMESPACE,
        models=TESTING_APP_MODELS,
        thread_preset_codex_model=GPT6_LUNA_RESPONSES,
        action_federation=action_federation,
        sandbox_service_grpc_channel_options=sandbox_service_grpc_channel_options,
    )
