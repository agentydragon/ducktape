from agentplane.runner_gateway.dispatcher.api.session import session_dispatcher_pb2

# gazelle:include_dep @pypi//protobuf


assert (
    session_dispatcher_pb2.DESCRIPTOR.name
    == "agentplane/runner_gateway/dispatcher/api/session/session_dispatcher.proto"
)
