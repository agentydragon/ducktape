// Markdown tables from the JSON lines `run_cases.mjs` wrote.
//
//   node summarize.mjs results/cases.jsonl > results/summary.md
import { readFileSync } from "node:fs";

const rows = process.argv
  .slice(2)
  .flatMap((f) => readFileSync(f, "utf8").split("\n").filter(Boolean))
  .map((line) => JSON.parse(line));

const kind = (r) => r.outcome.split(":")[0];
const cause = (r) => r.outcome.slice("unsupported:".length);
const groupBy = (items, key) => {
  const groups = new Map();
  for (const item of items) groups.set(key(item), [...(groups.get(key(item)) ?? []), item]);
  return groups;
};
const byCount = (a, b) => b[1].length - a[1].length || String(a[0]).localeCompare(String(b[0]));

// Padded like prettier pads markdown tables, so formatting the file changes nothing.
function table(header, body) {
  const all = [header, ...body];
  const widths = header.map((_, c) => Math.max(3, ...all.map((row) => String(row[c]).length)));
  const line = (cells) => `| ${cells.map((cell, c) => String(cell).padEnd(widths[c])).join(" | ")} |`;
  return [line(header), line(widths.map((w) => "-".repeat(w))), ...body.map(line)].join("\n");
}

const expectations = ["accept", "reject:atomic_conflict", "reject:cycle", "reject:stderr"];
const outcomeOrder = [
  "agree-accept",
  "agree-reject",
  "strict-but-runs",
  "strict-and-breaks",
  "unsound-accept",
  "lenient",
  "refusal-test",
  "unsupported",
  "error",
];
const outcomes = groupBy(rows, kind);
const out = [];

out.push("# debundle e2e cases against the Lean checker", "");
out.push(
  `${rows.length} spec entries from \`devinfra/js/debundle/e2e/cases\`. Definitions: [README](../README.md).`,
  ""
);

out.push("## Outcomes", "");
out.push(
  table(
    ["Outcome", ...expectations.map((e) => `debundle: ${e}`), "Total"],
    [...outcomeOrder, ...[...outcomes.keys()].filter((k) => !outcomeOrder.includes(k))]
      .filter((k) => outcomes.has(k))
      .map((k) => [
        k,
        ...expectations.map((e) => outcomes.get(k).filter((r) => r.expect === e).length),
        outcomes.get(k).length,
      ])
  ),
  ""
);

const agreeing = rows.filter((r) => kind(r) === "agree-accept");
const ran = rows.filter((r) => r.node);
const accepted = ran.filter((r) => r.expect === "accept");
const rejected = ran.filter((r) => r.expect === "reject:atomic_conflict" || r.expect === "reject:cycle");
out.push("## What the runs check", "");
out.push(
  table(
    ["Measure", "Value"],
    [
      ["Accepted splits that agree with debundle", agreeing.length],
      ["Of them, with at least one module besides the entry", agreeing.filter((r) => r.split.modules > 0).length],
      [
        "Statements moved out of the entry, of all statements in those cases",
        `${agreeing.reduce((n, r) => n + r.split.moved, 0)} of ${agreeing.reduce((n, r) => n + r.split.statements, 0)}`,
      ],
      [
        "Of them, with assertions I do not compare (`exports`, `source`, `probes`, `check_all_js`)",
        agreeing.filter((r) => r.unchecked.length > 0).length,
      ],
      [
        "Accept entries run whose original chunk prints the expected stdout",
        `${accepted.filter((r) => r.node.originalMatchesExpect).length} of ${accepted.length}`,
      ],
      [
        "Expected rejections whose modules, written anyway, print other output or fail",
        `${rejected.filter((r) => !r.node.emittedMatchesOriginal || r.node.emittedError).length} of ${rejected.length}`,
      ],
    ]
  ),
  ""
);

const unsupported = rows.filter((r) => kind(r) === "unsupported");
out.push("## Cases the adapter does not run, by cause", "");
out.push(
  table(
    ["Cause", "Cases", "debundle accepts", "debundle rejects", "Examples"],
    [...groupBy(unsupported, cause)].sort(byCount).map(([c, rs]) => [
      c,
      rs.length,
      rs.filter((r) => r.expect === "accept").length,
      rs.filter((r) => r.expect !== "accept").length,
      rs
        .slice(0, 2)
        .map((r) => `\`${r.id}\``)
        .join(", "),
    ])
  ),
  ""
);

const disagree = rows.filter((r) =>
  ["strict-but-runs", "strict-and-breaks", "unsound-accept", "lenient"].includes(kind(r))
);
out.push("## Cases where my verdict differs from debundle's", "");
out.push(
  table(
    ["Outcome", "Case", "Checks that failed", "Emitted modules in Node"],
    disagree.map((r) => [
      r.outcome,
      `\`${r.id}\``,
      r.mine.failed.length ? [...new Set(r.mine.failed)].join(", ") : "(none)",
      r.node.emittedError || (r.node.emittedMatchesExpect ? "expected output" : "other output"),
    ])
  ),
  ""
);

out.push("## By area", "");
out.push(
  table(
    ["Area", "Entries", "Agree", "Strict", "Unsound or lenient", "Refusal tests", "Not run"],
    [...groupBy(rows, (r) => r.area)].sort(byCount).map(([area, rs]) => {
      const n = (pred) => rs.filter((r) => pred(kind(r))).length;
      return [
        area,
        rs.length,
        n((k) => k.startsWith("agree")),
        n((k) => k.startsWith("strict")),
        n((k) => k === "unsound-accept" || k === "lenient"),
        n((k) => k === "refusal-test"),
        n((k) => k === "unsupported" || k === "error"),
      ];
    })
  )
);

console.log(out.join("\n"));
