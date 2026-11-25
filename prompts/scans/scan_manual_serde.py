#!/usr/bin/env python3
"""Scan Python codebase for manual dict construction and Pydantic model design issues.

This script performs two main analyses:
1. Finds dict literals with string-literal keys (candidates for Pydantic __init__)
2. Analyzes BaseModel classes for design issues (overlapping fields, single-field models)

Usage (from the repository root):
    bb run //prompts/scans:scan_manual_serde_bin -- [directory]

Output:
    JSON with dict_literals and pydantic_models sections
"""

import ast
import sys
from pathlib import Path
from typing import Any

from prompts.scans.scan_runner import iter_python_files, run_cli


class DictLiteralFinder(ast.NodeVisitor):
    """Find dict literals with string-literal keys."""

    def __init__(self):
        self.dict_literals: list[dict[str, Any]] = []
        self.current_function: str | None = None
        self.current_class: str | None = None

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        old_func = self.current_function
        self.current_function = node.name
        self.generic_visit(node)
        self.current_function = old_func

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        old_func = self.current_function
        self.current_function = node.name
        self.generic_visit(node)
        self.current_function = old_func

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        old_class = self.current_class
        self.current_class = node.name
        self.generic_visit(node)
        self.current_class = old_class

    def visit_Dict(self, node: ast.Dict) -> None:
        # Check if any keys are string literals
        string_keys = []
        for key in node.keys:
            if key is None:  # **dict unpacking
                continue
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                string_keys.append(key.value)

        if string_keys:
            context = []
            if self.current_class:
                context.append(f"class {self.current_class}")
            if self.current_function:
                context.append(f"function {self.current_function}")

            self.dict_literals.append(
                {
                    "line": node.lineno,
                    "col": node.col_offset,
                    "keys": string_keys,
                    "context": " > ".join(context) if context else "module-level",
                }
            )

        self.generic_visit(node)


class PydanticModelAnalyzer(ast.NodeVisitor):
    """Analyze Pydantic BaseModel classes."""

    def __init__(self):
        self.models: list[dict[str, Any]] = []

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        # Check if inherits from BaseModel
        inherits_basemodel = False
        for base in node.bases:
            if isinstance(base, ast.Name) and base.id == "BaseModel":
                inherits_basemodel = True
                break
            # Check for qualified names like pydantic.BaseModel
            if isinstance(base, ast.Attribute) and base.attr == "BaseModel":
                inherits_basemodel = True
                break

        if inherits_basemodel:
            fields = self._extract_fields(node)
            self.models.append({"line": node.lineno, "name": node.name, "fields": fields})

        self.generic_visit(node)

    def _extract_fields(self, class_node: ast.ClassDef) -> dict[str, str]:
        """Extract field names and type annotations from class body."""
        fields = {}
        for stmt in class_node.body:
            if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                field_name = stmt.target.id
                # Get type annotation as string
                type_str = ast.unparse(stmt.annotation) if stmt.annotation else "Any"
                fields[field_name] = type_str
        return fields


def scan_file(filepath: Path) -> tuple[list[dict], list[dict]]:
    """Scan a single Python file for dict literals and Pydantic models."""
    try:
        source = filepath.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(filepath))

        dict_finder = DictLiteralFinder()
        dict_finder.visit(tree)

        model_analyzer = PydanticModelAnalyzer()
        model_analyzer.visit(tree)

        return dict_finder.dict_literals, model_analyzer.models
    except SyntaxError, UnicodeDecodeError:
        # Skip files with syntax errors or encoding issues
        return [], []


def scan_directory(root: Path) -> dict[str, Any]:
    """Scan all Python files in directory tree."""
    dict_literals_by_file: dict[str, list[dict]] = {}
    models_by_file: dict[str, list[dict]] = {}
    total_dict_literals = 0
    total_models = 0

    for py_file in iter_python_files(root):
        dict_literals, models = scan_file(py_file)

        if dict_literals:
            dict_literals_by_file[str(py_file)] = dict_literals
            total_dict_literals += len(dict_literals)

        if models:
            models_by_file[str(py_file)] = models
            total_models += len(models)

    return {
        "summary": {"total_dict_literals": total_dict_literals, "total_models": total_models},
        "dict_literals": dict_literals_by_file,
        "pydantic_models": models_by_file,
    }


def write_summary(results: dict[str, Any]) -> None:
    print(f"Dict literals with string keys: {results['summary']['total_dict_literals']}", file=sys.stderr)
    print(f"Pydantic models found: {results['summary']['total_models']}", file=sys.stderr)


def main() -> None:
    run_cli(scan_directory, write_summary)


if __name__ == "__main__":
    main()
