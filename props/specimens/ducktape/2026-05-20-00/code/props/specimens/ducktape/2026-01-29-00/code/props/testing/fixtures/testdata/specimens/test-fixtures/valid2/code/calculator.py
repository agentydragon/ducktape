"""Calculator module for validation testing."""


def calculate(operation: str, a: float, b: float) -> float:
    """Perform calculation based on operation string."""
    if operation == "add":
        return a + b
    if operation == "subtract":
        return a - b
    if operation == "multiply":
        return a * b
    if operation == "divide":
        if b == 0:
            raise ValueError("Cannot divide by zero")
        return a / b
    raise ValueError(f"Unknown operation: {operation}")
