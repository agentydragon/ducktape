"""Mutation test of the trusted logic: weaken it, rebuild `render` in a scratch copy, run the fuzzer.
A mutant that the fuzzer does not flag UNSOUND means the fuzzer (or the mutation) is too weak.

    python3 fuzz/mutate.py [count]
"""

import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
MUTANTS = {
    "drop effect-effect conflict": (
        "EsmSplit/Bundle.lean",
        "(a.effect && b.effect)\n    || (!b.hoisted",
        "false\n    || (!b.hoisted",
    ),
    "drop read-after-declare conflict": (
        "EsmSplit/Bundle.lean",
        "|| (!b.hoisted && b.defs.any",
        "|| (false && b.defs.any",
    ),
    "drop declare-after-read conflict": (
        "EsmSplit/Bundle.lean",
        "|| (!a.hoisted && a.defs.any",
        "|| (false && a.defs.any",
    ),
    "no transitive reads": (
        "EsmSplit/Bundle.lean",
        "closeFrom idx B.fuel s.eager ∅",
        "some (s.eager.foldl (·.insert ·) ∅)",
    ),
    "imports run in reverse order": (
        "EsmSplit/Plan.lean",
        "srcs.foldl (fun st s => P.visit fuel s st) (m :: entered, done)",
        "srcs.reverse.foldl (fun st s => P.visit fuel s st) (m :: entered, done)",
    ),
    "cycle cut after the module runs": (
        "EsmSplit/Plan.lean",
        "(st.1, st.2 ++ [m])",
        "(st.1.filter (· != m), st.2 ++ [m])",
    ),
}


def main():
    count = sys.argv[1] if len(sys.argv) > 1 else "400"
    for name, (file, old, new) in MUTANTS.items():
        with tempfile.TemporaryDirectory() as tmp:
            lean = pathlib.Path(tmp) / "lean"
            shutil.copytree(ROOT / "lean", lean, ignore=shutil.ignore_patterns(".lake"))
            src = (lean / file).read_text()
            assert old in src, f"mutation site not found: {name}"
            (lean / file).write_text(src.replace(old, new))
            # proofs about the mutated code may no longer hold; we only want the executable
            sem = (lean / "EsmSplit/Semantics.lean").read_text()
            a, b_ = sem.index("theorem Bundle.step_comm"), sem.index("theorem Bundle.exec_cons")
            head = sem[a : sem.index(":= by", a)]
            (lean / "EsmSplit/Semantics.lean").write_text(sem[:a] + head + ":= by sorry\n\n" + sem[b_:])
            for f in ["EsmSplit/Instance.lean", "EsmSplit/Controls.lean"]:
                (lean / f).write_text((lean / f).read_text().replace("by decide +kernel", "by sorry"))
            b = subprocess.run(["lake", "build", "render"], check=False, cwd=lean, capture_output=True, text=True)
            if b.returncode:
                # the changed definition is no longer the one a theorem was proved about
                first_error = next((ln for ln in b.stdout.splitlines() if "error" in ln), "")
                print(f"{'CAUGHT by a proof':22} {name:36} build fails: {first_error[:90]}", flush=True)
                continue
            env = dict(os.environ, RENDER=str(lean / ".lake/build/bin/render"))
            r = subprocess.run(
                [sys.executable, str(ROOT / "fuzz/fuzz.py"), count, "7"],
                check=False,
                env=env,
                capture_output=True,
                text=True,
            )
            tally = next((ln for ln in r.stdout.splitlines() if ln.startswith("{")), "")
            verdict = "CAUGHT" if r.returncode else ("no valid split at all" if "'valid'" not in tally else "SURVIVED")
            print(f"{verdict:22} {name:36} {tally}", flush=True)


main()
