"""Differential test of the trusted parts (footprints, ES order model, renderer).

Random placements of the bundle's statements go through `render --spec ... --force`.
Soundness claim under test: if the checks pass, the split program prints what the bundle prints.
Rejected specs are rendered anyway to see how often rejection was necessary.

    python3 fuzz/fuzz.py [count] [seed]
"""

import collections
import json
import os
import pathlib
import random
import re
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
RENDER = pathlib.Path(os.environ.get("RENDER", ROOT / "lean/.lake/build/bin/render"))
# Selector for each statement of example/input/bundle.mjs, by position.
SEL = ["PI", "square", "area", "isEven", "isOdd", "registry", "register", "#7", "label", "#9", "#10", "#11"]


def run_node(directory, entry):
    p = subprocess.run(["node", entry], check=False, cwd=directory, capture_output=True, text=True, timeout=30)
    return (p.returncode, p.stdout)


def gen(rng):
    k = rng.randint(2, 6)
    mode = rng.choice(["uniform", "blocks", "perturb"])
    if mode == "uniform":
        assign = [rng.randrange(k) for _ in SEL]
    elif mode == "blocks":
        cuts = sorted(rng.sample(range(1, len(SEL)), k - 1))
        order = rng.sample(range(k), k)
        assign, block = [], 0
        for i in range(len(SEL)):
            while block < len(cuts) and i >= cuts[block]:
                block += 1
            assign.append(order[block])
        for _ in range(rng.randint(0, 3)):
            assign[rng.randrange(len(SEL))] = rng.randrange(k)
    else:  # perturb the shipped spec
        good = json.loads((ROOT / "example" / "spec.json").read_text())["modules"]
        names = list(good)
        owner = {s: names.index(m) for m, sels in good.items() for s in sels}
        assign = [owner[s] for s in SEL]
        k = len(names)
        for _ in range(rng.randint(1, 3)):
            assign[rng.randrange(len(SEL))] = rng.randrange(k)
    mods = collections.defaultdict(list)
    for sel, m in zip(SEL, assign, strict=True):
        mods[f"m{m}.mjs"].append(sel)
    entry = f"m{assign[-1]}.mjs" if rng.random() < 0.8 else rng.choice(list(mods))
    return {"entry": entry, "modules": dict(mods)}


def main():
    count = int(sys.argv[1]) if len(sys.argv) > 1 else 300
    rng = random.Random(int(sys.argv[2]) if len(sys.argv) > 2 else 0)
    expected = run_node(ROOT / "example" / "input", "bundle.mjs")
    tally = collections.Counter()
    seen, examples = set(), []
    for _ in range(count):
        spec = gen(rng)
        key = json.dumps(spec, sort_keys=True)
        if key in seen:
            continue
        seen.add(key)
        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = pathlib.Path(tmp_name)
            (tmp / "spec.json").write_text(json.dumps(spec))
            r = subprocess.run(
                [RENDER, str(tmp / "out"), "--spec", str(tmp / "spec.json"), "--force"],
                check=False,
                capture_output=True,
                text=True,
            )
            if r.returncode not in (0, 1):
                raise SystemExit(f"render failed ({r.returncode}): {r.stderr}\n{spec}")
            valid = r.returncode == 0
            try:
                got = run_node(tmp / "out", spec["entry"])
            except subprocess.TimeoutExpired:
                got = ("timeout", "")
            same = got == expected
            tally["valid" if valid else "rejected"] += 1
            if valid and not same:
                tally["UNSOUND"] += 1
                print("UNSOUND: accepted a split that behaves differently", json.dumps(spec), got, file=sys.stderr)
            elif valid:
                order = re.search(
                    r"\[[0-9, ]+\]", next(ln for ln in r.stdout.splitlines() if "statement order" in ln)
                ).group(0)
                examples.append((order != str(list(range(len(SEL)))), json.dumps(spec["modules"]), order))
            elif same:
                tally["rejected_but_harmless"] += 1
            else:
                tally["rejected_and_broken"] += 1
    print(dict(tally))
    reshuffled = [e for e in examples if e[0]]
    print(f"accepted: {len(examples)} (of which {len(reshuffled)} run statements in a different order than the bundle)")
    for _, mods, order in reshuffled[:3]:
        print("  e.g.", order, mods)
    if tally["UNSOUND"]:
        raise SystemExit(1)


main()
