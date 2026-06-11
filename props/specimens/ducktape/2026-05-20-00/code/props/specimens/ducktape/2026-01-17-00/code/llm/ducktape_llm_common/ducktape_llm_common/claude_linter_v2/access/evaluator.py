"""Predicate evaluation engine for access control."""

import ast
import logging
from collections.abc import Callable

from .context import PredicateContext

logger = logging.getLogger(__name__)


class PredicateEvaluator:
    """Evaluates Python predicate functions safely."""

    def evaluate(self, predicate: str, context: PredicateContext) -> bool:
        """
        Evaluate a predicate function.

        The predicate must be exactly one function definition that:
        1. Takes exactly one parameter (the context)
        2. Returns a boolean value
        3. Can import modules and use complex logic

        Example:
        def check_tool(ctx):
            return ctx.tool == 'Bash'
        """
        try:
            func = self._validate_and_extract_function(predicate)
            result = func(context)
            return bool(result)
        except Exception as e:
            logger.error(f"Failed to evaluate predicate function: {e}")
            # Include first line of predicate for context
            first_line = predicate.strip().split("\n")[0][:50]
            raise ValueError(f"Invalid predicate function starting with '{first_line}...': {e}") from e

    def _validate_and_extract_function(self, predicate_code: str) -> Callable:
        """Parse and validate that predicate is exactly one function."""
        try:
            tree = ast.parse(predicate_code.strip())
        except SyntaxError as e:
            raise ValueError(f"Invalid Python syntax: {e}") from e

        # Must contain exactly one function definition at top level
        functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)]
        if len(functions) != 1:
            raise ValueError(
                f"Predicate must contain exactly one function definition. Found {len(functions)} functions."
            )

        # Check for other top-level statements (imports are allowed)
        non_function_statements = [
            node for node in tree.body if not isinstance(node, ast.FunctionDef | ast.Import | ast.ImportFrom)
        ]
        if non_function_statements:
            raise ValueError(
                "Predicate can only contain one function definition and imports. No other statements allowed."
            )

        func_node = functions[0]

        # Function must take exactly one parameter
        if len(func_node.args.args) != 1:
            raise ValueError(
                f"Predicate function must take exactly one parameter (ctx). "
                f"Found {len(func_node.args.args)} parameters."
            )

        # Execute to get the function
        namespace = {"__import__": __import__}
        exec(predicate_code, namespace)

        # Return the function
        func_name = func_node.name
        if func_name not in namespace:
            raise ValueError(f"Function '{func_name}' not found after execution")

        func = namespace[func_name]
        if not callable(func):
            raise ValueError(f"'{func_name}' is not callable")

        return func
