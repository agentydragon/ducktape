"""The fixture's local-model catalog, shared by rendering and native test callers."""

# gazelle:include_dep @pypi//protobuf
from agentplane.proto import session_pb2
from model_catalog.catalog import GPT6_LUNA_RESPONSES, OLLAMA_QWEN_IQ4XS_128K, Route

LOCAL_MODEL_MODELS: dict[session_pb2.Harness, Route] = {
    session_pb2.HARNESS_CLAUDE: OLLAMA_QWEN_IQ4XS_128K.native,
    session_pb2.HARNESS_CODEX: GPT6_LUNA_RESPONSES,
}
