"""Identity of the cluster's TabbyAPI service and loaded checkpoint."""

NAME = "tabbyapi"
NAMESPACE = "ollama"
PORT = 5000
MODEL_NAME = "qwen38-exl3-268"
SWITCHING_SERVICE = "tabbyapi-model-switching"
SWITCHING_API_BASE = f"http://{SWITCHING_SERVICE}.{NAMESPACE}.svc:{PORT}/v1"
API_BASE = f"http://{NAME}.{NAMESPACE}.svc:{PORT}/v1"
