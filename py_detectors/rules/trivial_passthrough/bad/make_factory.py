# Trivial passthrough - forwards all args to a constructor


class Thing:
    def __init__(self, *, name):
        self.name = name


def make_thing(*, name) -> Thing:
    """Create a Thing."""
    return Thing(name=name)


class Widget:
    def __init__(self, x, y):
        self.x = x
        self.y = y


def make_widget(x, y):
    return Widget(x, y)
