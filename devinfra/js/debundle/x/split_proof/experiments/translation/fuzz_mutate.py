"""Mutation test of the fuzzer: weaken the checker in a scratch copy, rebuild `translate`, run `fuzz.mjs`.

    python3 fuzz_mutate.py [bundles-per-mutant] [seed] [only-substring]

A mutant the fuzzer does not flag as a WRONG ACCEPT either is harmless (the rule it removes is redundant or
conservative) or shows a gap in the fuzzer's catalog. Each line says which.
"""

import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent.parent
BUNDLE, PLAN, LOWER, TRANSLATE = (
    "EsmSplit/Bundle.lean",
    "EsmSplit/Plan.lean",
    "Experiments/Lower.lean",
    "Experiments/Translate.lean",
)

# name: (file, old text, new text, why the fuzzer should see it)
MUTANTS = {
    "drop the effect-effect conflict": (
        BUNDLE,
        "(a.effect && b.effect)\n    || (!b.hoisted",
        "false\n    || (!b.hoisted",
        "effects swap",
    ),
    "drop read-after-declare": (
        BUNDLE,
        "|| (!b.hoisted && b.binds.any",
        "|| (false && b.binds.any",
        "a read runs before its declaration",
    ),
    "drop declare-after-read": (
        BUNDLE,
        "|| (!a.hoisted && a.binds.any",
        "|| (false && a.binds.any",
        "`var` read before its declaration",
    ),
    "imports run in reverse order": (
        PLAN,
        "srcs.foldl (fun st s => P.visit fuel s st) (m :: entered, done)",
        "srcs.reverse.foldl (fun st s => P.visit fuel s st) (m :: entered, done)",
        "module order",
    ),
    "conflicts ignore writes": (
        BUNDLE,
        "def Stmt.binds (s : Stmt) : List Name := s.defs ++ s.writes",
        "def Stmt.binds (s : Stmt) : List Name := s.defs",
        "a read of a variable moves across its write",
    ),
    "a call writes nothing": (
        LOWER,
        "let writes := if effect then (own ++",
        "let writes := if false then (own ++",
        "a call that assigns through a function moves past a reader",
    ),
    "blocks do not declare var": (
        LOWER,
        "  | _ => varNames s\n",
        "  | _ => []\n",
        "`{ var x }` declares a module-scope name",
    ),
    "classes are hoisted": (
        LOWER,
        '  typ s == "FunctionDeclaration"\n    || ((typ s == "ExportNamedDeclaration"',
        '  typ s == "FunctionDeclaration" || typ s == "ClassDeclaration"\n    || ((typ s == "ExportNamedDeclaration"',
        "a class is in its temporal dead zone",
    ),
    "inert: array spread": (LOWER, 'typ e != "SpreadElement" && inertExpr e', "true", "a spread runs an iterator"),
    "inert: template with expressions": (
        LOWER,
        '"TemplateLiteral" => (items (fld j "expressions")).isEmpty',
        '"TemplateLiteral" => true',
        "a template calls toString",
    ),
    "inert: member read": (
        LOWER,
        '  | "Identifier" | "Literal" | "FunctionExpression" | "ArrowFunctionExpression" => true',
        '  | "Identifier" | "Literal" | "FunctionExpression" | "ArrowFunctionExpression" | "MemberExpression" => true',
        "a read runs a getter",
    ),
    "inert: destructuring declarations": (
        LOWER,
        'typ (fld d "id") == "Identifier" &&',
        "true &&",
        "a pattern runs an iterator",
    ),
    "inert: static block": (
        LOWER,
        '(!flag m "static" || isNull (fld m "value"))\n    | _ => false',
        '(!flag m "static" || isNull (fld m "value"))\n    | _ => true',
        "a static block runs at definition",
    ),
    "inert: assignment": (
        LOWER,
        '  | "Identifier" | "Literal" | "FunctionExpression" | "ArrowFunctionExpression" => true',
        '  | "Identifier" | "Literal" | "FunctionExpression" | "ArrowFunctionExpression" | "AssignmentExpression" => true',
        "a write moves",
    ),
    "no AssignsHome": (
        TRANSLATE,
        "fun _ => decide (AssignsHome B P)",
        "fun _ => true",
        "an assignment through an import",
    ),
    "no Hygienic": (TRANSLATE, "fun _ => decide (Hygienic B P)", "fun _ => true", "redundant with the link check"),
}


def main():
    count = sys.argv[1] if len(sys.argv) > 1 else "150"
    seed = sys.argv[2] if len(sys.argv) > 2 else "2"
    only = sys.argv[3] if len(sys.argv) > 3 else ""
    with tempfile.TemporaryDirectory() as tmp:
        lean = pathlib.Path(tmp) / "lean"
        shutil.copytree(ROOT / "lean", lean, symlinks=True)
        for name, (file, old, new, why) in MUTANTS.items():
            if only not in name:
                continue
            path = lean / file
            original = path.read_text()
            assert old in original, f"mutation site not found: {name}"
            path.write_text(original.replace(old, new))
            # proofs about a mutated definition may no longer hold; only the executable matters here
            sem = lean / "EsmSplit/Semantics.lean"
            sem_original = sem.read_text()
            a, b = sem_original.index("theorem Bundle.step_comm"), sem_original.index("theorem Bundle.exec_cons")
            sem.write_text(
                sem_original[:a]
                + sem_original[a : sem_original.index(":= by", a)]
                + ":= by sorry\n\n"
                + sem_original[b:]
            )
            others = {f: (lean / f).read_text() for f in ["EsmSplit/Instance.lean", "EsmSplit/Controls.lean"]}
            for f, text in others.items():
                (lean / f).write_text(text.replace("by decide +kernel", "by sorry"))
            try:
                build = subprocess.run(
                    ["lake", "build", "translate"], check=False, cwd=lean, capture_output=True, text=True
                )
                if build.returncode:
                    first = next((ln for ln in build.stdout.splitlines() if "error" in ln), "")
                    print(f"{'CAUGHT by a proof':18} {name:36} build fails: {first[:80]}", flush=True)
                    continue
                env = dict(os.environ, TRANSLATE=str(lean / ".lake/build/bin/translate"))
                run = subprocess.run(
                    ["node", str(HERE / "fuzz.mjs"), "--count", count, "--seed", seed],
                    check=False,
                    env=env,
                    capture_output=True,
                    text=True,
                )
                line = next(
                    (ln for ln in run.stdout.splitlines() if ln.startswith("WRONG ACCEPTS")),
                    run.stdout[-200:] + run.stderr[-200:],
                )
                print(f"{'CAUGHT' if run.returncode else 'SURVIVED':18} {name:36} {line}  [{why}]", flush=True)
            finally:
                path.write_text(original)
                sem.write_text(sem_original)
                for f, text in others.items():
                    (lean / f).write_text(text)


main()
