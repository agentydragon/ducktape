"""Synthetic bundle of n statements in the Lean `Stmt` format, split into contiguous blocks.

    python3 gen.py n modules mode [nat]   # mode: kernel | eval | native; `nat`: names are numbers, not strings

Shapes mimic a real bundle: mostly hoisted functions and arrow consts that reach back to a few
recent declarations, a few inert data consts, and a registration call every 6th statement.
With `nat` the names (and module names) are numbers, for the copy of the Lean sources whose `Name` is `Nat`
(`nat_names.sh`). The statement text stays a string.
"""

import random
import sys

n, k, mode = int(sys.argv[1]), int(sys.argv[2]), sys.argv[3]
nat = len(sys.argv) > 4 and sys.argv[4] == "nat"
rng = random.Random(n)
stmts, names = [], []  # names declared so far: (name, kind)
sels = []  # selector per statement
ids = {}


def lean_name(name):
    """A name as a Lean term."""
    return str(ids.setdefault(name, len(ids))) if nat else f'"{name}"'


def pick(m):
    pool = [nm for nm, _ in names[-12:]]
    return rng.sample(pool, min(m, len(pool))) if pool else []


def lean_names(xs):
    return "[" + ", ".join(lean_name(x) for x in xs) + "]"


stmts.append(f'{{ src := "function register(n, f) {{}}", defs := {lean_names(["register"])}, hoisted := true }}')
names.append(("register", "fn"))
sels.append(f".name {lean_name('register')}")
for i in range(1, n):
    kind = i % 6
    if kind == 5:  # registration call: effect, eager reads
        deps = pick(2)
        stmts.append(f'{{ src := "register({i});", eager := {lean_names(["register", *deps])}, effect := true }}')
        sels.append(f".stmt {i}")
    elif kind in (0, 2):  # hoisted function
        nm = f"f{i}"
        deps = pick(3)
        stmts.append(
            f'{{ src := "function {nm}() {{}}", defs := {lean_names([nm])}, lazy := {lean_names(deps)}, hoisted := true }}'
        )
        names.append((nm, "fn"))
        sels.append(f".name {lean_name(nm)}")
    elif kind in (1, 3):  # arrow const
        nm = f"c{i}"
        deps = pick(3)
        stmts.append(f'{{ src := "const {nm} = () => 0;", defs := {lean_names([nm])}, lazy := {lean_names(deps)} }}')
        names.append((nm, "const"))
        sels.append(f".name {lean_name(nm)}")
    else:  # inert data
        nm = f"v{i}"
        stmts.append(f'{{ src := "const {nm} = [];", defs := {lean_names([nm])} }}')
        names.append((nm, "const"))
        sels.append(f".name {lean_name(nm)}")

per = (n + k - 1) // k
mods = []
for m in range(k):
    block = sels[m * per : (m + 1) * per]
    if block:
        module = str(m) if nat else f'"m{m}.mjs"'
        mods.append(f"    ({module}, [{', '.join(block)}])")
last = str(len(mods) - 1) if nat else f'"m{len(mods) - 1}.mjs"'

print("import EsmSplit.Split\nimport EsmSplit.Correct\nopen EsmSplit\n")
print("def bundle : Bundle := [\n  " + ",\n  ".join(stmts) + "\n]\n")
print(f"def spec : Spec := {{ entry := {last}, modules := [\n{',\n'.join(mods)}\n] }}\n")
print("def plan : Plan := mkPlan bundle spec\n")
if mode == "kernel":
    print("theorem plan_valid : Valid bundle spec plan := by decide +kernel")
elif mode == "native":
    print("theorem plan_valid : Valid bundle spec plan := by native_decide")
else:
    print("#eval decide (Valid bundle spec plan)")
