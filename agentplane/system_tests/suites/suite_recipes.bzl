"""Explicit deployment choices for each common system-test suite."""

SUITE_RECIPES = {
    "pod": ["--runtime", "pod", "--backend", "local-model", "--mcp-edge", "omit", "--template", "pod"],
    "probe": ["--runtime", "pod", "--backend", "local-model", "--mcp-edge", "include", "--template", "pod"],
    "vm": ["--runtime", "kubevirt", "--backend", "local-model", "--mcp-edge", "omit", "--template", "kubevirt"],
    "firecracker": ["--runtime", "firecracker", "--backend", "local-model", "--mcp-edge", "omit", "--template", "firecracker"],
    "real_model": ["--runtime", "pod", "--backend", "public-litellm", "--mcp-edge", "include", "--template", "pod"],
}
