import ast
import pkgutil
from pathlib import Path

from wt.testing.conftest import get_wt_package_dir

ROOT = get_wt_package_dir()

CLIENT_PREFIX = "wt.client"
SERVER_PREFIX = "wt.server"
SHARED_PREFIX = "wt.shared"

ALLOWED_PREFIXES_FOR_CLIENT = {CLIENT_PREFIX, SHARED_PREFIX}
ALLOWED_PREFIXES_FOR_SERVER = {SERVER_PREFIX, SHARED_PREFIX}


def iter_modules(package_prefix: str):
    pkg_path = ROOT / package_prefix.removeprefix("wt.").replace(".", "/")
    for m in pkgutil.walk_packages([str(pkg_path)], prefix=package_prefix + "."):
        if not m.ispkg:
            yield m.name


def _module_path(module_name: str) -> Path:
    if not module_name.startswith("wt."):
        raise ValueError(module_name)
    rel = module_name.removeprefix("wt.").replace(".", "/") + ".py"
    return ROOT / rel


def _resolve_from_import(module_name: str, node: ast.ImportFrom) -> str:
    base_module = node.module or ""
    if node.level and node.level > 0:
        pkg = module_name.rsplit(".", 1)[0]
        parts = pkg.split(".")
        pkg_base = ".".join(parts[: len(parts) - node.level]) if node.level <= len(parts) else "wt"
        return pkg_base + ("." + base_module if base_module else "")
    return base_module


def get_imports(module_name: str) -> set[str]:
    path = _module_path(module_name)
    source = path.read_text()

    imports: set[str] = set()
    tree = ast.parse(source, filename=str(path))

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name:
                    imports.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            target = _resolve_from_import(module_name, node)
            if target:
                imports.add(target)
    return imports


def test_client_does_not_import_server():
    violations = []
    for mod in iter_modules(CLIENT_PREFIX):
        imports = get_imports(mod)
        for imp in imports:
            if imp.startswith(SERVER_PREFIX):
                violations.append((mod, imp))
    assert not violations, f"Client imports server: {violations}"


def test_server_does_not_import_client():
    violations = []
    for mod in iter_modules(SERVER_PREFIX):
        imports = get_imports(mod)
        for imp in imports:
            if imp.startswith(CLIENT_PREFIX):
                violations.append((mod, imp))
    assert not violations, f"Server imports client: {violations}"
