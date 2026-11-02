// Raw source for freezing a page's wall clock. Never imported as a module:
// frontend_visual.frozen_clock_script reads this file as text and injects it as
// `(() => { <file contents> frozenClock(nowMs); })()` before any page script
// runs (Playwright add_init_script). Keep it a single `const frozenClock = ...`
// declaration with no imports.
const frozenClock = (nowMs) => {
  const OriginalDate = Date;
  class FrozenDate extends OriginalDate {
    constructor(...args) {
      if (args.length === 0) {
        super(nowMs);
      } else {
        super(...args);
      }
    }
    static now() {
      return nowMs;
    }
  }
  globalThis.Date = FrozenDate;
};
