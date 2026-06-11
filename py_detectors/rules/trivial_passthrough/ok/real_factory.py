# These are NOT trivial passthroughs - they add real value


class Server:
    def __init__(self, host, port, config):
        self.host = host
        self.port = port
        self.config = config


def make_server_with_defaults(host: str):
    """Factory that adds default configuration - not a trivial passthrough."""
    return Server(host=host, port=8080, config={"debug": True})


def create_server_from_env():
    """Factory that reads from environment - not a trivial passthrough."""
    import os

    return Server(host=os.getenv("HOST", "localhost"), port=int(os.getenv("PORT", "8080")), config={})


def transform_and_create(data: dict):
    """Factory that transforms input - not a trivial passthrough."""
    processed = {k.upper(): v for k, v in data.items()}
    return Server(host="localhost", port=8080, config=processed)
