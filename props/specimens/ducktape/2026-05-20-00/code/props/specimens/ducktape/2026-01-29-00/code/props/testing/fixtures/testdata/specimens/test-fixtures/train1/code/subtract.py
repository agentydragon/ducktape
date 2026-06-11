#!/usr/bin/env python3
"""A trivial script that subtracts two numbers."""


def subtract(a: float, b: float) -> float:
    """Subtract b from a."""
    return a - b


def main() -> None:
    """Main entry point."""
    print("Enter two numbers to subtract:")
    try:
        num1 = float(input("First number: "))
        num2 = float(input("Second number: "))
        result = subtract(num1, num2)
        print(f"{num1} - {num2} = {result}")
    except ValueError:
        print("Error: Please enter valid numbers")
        return


if __name__ == "__main__":
    main()
