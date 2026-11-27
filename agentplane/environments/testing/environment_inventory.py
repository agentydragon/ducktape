"""Client-side fold of one EnvironmentService `WatchEnvironments` stream into complete inventories."""

from agentplane.environments.v1 import environment_service_pb2

type EnvironmentKey = tuple[str, str]


def _key(descriptor: environment_service_pb2.EnvironmentDescriptor) -> EnvironmentKey:
    target = descriptor.target
    if not descriptor.HasField("target") or not target.namespace or not target.name or not target.uid:
        raise RuntimeError("EnvironmentService returned a descriptor without its complete target")
    return target.namespace, target.name


class EnvironmentInventory:
    """One stream's state: snapshot chunks until completion, then upserts and UID-checked removals.

    A reconnected stream starts over with a fresh snapshot, so it needs a new instance.
    """

    def __init__(self) -> None:
        self._pending: dict[EnvironmentKey, environment_service_pb2.EnvironmentDescriptor] | None = {}
        self._current: dict[EnvironmentKey, environment_service_pb2.EnvironmentDescriptor] = {}

    def apply(
        self, frame: environment_service_pb2.WatchEnvironmentsResponse
    ) -> list[environment_service_pb2.EnvironmentDescriptor] | None:
        """Return the complete inventory, sorted by namespace and name, when `frame` changes it."""
        match frame.WhichOneof("frame"):
            case "snapshot":
                if self._pending is None:
                    raise RuntimeError("unexpected Environment snapshot chunk")
                for descriptor in frame.snapshot.environments:
                    self._pending[_key(descriptor)] = descriptor
                return None
            case "snapshot_complete":
                if self._pending is None:
                    raise RuntimeError("unexpected Environment snapshot completion")
                self._current, self._pending = self._pending, None
            case "upsert":
                if self._pending is not None:
                    raise RuntimeError("Environment update arrived before snapshot completion")
                self._current[_key(frame.upsert)] = frame.upsert
            case "removed":
                if self._pending is not None:
                    raise RuntimeError("Environment removal arrived before snapshot completion")
                key = (frame.removed.namespace, frame.removed.name)
                previous = self._current.get(key)
                # A removal for an older incarnation must not drop its same-named replacement.
                if previous is None or previous.target.uid != frame.removed.uid:
                    return None
                del self._current[key]
            case _:
                raise RuntimeError("EnvironmentService returned an empty watch frame")
        return [self._current[key] for key in sorted(self._current)]
