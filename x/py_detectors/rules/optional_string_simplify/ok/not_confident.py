def f(x):
    # Unknown type; detector should be conservative
    if x is None or x == "":
        return True
    return False
