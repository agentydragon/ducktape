def unwrap_singleton(it):
    it = iter(it)
    x = next(it)
    try:
        next(it)
        raise ValueError("Expected a single item, got more.")
    except StopIteration:
        return x
