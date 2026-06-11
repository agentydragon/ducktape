"""Scan Python files for trivial aliases, renamed imports, and thin wrappers."""

from __future__ import annotations

import argparse
import ast
import sys
import tomllib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from fnmatch import fnmatch
from functools import lru_cache
from pathlib import Path


@dataclass
class AliasAssign:
    alias: str
    param: str
    line: int
    col: int


@dataclass
class FunctionContext:
    params: set[str]
    param_kinds: dict[str, str]
    positional_order: list[str]
    keyword_only: set[str]
    vararg: str | None
    kwarg: str | None
    alias_assigns: dict[str, AliasAssign] = field(default_factory=dict)
    invalid_aliases: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class Config:
    project_root: Path
    skip_globs: tuple[str, ...]


class FileAnalyzer(ast.NodeVisitor):
    def __init__(self, path: Path) -> None:
        self.path = path
        self.findings: list[str] = []
        self.stack: list[FunctionContext] = []

    # -- Import alias detection -------------------------------------------------
    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            if alias.asname:
                self.findings.append(
                    f"{self.path}:{node.lineno}:{node.col_offset} RENAMED_IMPORT {alias.name} as {alias.asname} review alias necessity"
                )
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or "<unknown>"
        for alias in node.names:
            if alias.asname:
                self.findings.append(
                    f"{self.path}:{node.lineno}:{node.col_offset} RENAMED_IMPORT from {module} import {alias.name} as {alias.asname} review alias necessity"
                )
        self.generic_visit(node)

    # -- Function analysis ------------------------------------------------------
    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    # -- Assignment/store tracking ---------------------------------------------
    def visit_Assign(self, node: ast.Assign) -> None:
        ctx = self._current()
        if ctx is not None:
            for target in node.targets:
                self._record_store_target(target, node.lineno)
            if len(node.targets) == 1:
                target = node.targets[0]
                value = node.value
                if isinstance(target, ast.Name) and isinstance(value, ast.Name):
                    alias = target.id
                    param = value.id
                    if param in ctx.params and alias != param and alias not in ctx.alias_assigns:
                        ctx.alias_assigns[alias] = AliasAssign(
                            alias=alias, param=param, line=node.lineno, col=node.col_offset
                        )
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        ctx = self._current()
        if ctx is not None:
            self._record_store_target(node.target, node.lineno)
        self.generic_visit(node)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        ctx = self._current()
        if ctx is not None:
            self._record_store_target(node.target, node.lineno)
        self.generic_visit(node)

    def visit_For(self, node: ast.For) -> None:
        ctx = self._current()
        if ctx is not None:
            self._record_store_target(node.target, node.lineno)
        self.generic_visit(node)

    def visit_AsyncFor(self, node: ast.AsyncFor) -> None:
        ctx = self._current()
        if ctx is not None:
            self._record_store_target(node.target, node.lineno)
        self.generic_visit(node)

    def visit_With(self, node: ast.With) -> None:
        ctx = self._current()
        if ctx is not None:
            for item in node.items:
                if item.optional_vars is not None:
                    self._record_store_target(item.optional_vars, node.lineno)
        self.generic_visit(node)

    def visit_AsyncWith(self, node: ast.AsyncWith) -> None:
        ctx = self._current()
        if ctx is not None:
            for item in node.items:
                if item.optional_vars is not None:
                    self._record_store_target(item.optional_vars, node.lineno)
        self.generic_visit(node)

    # -- Helpers ----------------------------------------------------------------
    def _current(self) -> FunctionContext | None:
        if self.stack:
            return self.stack[-1]
        return None

    def _visit_function(self, node: ast.AST) -> None:
        ctx = self._make_context(node)
        self.stack.append(ctx)
        self.generic_visit(node)
        self._finalize_function(node, ctx)
        self.stack.pop()

    def _make_context(self, node: ast.AST) -> FunctionContext:
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            raise TypeError(f"expected function node, got {type(node).__name__}")

        args = node.args
        params: list[str] = []
        param_kinds: dict[str, str] = {}
        positional_order: list[str] = []
        keyword_only: set[str] = set()

        for arg in args.posonlyargs:
            params.append(arg.arg)
            param_kinds[arg.arg] = "posonly"
            positional_order.append(arg.arg)
        for arg in args.args:
            params.append(arg.arg)
            param_kinds[arg.arg] = "positional"
            positional_order.append(arg.arg)

        vararg = args.vararg.arg if args.vararg else None
        if vararg is not None:
            params.append(vararg)
            param_kinds[vararg] = "vararg"

        for arg in args.kwonlyargs:
            params.append(arg.arg)
            param_kinds[arg.arg] = "kwonly"
            keyword_only.add(arg.arg)

        kwarg = args.kwarg.arg if args.kwarg else None
        if kwarg is not None:
            params.append(kwarg)
            param_kinds[kwarg] = "kwarg"

        return FunctionContext(
            params=set(params),
            param_kinds=param_kinds,
            positional_order=positional_order,
            keyword_only=keyword_only,
            vararg=vararg,
            kwarg=kwarg,
        )

    def _record_store_target(self, target: ast.AST, line: int) -> None:
        ctx = self._current()
        if ctx is None:
            return

        for name in self._iter_store_names(target):
            record = ctx.alias_assigns.get(name)
            if record is not None and line > record.line:
                ctx.invalid_aliases.add(name)

    def _iter_store_names(self, target: ast.AST) -> list[str]:
        names: list[str] = []

        def _walk(node: ast.AST) -> None:
            if isinstance(node, ast.Name):
                names.append(node.id)
            elif isinstance(node, ast.Tuple | ast.List):
                for elt in node.elts:
                    _walk(elt)
            elif isinstance(node, ast.Starred):
                _walk(node.value)

        _walk(target)
        return names

    def _finalize_function(self, node: ast.AST, ctx: FunctionContext) -> None:
        self._report_trivial_aliases(ctx)
        self._report_trivial_wrapper(node, ctx)

    def _report_trivial_aliases(self, ctx: FunctionContext) -> None:
        for alias, record in ctx.alias_assigns.items():
            if alias in ctx.invalid_aliases:
                continue
            self.findings.append(
                f"{self.path}:{record.line}:{record.col} TRIVIAL_ALIAS {alias} -> {record.param} Trivial alias to fixture/param; use '{record.param}' directly."
            )

    def _report_trivial_wrapper(self, node: ast.AST, ctx: FunctionContext) -> None:
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            return

        body = list(node.body)
        if not body:
            return

        if (
            isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            body = body[1:]
        body = [stmt for stmt in body if not isinstance(stmt, ast.Pass)]
        if len(body) != 1:
            return

        stmt = body[0]
        if not isinstance(stmt, ast.Return):
            return
        if stmt.value is None or not isinstance(stmt.value, ast.Call):
            return

        call = stmt.value
        if not self._forwards_all_params(call, ctx):
            return

        call_name = self._call_name(call.func)
        if call_name is not None:
            simple = call_name.split(".")[-1]
            stripped = simple.lstrip("_")
            if stripped and stripped[0].isupper():
                return

        try:
            func_expr = ast.unparse(call.func)
        except (ValueError, RecursionError):  # pragma: no cover - malformed AST
            func_expr = call.func.id if isinstance(call.func, ast.Name) else "<call>"

        name = node.name if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) else "<lambda>"
        self.findings.append(
            f"{self.path}:{node.lineno}:{node.col_offset} TRIVIAL_FUNCTION {name} -> {func_expr} Thin wrapper around another call; inline or reuse callee."
        )

    def _forwards_all_params(self, call: ast.Call, ctx: FunctionContext) -> bool:
        forwarded: set[str] = set()
        positional_remaining = list(ctx.positional_order)

        for arg in call.args:
            if isinstance(arg, ast.Name):
                if not positional_remaining:
                    return False
                expected = positional_remaining.pop(0)
                if arg.id != expected:
                    return False
                forwarded.add(arg.id)
            elif isinstance(arg, ast.Starred) and isinstance(arg.value, ast.Name):
                name = arg.value.id
                if name != ctx.vararg:
                    return False
                forwarded.add(name)
            else:
                return False

        for kw in call.keywords:
            if kw.arg is None:
                if not isinstance(kw.value, ast.Name):
                    return False
                name = kw.value.id
                if name != ctx.kwarg:
                    return False
                forwarded.add(name)
                continue

            if not isinstance(kw.value, ast.Name):
                return False
            name = kw.value.id
            if kw.arg != name:
                return False
            if name not in ctx.params:
                return False
            forwarded.add(name)
            if name in positional_remaining:
                positional_remaining.remove(name)

        if positional_remaining:
            return False
        if ctx.keyword_only and not ctx.keyword_only.issubset(forwarded):
            return False
        if ctx.vararg and ctx.vararg not in forwarded:
            return False
        if ctx.kwarg and ctx.kwarg not in forwarded:
            return False

        return forwarded == ctx.params

    def _call_name(self, node: ast.AST) -> str | None:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            parent = self._call_name(node.value)
            if parent is None:
                return None
            return f"{parent}.{node.attr}"
        return None


def detect_file(path: Path) -> list[str]:
    """Analyze a Python file for trivial patterns.

    Raises on I/O or parse errors - caller must handle.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    analyzer = FileAnalyzer(path)
    analyzer.visit(tree)
    return analyzer.findings


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Detect renamed imports, fixture aliases, and trivial wrappers (report-only)"
    )
    parser.add_argument(
        "--scope",
        action="append",
        metavar="PATH",
        help=(
            "Limit findings to files under the given path spec. "
            "Repeat the flag or supply comma-separated specs. "
            "When omitted, the entire project is scanned."
        ),
    )
    parser.add_argument("paths", nargs="+", help="Files or directories to scan (e.g. tests/)")
    return parser


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = build_parser()
    return parser.parse_args(list(argv) if argv is not None else None)


def collect_files(raw_paths: Iterable[str]) -> list[Path]:
    files: list[Path] = []
    for raw in raw_paths:
        candidate = Path(raw)
        if candidate.is_dir():
            files.extend(candidate.rglob("*.py"))
        elif candidate.suffix == ".py":
            files.append(candidate)
    return files


def run(paths: Iterable[str], scope: tuple[str, ...], *, config: Config | None = None) -> int:
    cfg = config or load_config()
    files = collect_files(paths)
    results: list[str] = []
    errors: list[str] = []
    for file_path in files:
        if not _in_scope(file_path, scope, cfg.project_root):
            continue
        if _should_skip(file_path, cfg):
            continue
        try:
            results.extend(detect_file(file_path))
        except SyntaxError as e:
            errors.append(f"{file_path}: SyntaxError: {e.msg} (line {e.lineno})")
        except OSError as e:
            errors.append(f"{file_path}: {e}")

    for line in results:
        print(line)
    for err in errors:
        print(f"ERROR: {err}", file=sys.stderr)

    return 1 if results or errors else 0


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    scope_specs = _parse_scope(args.scope)
    return run(args.paths, scope_specs)


def _parse_scope(raw: list[str] | None) -> tuple[str, ...]:
    if not raw:
        return ()
    specs: list[str] = []
    for item in raw:
        for fragment in item.split(","):
            stripped_fragment = fragment.strip()
            if stripped_fragment:
                specs.append(stripped_fragment)
    if not specs:
        return ()
    return tuple(specs)


def _in_scope(path: Path, scope: tuple[str, ...], project_root: Path) -> bool:
    if not scope:
        return True
    rel_path = _relative_to_root(path, project_root)
    rel = rel_path.as_posix()
    for spec in scope:
        normalized = spec.strip()
        if not normalized:
            continue
        normalized = normalized.removeprefix("./")
        normalized = normalized.rstrip("/")
        if not normalized:
            continue
        if rel == normalized or rel.startswith(f"{normalized}/"):
            return True
        if fnmatch(rel, normalized):
            return True
    return False


def _relative_to_root(path: Path, project_root: Path) -> Path:
    resolved = path.resolve()
    try:
        return resolved.relative_to(project_root)
    except ValueError:
        return resolved


def _should_skip(path: Path, config: Config) -> bool:
    if not config.skip_globs:
        return False
    rel = _relative_to_root(path, config.project_root).as_posix()
    return any(fnmatch(rel, pattern) for pattern in config.skip_globs)


def _project_root(start: Path | None = None) -> Path:
    current = (start or Path.cwd()).resolve()
    for candidate in (current, *current.parents):
        if (candidate / "pyproject.toml").is_file():
            return candidate
    return current


@lru_cache(maxsize=1)
def load_config() -> Config:
    root = _project_root()
    skip: list[str] = []
    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        tool_cfg = data.get("tool") or {}
        adgn_cfg = tool_cfg.get("adgn") or {}
        trivial_cfg = adgn_cfg.get("trivial-patterns") or adgn_cfg.get("trivial_patterns") or {}
        raw_skip = trivial_cfg.get("skip-globs") or trivial_cfg.get("skip_globs")
        if isinstance(raw_skip, list):
            for item in raw_skip:
                text = str(item).strip()
                if text:
                    skip.append(text)
    # Preserve order while de-duplicating
    unique_skip: list[str] = []
    for pattern in skip:
        if pattern not in unique_skip:
            unique_skip.append(pattern)
    return Config(project_root=root, skip_globs=tuple(unique_skip))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
