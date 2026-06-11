"""Flake8 plugin to detect code that should use early bailout pattern."""

import ast
from collections.abc import Generator
from typing import Any


class EarlyBailoutChecker:
    """Checker for code that could use early bailout pattern."""

    name = "flake8-early-bailout"
    version = "0.2.0"

    def __init__(self, tree: ast.AST) -> None:
        self.tree = tree
        self.errors: list[tuple[int, int, str, type[Any]]] = []

    def run(self) -> Generator[tuple[int, int, str, type[Any]]]:
        """Run the checker and yield errors."""
        visitor = EarlyBailoutVisitor()
        visitor.visit(self.tree)
        self.errors = visitor.errors
        yield from self.errors


class EarlyBailoutVisitor(ast.NodeVisitor):
    """AST visitor to detect early bailout opportunities."""

    # NOTE: Method names intentionally use CamelCase to match ast.NodeVisitor
    # dispatch (visit_<NodeClass>) so the visitor methods are invoked correctly.
    # These method names violate normal naming conventions, so we suppress
    # the N802 linter rule on the individual method definitions below.

    def __init__(self) -> None:
        self.errors: list[tuple[int, int, str, type[Any]]] = []
        self.current_function: ast.FunctionDef | ast.AsyncFunctionDef | None = None
        self.loop_depth = 0

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802, RUF100
        """Track current function context."""
        old_function = self.current_function
        self.current_function = node
        self.generic_visit(node)
        self.current_function = old_function

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:  # noqa: N802, RUF100
        """Track current async function context."""
        old_function = self.current_function
        self.current_function = node
        self.generic_visit(node)
        self.current_function = old_function

    def visit_For(self, node: ast.For) -> None:  # noqa: N802, RUF100
        """Track loop depth."""
        self.loop_depth += 1
        self.generic_visit(node)
        self.loop_depth -= 1

    def visit_While(self, node: ast.While) -> None:  # noqa: N802, RUF100
        """Track loop depth."""
        self.loop_depth += 1
        self.generic_visit(node)
        self.loop_depth -= 1

    def visit_If(self, node: ast.If) -> None:  # noqa: N802, RUF100
        """Check if statements for early bailout opportunities."""
        # Check if this if has an else clause
        if node.orelse:
            self._check_inverted_pattern(node)

        # Check for any if that contains another if (even without else)
        self._check_nested_if_pattern(node)

        self.generic_visit(node)

    def _check_inverted_pattern(self, node: ast.If) -> None:
        """Check if the if/else could be inverted for early bailout."""
        if_size = self._estimate_block_size(node.body)
        else_size = self._estimate_block_size(node.orelse)

        # If the else block is significantly smaller and we can bail out
        if else_size < if_size and else_size <= 3 and if_size >= 5 and self._can_use_early_exit(node.orelse):
            bailout_type = self._get_bailout_type()
            self.errors.append(
                (
                    node.lineno,
                    node.col_offset,
                    f"EB100 Consider early bailout pattern - invert condition and {bailout_type} early. "
                    f"Main path ({if_size} lines) is in 'if', short path ({else_size} lines) is in 'else'.",
                    type(self),
                )
            )

    def _check_nested_if_pattern(self, node: ast.If) -> None:
        """Check for any if that contains another if - creates rightward drift."""
        # Look for if statements in the body
        has_nested_if = any(isinstance(stmt, ast.If) for stmt in node.body)

        # If we're in a context where we can bail out early, flag nested ifs
        if has_nested_if and (self.current_function is not None or self.loop_depth > 0):
            bailout_type = self._get_bailout_type()
            self.errors.append(
                (
                    node.lineno,
                    node.col_offset,
                    f"EB101 Nested if statements increase indentation - consider {bailout_type} early to flatten the code.",
                    type(self),
                )
            )

    def _estimate_block_size(self, block: list[ast.stmt]) -> int:
        """Estimate the size of a code block in lines."""
        if not block:
            return 0

        # For simple statements, count them
        count = 0
        for stmt in block:
            if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant):
                # Single constants/strings are usually 1 line
                count += 1
            elif isinstance(stmt, ast.Return | ast.Pass | ast.Break | ast.Continue | ast.Raise | ast.Assign):
                count += 1
            elif isinstance(stmt, ast.If | ast.For | ast.While | ast.With | ast.Try):
                # Complex statements are at least 3 lines
                count += 3 + self._estimate_block_size(getattr(stmt, "body", []))
            else:
                # Default: assume 2 lines for other statements
                count += 2

        return count

    def _can_use_early_exit(self, block: list[ast.stmt]) -> bool:
        """Check if a block ends with or could use an early exit."""
        if not block:
            return False

        # Check if the block already ends with an exit
        last_stmt = block[-1]
        if isinstance(last_stmt, ast.Return | ast.Raise | ast.Break | ast.Continue):
            return True

        # In functions, we can always add a return
        if self.current_function is not None:
            return True

        # In loops, we can use continue or break
        return self.loop_depth > 0

    def _get_bailout_type(self) -> str:
        """Get the appropriate bailout mechanism for current context."""
        if self.current_function is not None and self.loop_depth == 0:
            return "return"
        if self.loop_depth > 0:
            return "continue or break"
        return "restructure logic"
