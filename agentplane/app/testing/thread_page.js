// Page-side helpers of the thread history browser tests, installed into every page before it loads
// (thread_browser.py) and called by name through window.__threadPage.
//
// The reader's place is the first row whose bottom is below the top of the history area, and how far
// that row's top sits from the area's top. Tests record it, let the app react, and compare.
(() => {
  const sample = (area) => {
    const top = area.getBoundingClientRect().top;
    const row = [...area.querySelectorAll("[data-thread-anchor]")].find(
      (candidate) => candidate.getBoundingClientRect().bottom > top
    );
    const rowTop = row.getBoundingClientRect().top;
    return { cursor: row.dataset.threadAnchor, top: rowTop, offset: rowTop - top };
  };

  window.__threadPage = {
    // Resolves after the paint that follows the next layout, and any effect or observer it runs.
    frames: () => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))),

    // The reader's place, sampled once two consecutive frames agree, so a pending re-measure right
    // after a just-ended gesture cannot register as a false position.
    settledAnchor: (area) =>
      new Promise((resolve) => {
        const settle = (previous) =>
          requestAnimationFrame(() => {
            const current = sample(area);
            if (current.cursor === previous.cursor && current.top === previous.top) resolve(current);
            else settle(current);
          });
        requestAnimationFrame(() => settle(sample(area)));
      }),

    // `ended` resolves at the area's next scrollend event.
    scrollEnded: (area) => ({
      ended: new Promise((resolve) => area.addEventListener("scrollend", () => resolve(), { once: true })),
    }),

    // `anchor` resolves with the reader's place as the next scrollend event sees it, sampled inside
    // the event, which is the instant the app adopts it.
    anchorAtScrollend: (area) => ({
      anchor: new Promise((resolve) => area.addEventListener("scrollend", () => resolve(sample(area)), { once: true })),
    }),
  };
})();
//# sourceURL=thread_page.js
