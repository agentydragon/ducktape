#!/usr/bin/env python3
"""Division operations for test fixture."""


def divide(a: float, b: float) -> float:
    """Divide a by b."""
    if b == 0:
        raise ValueError("Cannot divide by zero")
    return a / b


def safe_divide(a: float, b: float, default: float = 0.0) -> float:
    """Safely divide a by b, returning default if b is zero."""
    try:
        return divide(a, b)
    except ValueError:
        return default


if __name__ == "__main__":
    print("Division examples:")
    print(f"10 / 2 = {divide(10, 2)}")
    print(f"10 / 0 (safe) = {safe_divide(10, 0, default=-1)}")
