from __future__ import annotations

import copy
from typing import Any

import uvicorn
from uvicorn.config import LOGGING_CONFIG


def _log_config() -> dict[str, Any]:
    config: dict[str, Any] = copy.deepcopy(LOGGING_CONFIG)

    default_fmt = "%(asctime)s %(levelname)s [%(name)s] %(message)s"
    access_fmt = '%(asctime)s %(levelname)s [%(name)s] %(client_addr)s - "%(request_line)s" %(status_code)s'

    config["formatters"]["default"]["fmt"] = default_fmt
    config["formatters"]["access"]["fmt"] = access_fmt
    config.setdefault("loggers", {})
    config["loggers"][""] = {"handlers": ["default"], "level": "INFO", "propagate": True}
    config["loggers"]["uvicorn.error"] = {"level": "INFO"}
    config["loggers"]["uvicorn.access"] = {"handlers": ["access"], "level": "INFO", "propagate": False}
    config["loggers"]["uvicorn"] = {"handlers": ["default"], "level": "INFO", "propagate": False}
    config["loggers"]["ember"] = {"handlers": ["default"], "level": "INFO", "propagate": False}
    config["loggers"]["ember.matrix_client"] = {"handlers": ["default"], "level": "DEBUG", "propagate": False}
    return config


def main() -> None:
    uvicorn.run(
        "ember.app:create_app", factory=True, host="0.0.0.0", port=8000, log_level="info", log_config=_log_config()
    )


if __name__ == "__main__":
    main()
