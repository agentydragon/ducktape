"""Emit the compile-time Go service URI constants and membership checks."""

import json
from collections.abc import Iterable

from agentplane.net.peertls.service_uris import SERVICES, WORKLOAD_PEERS


def predicate(name: str, constants: Iterable[str]) -> str:
    cases = ", ".join(constants)
    return f"""func {name}(uri string) bool {{
    switch uri {{
    case {cases}:
        return true
    }}
    return false
}}"""


if __name__ == "__main__":
    print("// Code generated from service_uris.py. DO NOT EDIT.")
    print("package peertls\n\nconst (")
    for name, uri in SERVICES.items():
        print(f"\t{name} = {json.dumps(uri)}")
    print(")")
    print(predicate("IsServiceURI", SERVICES))
    print(predicate("isWorkloadServiceURI", WORKLOAD_PEERS))
