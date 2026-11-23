"""LLM proxy entrypoint: `serve` starts the uvicorn server."""

from props.data_plane import create_serve_cli
from props.llm_proxy.app import create_app

cli = create_serve_cli(name="LLM proxy", create_app=create_app)


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
