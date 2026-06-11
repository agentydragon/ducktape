"""Prompt generation for LLMs to access Gatelet."""

from gatelet.server.config import ChallengeResponseAuthSettings, KeyInUrlAuthSettings
from gatelet.server.models import AuthKey


def generate_access_prompt(
    base_url: str, config: KeyInUrlAuthSettings | ChallengeResponseAuthSettings, auth_key: AuthKey
) -> str:
    """Generate a prompt for LLMs on how to access Gatelet.

    Args:
        base_url: Base URL of the Gatelet service
        config: Authentication configuration object
        auth_key: Authentication key to include in the prompt

    Returns:
        Plaintext prompt for the LLM
    """
    if isinstance(config, KeyInUrlAuthSettings):
        return f"Go to: {base_url}/k/{auth_key.key_value}/"
    if isinstance(config, ChallengeResponseAuthSettings):
        return f"Go to: {base_url}/cr/{auth_key.id}"
    raise ValueError(f"Unsupported authentication configuration: {type(config)}")
