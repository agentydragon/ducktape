def f():
    x: str | None = ""
    if x is None or x == "":
        return True
    return False
