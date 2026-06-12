"""Generate a synthetic debundle corpus at the gaffer-scale proposer shape.

The proposer perf baseline in `perf/proposer.md` was historically measured
against a private downstream fixture that public CI cannot access. This
generator produces a public stand-in with the same graph shape the perf doc
describes: `|V| ~= 10^4` owners, `|E| = O(|V|)`, block-clustered eager
references (so the greedy proposer finds mergeable communities), a lazy-edge
minority, at-init calls (exercising promotion), a sprinkle of asymmetric
I-cycles (lazy forward, eager back — the shape that drives the gate ladder
to tier 2/3), and a sampled export surface.

`--claim-blocks N` assigns the first N blocks to pre-existing spec modules
(`auto_partition/block_<k>`), both in the run spec's `logical_modules` (so
the emitted owner graph carries non-residual destinations and the proposer
seeds pre-existing-module classes) and as a spec-modules YAML tree for
`modules propose`. Claiming a contiguous *prefix* keeps the spec
realizable: claimed blocks only reference earlier (claimed) blocks, so all
residual→claimed eager reads point at modules that evaluate first.
`--claim-blocks 0` leaves the graph fully residual — every proposer merge
is then a delta-free tier-0 query.

Output layout under `--out`:

    snapshot/package.json     ESM marker
    snapshot/static/app.js    the synthetic chunk
    extracted/js-files.txt    chunk list for `inputs.js_list_path`
    spec.json                 minimal `debundle run` spec (absolute paths)
    modules/                  spec-modules tree for `modules propose`

Measurement recipe (see `perf/proposer.md` "How to run"):

    gen_synth_corpus --out /tmp/synth --statements 10000 --seed 1 --claim-blocks 62
    debundle run --spec /tmp/synth/spec.json
    DEBUNDLE_TIMING=1 debundle modules propose \\
        --graph /tmp/synth/out/reports/tree/static/app/owner_graph.json \\
        --modules /tmp/synth/modules --format json >/dev/null
"""

import argparse
import itertools
import json
import random
from dataclasses import dataclass, field
from pathlib import Path

# Tuned so a 10k-statement corpus lands near the historical fixture shape:
# blocks of 20-60 owners (proposal-sized communities), ~12% lazy function
# owners, ~8% at-init calls, ~5% cross-block eager refs, ~10% exports.
BLOCK_MIN = 20
BLOCK_MAX = 60
P_FUNCTION = 0.12
P_AT_INIT_CALL = 0.20
P_CROSS_BLOCK_REF = 0.05
P_EXPORT = 0.10
ASYMMETRIC_CYCLE_PAIRS = 15
EXPORTS_PER_STATEMENT = 50


@dataclass
class Chunk:
    source: str
    # Block ordinal -> binding names declared in that block, in
    # declaration order (claimable members for `--claim-blocks`).
    blocks: dict[int, list[str]] = field(default_factory=dict)


def generate_chunk(n_statements: int, rng: random.Random) -> Chunk:
    lines = ['const anchor = "anchor";']
    consts: list[str] = []
    funcs: list[str] = []
    block_consts: list[str] = []
    exports = ["anchor"]
    blocks: dict[int, list[str]] = {}
    block = 0
    block_left = 0
    for i in range(n_statements):
        if block_left == 0:
            block += 1
            block_left = rng.randint(BLOCK_MIN, BLOCK_MAX)
            block_consts = []
            blocks[block] = []
        block_left -= 1
        roll = rng.random()
        if roll < P_FUNCTION and block_consts:
            # Function owner: lazy reads of block consts (occasionally
            # cross-block) that only constrain init order via promotion.
            name = f"b{block}_f{i}"
            refs = rng.sample(block_consts, k=min(len(block_consts), rng.randint(1, 2)))
            if consts and rng.random() < P_CROSS_BLOCK_REF * 4:
                refs.append(rng.choice(consts))
            lines.append(f"function {name}() {{ return {' + '.join(refs)}; }}")
            funcs.append(name)
        elif roll < P_AT_INIT_CALL and funcs:
            # At-init call: exercises eager-edge promotion through the
            # callee's transitive lazy reads.
            name = f"b{block}_w{i}"
            lines.append(f"const {name} = {rng.choice(funcs[-40:])}();")
            block_consts.append(name)
            consts.append(name)
        else:
            name = f"b{block}_v{i}"
            refs = rng.sample(block_consts, k=min(len(block_consts), rng.randint(1, 3)))
            if consts and rng.random() < P_CROSS_BLOCK_REF:
                refs.append(rng.choice(consts))
            expr = " + ".join(refs) if refs else str(rng.randint(1, 1000))
            lines.append(f"const {name} = {expr};")
            block_consts.append(name)
            consts.append(name)
        blocks[block].append(name)
        if rng.random() < P_EXPORT:
            exports.append(name)
    # Asymmetric I-cycles (lazy forward, eager back): each pair is a 2-owner
    # I-SCC with one constraining edge. The extra block-const references give
    # the greedy a reason to pull the two owners into different block cells,
    # turning the pair into a cross-module I-cycle the ladder must escalate
    # past tier 1 for.
    for pair in range(ASYMMETRIC_CYCLE_PAIRS):
        fwd, back = rng.sample(consts, k=2)
        lines.append(f"function x{pair}_f() {{ return x{pair}_v[0] + {fwd}; }}")
        # Fresh array literal keeps the statement pure (no S-chain edge)
        # while still eagerly reading the pair's function binding.
        lines.append(f"const x{pair}_v = [x{pair}_f, {back}];")
    lines.extend(
        f"export {{ {', '.join(batch)} }};" for batch in itertools.batched(exports, EXPORTS_PER_STATEMENT, strict=False)
    )
    return Chunk(source="\n".join(lines) + "\n", blocks=blocks)


def member_entry(name: str) -> dict[str, object]:
    return {"name": name, "selector": {"binding": {"name": name}}}


def write_corpus(out_root: Path, n_statements: int, seed: int, claim_blocks: int) -> None:
    snapshot = out_root / "snapshot"
    snapshot.joinpath("static").mkdir(parents=True, exist_ok=True)
    extracted = out_root / "extracted"
    extracted.mkdir(parents=True, exist_ok=True)
    modules_root = out_root / "modules"
    modules_root.mkdir(parents=True, exist_ok=True)

    chunk = generate_chunk(n_statements, random.Random(seed))
    snapshot.joinpath("package.json").write_text(json.dumps({"type": "module"}) + "\n")
    snapshot.joinpath("static/app.js").write_text(chunk.source)
    js_list_path = extracted / "js-files.txt"
    js_list_path.write_text("static/app.js\n")

    logical_modules: dict[str, dict[str, object]] = {"anchors/anchor": {"members": [member_entry("anchor")]}}
    claimed_blocks = [k for k in chunk.blocks if k <= claim_blocks]
    for k in claimed_blocks:
        members = chunk.blocks[k]
        logical_modules[f"auto_partition/block_{k}"] = {"members": [member_entry(m) for m in members]}
        yaml_lines = ["members:"]
        for m in members:
            yaml_lines += [f"  - name: {m}", "    selector:", "      binding:", f"        name: {m}"]
        module_yaml = modules_root / f"auto_partition/block_{k}.yaml"
        module_yaml.parent.mkdir(parents=True, exist_ok=True)
        module_yaml.write_text("\n".join(yaml_lines) + "\n")

    spec = {
        "inputs": {"input_root": str(snapshot), "js_list_path": str(js_list_path)},
        "logical_modules": {"static/app": logical_modules},
        "chunk_renames": {},
        "unassigned_mode": {"static/app": {"kind": "inline_in_entry"}},
        "materialize_logical_modules": {
            "prune_other_chunks": False,
            "report_out_dir": str(out_root / "out/reports/tree"),
            "target_dir": "modules",
        },
        "write_js_tree": {"out_dir": str(out_root / "out")},
    }
    out_root.joinpath("spec.json").write_text(json.dumps(spec, indent=2) + "\n")
    claimed = sum(len(chunk.blocks[k]) for k in claimed_blocks)
    print(
        f"synthetic corpus written to {out_root}: {n_statements} statements, "
        f"{len(chunk.blocks)} blocks, {len(claimed_blocks)} claimed modules ({claimed} bindings)"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--statements", type=int, default=10_000)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--claim-blocks", type=int, default=0)
    args = parser.parse_args()
    write_corpus(args.out, args.statements, args.seed, args.claim_blocks)


if __name__ == "__main__":
    main()
