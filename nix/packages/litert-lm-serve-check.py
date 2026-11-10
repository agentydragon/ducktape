"""Check the packaged serve option through its real configuration resolver."""

import contextlib
import dataclasses
import http.server
from unittest.mock import patch

import litert_lm
from click.testing import CliRunner
from litert_lm_cli import config, model
from litert_lm_cli.commands.serve import module, util


@dataclasses.dataclass
class FixtureModel:
    model_id: str = "fixture"
    model_path: str = "/unused-model"

    def exists(self) -> bool:
        return True


def check_mode(args: list[str], expected: litert_lm.SpeculativeDecodingMode) -> None:
    observed = []

    class FixtureEngine:
        def __init__(self, model_path, **kwargs):
            assert model_path == "/unused-model"
            observed.append(kwargs["speculative_decoding_config"].mode)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return None

    def serve_once(server):
        # The real server and resolver run; only transport, file lookup and
        # the native inference engine are replaced. The configured adaptive
        # mode must lose to the explicit/default CLI on/off setting.
        util.get_or_initialize_server_engine(server, model_id="fixture")

    with contextlib.ExitStack() as stack:
        stack.enter_context(patch.object(http.server.HTTPServer, "__init__", new=lambda *a, **kw: None))
        stack.enter_context(patch.object(http.server.HTTPServer, "server_close", new=lambda *a: None))
        stack.enter_context(patch.object(util.LiteRTLMServer, "serve_forever", new=serve_once))
        stack.enter_context(
            patch.object(model.Model, "from_model_reference", new=classmethod(lambda cls, ref: FixtureModel()))
        )
        stack.enter_context(patch.object(model, "parse_backend", new=lambda *a, **kw: None))
        stack.enter_context(
            patch.object(config, "get_model_config", new=lambda _: config.ModelConfig(speculative_decoding="adaptive"))
        )
        stack.enter_context(patch.object(litert_lm, "Engine", new=FixtureEngine))
        result = CliRunner().invoke(module.serve, args)
    assert result.exit_code == 0, (args, result.output, result.exception)
    assert observed == [expected], (args, observed, expected)


def main() -> None:
    for args, expected in [
        ([], litert_lm.SpeculativeDecodingMode.OFF),
        (["--enable-speculative-decoding=true"], litert_lm.SpeculativeDecodingMode.ON),
        (["--enable-speculative-decoding=false"], litert_lm.SpeculativeDecodingMode.OFF),
        (["--enable-speculative-decoding=TRUE"], litert_lm.SpeculativeDecodingMode.ON),
    ]:
        check_mode(args, expected)
    print("serve speculative-decoding: default, true, false and uppercase passed")


if __name__ == "__main__":
    main()
