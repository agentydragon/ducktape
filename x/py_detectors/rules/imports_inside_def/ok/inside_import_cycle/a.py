def f():
    from pkg.inside_import_cycle import b  # noqa: F401
