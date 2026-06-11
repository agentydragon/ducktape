---
title: Static typing in Python
---

This summer, I am interning at Dropbox in San Francisco. So far, the most
awesome Dropbox event I attended was [the 2015 Hack Week](https://blogs.dropbox.com/dropbox/2015/08/hack-week-2015/),
which consists of everyone basically dropping their usual job and doing whatever
they want. So yeah, it's pretty awesome.

I went totally out of my element and joined a bunch of excellent engineers
working to make static typing work in Python 2.

Python 3 lets you optionally annotate your functions with argument and return
types. This was defined in [PEP 484](https://www.python.org/dev/peps/pep-0484/).
The type system is gradual (i.e., you can gradually convert an untyped program
to a typed one, module by module) and fairly strong.
Aside from co-authoring the PEP with Dropbox superengineer-in-chief
Guido van Rossum, our team leader [Jukka Lehtosalo](http://www.cl.cam.ac.uk/~jal82/)
developed the first actual working type checker for Python:
[MyPy](http://mypy-lang.org/).

Most of Dropbox is written in Python 2, through a switch to Python 3 is
something everyone wants (and some other Hack Week projects made some very
impressive steps in that direction). First, we had to figure out how
to annotate existing code with PEP 484-like annotations. We ended up with
something like this:

<figure>
```python
from typing import Any, List, NamedTuple

Account = NamedTuple('Account', [('name', str), ('balance', int),
     ('notes', List[Any]])

def apply_promotion(amount, accounts, *notes):
    # type: (int, Iterable[Account],*Any) -> bool
    # Previous line declares function argument types and return type.

    for account in accounts:
        account.balance += amount
 account.notes.extend(notes)

    return len(accounts) > 0

```

<div>Python 2 with type annotations (please excuse the contrived example)</div>
</figure>

Then we hacked on MyPy until it gave us a clean compile on a small piece of
Dropbox's codebase. This involved hunting for bugs in MyPy's understanding
of Python 2, annotating existing Dropbox code, and (which I was doing most
of the time) teaching MyPy about non-Dropbox modules.

PEP 484 lets you separate your actual code from its type annotations.
The type annotations may be stored separately in a `.pyi` file (the `i` probably
stands for "interface"). A `.pyi` file for the above example might look like
this:

<figure>
```python
from typing import Any, List, NamedTuple

Account = NamedTuple('Account', [('name', str), ('balance', int),
     ('notes', List[Any])])

def apply_promotion(amount: int, accounts: Iterable[Account],
                    *notes: Any) -> bool: ...
```

<div>The `.pyi` for the example above. Note the `...` replacing the method body.</div>
</figure>

To let MyPy know about the type information known to be true about non-annotated
libraries, the MyPy repo (`https://github.com/JukkaL/mypy`) contains a directory
called `stubs/`, which contains `.pyi` files corresponding to various modules.
We had to add many builtin Python 2 modules, and also several external
libraries (our selected piece of Dropbox code to annotate depended on those).

Overall, the project was a great success: we got a mostly clean typecheck
on a chunk of Dropbox's codebase interacting with several external libraries.
We found, and the more knowledgeable engineers also fixed, several issues
with the Python 2 parser of MyPy and added a few other features we found useful
(e.g., attempting to parse types from existing docstrings). We generated and/or
wrote stubs for quite a bunch of libraries, which should make it much easier to
use MyPy for Python 2 in the future.

[Check out MyPy](http://mypy-lang.org), it's awesome!
