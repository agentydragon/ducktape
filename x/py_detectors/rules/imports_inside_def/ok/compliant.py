import math


def ok(n: int) -> float:
    # Legit top-level import; no in-function imports
    return math.sqrt(abs(n))
