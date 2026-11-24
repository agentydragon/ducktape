import { assertCertifiedResult, assertRejected, sequentialSplit } from "./harness.mjs";

const split = { "base.mjs": ["#0", "#1"], "entry.mjs": ["#2", "#3"] };

assertCertifiedResult(
  sequentialSplit(
    "derived-defaults-order",
    `const trace = { value: "" };
    class Base {
      constructor(value) { trace.value += "s"; this.value = value; }
    }
    class Child extends Base {
      ready = trace.value += "f";
      constructor(name, flag = (trace.value += "d", !1), last = flag) {
        const value = name + ":" + flag + ":" + last;
        trace.value += "p";
        super(value);
        trace.value += "b";
      }
    }
    export const result = (() => {
      if (Child.length !== 1) throw "incorrect constructor length";
      const first = new Child("a", undefined, "last");
      if (first.value !== "a:false:last" || first.ready !== "dpsf") throw "incorrect evaluation order";
      if (new Child("b").value !== "b:false:false") throw "missing argument or earlier parameter";
      if (new Child("c", false).value !== "c:false:false") throw "false argument ran the default";
      if (new Child("d", null).value !== "d:null:null") throw "null argument ran the default";
      if (trace.value !== "dpsfbdpsfbpsfbpsfb") throw "incorrect default side effects";
      return first.value;
    })();`,
    split,
    ["base.mjs"]
  ),
  "a:false:last"
);

assertCertifiedResult(
  sequentialSplit(
    "abrupt-derived-defaults",
    `const trace = { value: "" };
    class Base { constructor() { trace.value += "s"; } }
    class Child extends Base {
      field = trace.value += "f";
      constructor(fail, first = fail ? trace.abort() : typeof later, later = "ready") {
        trace.value += "p";
        super();
        trace.value += "b";
      }
    }
    export const result = (() => {
      trace.abort = () => { trace.value += "d"; throw "default"; };
      try { new Child(false); } catch (error) {
        if (typeof error !== "object") throw error;
        trace.value += "t";
      }
      if (trace.value !== "t") throw "default read a later parameter before initialization";
      try { new Child(true); } catch (error) {
        if (error !== "default") throw error;
        trace.value += "c";
      }
      if (trace.value !== "tdc") throw "an abrupt default reached the constructor body";
      return trace.value;
    })();`,
    split,
    ["base.mjs"]
  ),
  "tdc"
);

for (const [name, constructor, reason] of [
  ["receiver-in-default", "constructor(value = this.value) { super(); }", /before super/i],
  [
    "body-shadows-default",
    "constructor(value = outer) { const outer = 2; super(value); }",
    /default parameter|overlap|shadow/i,
  ],
]) {
  assertRejected(
    sequentialSplit(name, `const outer = 1; class Child extends Object { ${constructor} } export const result = 1;`),
    reason
  );
}
