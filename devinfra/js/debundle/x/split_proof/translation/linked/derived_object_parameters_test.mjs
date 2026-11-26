import { assertLinkedPreviewResult, prepareLinkedFixture } from "./fixture_preview.mjs";
import { assertPublicEmissionRefused } from "../testing/public_refusal.mjs";

const checked = prepareLinkedFixture(
  "derived-object-parameter",
  `const trace = { value: "" };
  class Base { constructor(x) { trace.value += "s"; this.value = x; } }
  class Child extends Base {
    field = trace.value += "f";
    constructor({ first: x, second: y }) { trace.value += "p"; super(x + y); }
  }
  export const result = (() => {
    const good = { get first() { trace.value += "a"; return 2; }, get second() { trace.value += "b"; return 3; } };
    const value = new Child(good).value;
    try { new Child({ get first() { trace.value += "x"; throw 7; }, get second() { trace.value += "!"; } }); }
    catch (e) { if (e !== 7) throw e; }
    if (trace.value !== "abpsfx" || value !== 5 || Child.length !== 1) throw "parameter order";
    return trace.value;
  })();`,
  {
    entry: "entry.mjs",
    modules: { "base.mjs": ["#0", "#1"], "entry.mjs": ["#2", "#3"] },
    order: ["base.mjs"],
    sequential: true,
  }
);
assertPublicEmissionRefused(checked.manifest, checked.out);
await assertLinkedPreviewResult(checked, "abpsfx");
