"""Run the output checker on: the splitter's output, an independently written reshuffle, and tampered copies.

python3 check_all.py
"""

import json
import pathlib
import shutil
import subprocess
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent.parent
BUNDLE = ROOT / "example/input/bundle.mjs"
CHECK = ROOT / "lean/.lake/build/bin/check"


def node(dir_, entry):
    p = subprocess.run(["node", entry], check=False, cwd=dir_, capture_output=True, text=True)
    err = next((line for line in p.stderr.splitlines() if "Error:" in line), "")
    return p.stdout if p.returncode == 0 else f"{err.strip()}"


EXPECTED = node(ROOT / "example/input", "bundle.mjs")


def edit(dir_, file, old, new):
    p = dir_ / file
    s = p.read_text()
    assert old in s, (file, old)
    p.write_text(s.replace(old, new, 1))


def tamper_swap_effects(d):
    edit(
        d,
        "plugins.mjs",
        'register("area", area);\nregister("label", label);',
        'register("label", label);\nregister("area", area);',
    )


def tamper_drop_import(d):
    edit(d, "main.mjs", 'import { registry } from "./registry.mjs";\n', "")


def tamper_extra(d):
    edit(d, "main.mjs", 'console.log(registry.join(","));', 'console.log(registry.join(","));\nconsole.log("extra");')


def tamper_edit_text(d):
    edit(d, "utils.mjs", "3.14159", "3.14")


def tamper_forgot_export(d):
    edit(d, "utils.mjs", "export { area, label, isEven };", "export { area, isEven };")


def tamper_drop_effects_import(d):
    edit(d, "main.mjs", 'import "./plugins.mjs";\n', "")


def tamper_cycle_tdz(d):
    edit(d, "utils.mjs", 'const label = (s) => "<" + s + ">";\n', "")
    edit(d, "utils.mjs", "export { area, label, isEven };", "export { area, isEven };")
    edit(
        d,
        "plugins.mjs",
        'import { area, label } from "./utils.mjs";',
        'import { area } from "./utils.mjs";\nimport { label } from "./main.mjs";',
    )
    edit(
        d,
        "main.mjs",
        'import { label, area, isEven } from "./utils.mjs";',
        'import { area, isEven } from "./utils.mjs";',
    )
    edit(
        d,
        "main.mjs",
        "console.log(label(",
        'const label = (s) => "<" + s + ">";\nexport { label };\nconsole.log(label(',
    )


CASES = [
    ("splitter output (example/output/)", None, None),
    ("independent reshuffle (external_ok/)", "external_ok", None),
    ("swap two registrations", None, tamper_swap_effects),
    ("forget an import", None, tamper_drop_import),
    ("add a console.log", None, tamper_extra),
    ("edit a constant", None, tamper_edit_text),
    ("forget an export", None, tamper_forgot_export),
    ('forget `import "./plugins.mjs"`', None, tamper_drop_effects_import),
    ("move label into main (cycle)", None, tamper_cycle_tdz),
]

print(f"{'case':38} {'checker':8} {'first failing check / reason':46} node")
for name, src, tamper in CASES:
    with tempfile.TemporaryDirectory() as tmp:
        d = pathlib.Path(tmp) / "out"
        shutil.copytree(ROOT / "example/output" if src is None else HERE / src, d)
        if tamper:
            tamper(d)
        facts = subprocess.run(
            ["node", ROOT / "extract/extract_output.mjs", BUNDLE, d, "main.mjs"],
            check=False,
            capture_output=True,
            text=True,
        )
        if facts.returncode:
            verdict, why = "REJECT", "parser: " + facts.stderr.strip().splitlines()[-1][:60]
        else:
            (pathlib.Path(tmp) / "facts.json").write_text(facts.stdout)
            r = subprocess.run(
                [CHECK, str(pathlib.Path(tmp) / "facts.json")], check=False, capture_output=True, text=True
            )
            lines = r.stdout.splitlines()
            verdict = "ACCEPT" if r.returncode == 0 else "REJECT"
            why = (
                next((ln.split()[1] for ln in lines if ln.strip().startswith("FAIL")), "")
                if verdict == "REJECT"
                else ""
            )
            if verdict == "REJECT" and not why:
                why = lines[1].strip()[:60] if len(lines) > 1 else lines[0][:60]
        got = node(d, "main.mjs")
        shown = (
            "same output"
            if got == EXPECTED
            else (got if got.startswith(("ReferenceError", "SyntaxError")) else json.dumps(got))
        )
        print(f"{name:38} {verdict:8} {why[:44]:46} {shown[:60]}", flush=True)

# Regression: a callback passed to a call runs while the statement runs, so `PI` must already exist.
# An earlier front end called the callback's reads "lazy" and accepted this split; it throws on load.
d = HERE / "callback_bug"
facts = subprocess.run(
    ["node", ROOT / "extract/extract_output.mjs", d / "bundle.mjs", d / "out", "consts.mjs"],
    capture_output=True,
    text=True,
    check=True,
)
r = subprocess.run([CHECK, "/dev/stdin"], check=False, input=facts.stdout, capture_output=True, text=True)
failing = next((ln.split()[1] for ln in r.stdout.splitlines() if ln.strip().startswith("FAIL")), "")
print(
    f"\nregression: callback reads a later constant -> {'REJECT' if r.returncode else 'ACCEPT'} {failing}  node: {node(d / 'out', 'consts.mjs')}"
)
assert r.returncode != 0, "front end regression: callback hole is back"
