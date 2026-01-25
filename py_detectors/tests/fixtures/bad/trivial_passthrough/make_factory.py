# Trivial passthrough pattern - the function just calls a constructor and forwards args
# This is modeled after the removed make_compositor_meta_server function


class CompositorMetaServer:
    def __init__(self, *, compositor):
        self.compositor = compositor


def make_compositor_meta_server(*, compositor) -> CompositorMetaServer:
    """Create a CompositorMetaServer."""
    return CompositorMetaServer(compositor=compositor)


# Another example - simple factory with no transformation
class Foo:
    def __init__(self, bar, baz):
        self.bar = bar
        self.baz = baz


def make_foo(bar, baz):
    return Foo(bar, baz)
