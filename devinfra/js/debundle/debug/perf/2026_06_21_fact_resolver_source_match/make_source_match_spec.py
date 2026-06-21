"""Rewrite a gen_synth_corpus spec's member selectors from binding-name to
source_match form, so `debundle run` exercises the production fact-based
ChunkResolver (chunk_facts EDB + selector_match homomorphism) on every member.

Each generated top-level statement is single-line:
  const NAME = EXPR;      -> target_binding NAME, match = whole line
  function NAME() {...}    -> target_binding NAME, match = whole line
We map binding name -> exact source line, then emit
  selector: {source_match: {match: "<line>", identifiers: exact, target_binding: NAME}}
Exact-identifier mode + the unique declaration line => unique resolution.
"""

import json
import re
import sys
from pathlib import Path

root = Path(sys.argv[1])
chunk = (root / "snapshot/static/app.js").read_text().splitlines()

# binding name -> source line (declarations are one per line in the generator)
decl_re = re.compile(r"^(?:const|function)\s+([A-Za-z_]\w*)")
name_to_line: dict[str, str] = {}
for line in chunk:
    m = decl_re.match(line)
    if m:
        name_to_line.setdefault(m.group(1), line)


def to_source_match(name: str) -> dict:
    line = name_to_line[name]
    return {"source_match": {"match": line, "identifiers": "exact", "target_binding": name}}


spec = json.loads((root / "spec.json").read_text())
lm = spec["logical_modules"]["static/app"]
converted = 0
skipped = 0
for mod in lm.values():
    for member in mod["members"]:
        nm = member["name"]
        if nm in name_to_line:
            member["selector"] = to_source_match(nm)
            converted += 1
        else:
            skipped += 1  # e.g. 'anchor' is `const anchor = "anchor";` -> present; at-init calls present too

# point output elsewhere so we don't clobber the binding-name run
spec["materialize_logical_modules"]["report_out_dir"] = str(root / "out_sm/reports/tree")
spec["write_js_tree"]["out_dir"] = str(root / "out_sm")
out_spec = root / "spec_sourcematch.json"
out_spec.write_text(json.dumps(spec, indent=2) + "\n")
print(f"wrote {out_spec}: converted={converted} members to source_match, skipped(no decl line)={skipped}")
print(f"distinct decl lines mapped: {len(name_to_line)}")
