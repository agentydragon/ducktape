(target) => {
  const describe = (element) => {
    const { x, y, width, height } = element.getBoundingClientRect();
    const style = getComputedStyle(element);
    return {
      tag: element.tagName.toLowerCase(),
      classes: Array.from(element.classList).slice(0, 4),
      role: element.getAttribute("role"),
      rect: { x, y, width, height },
      scrollTop: element.scrollTop,
      scrollHeight: element.scrollHeight,
      clientHeight: element.clientHeight,
      overflowY: style.overflowY,
      "data-layout-settled": element.getAttribute("data-layout-settled"),
      "data-scroll-mode": element.getAttribute("data-scroll-mode"),
    };
  };
  const scrollable = (element) => {
    const overflowY = getComputedStyle(element).overflowY;
    return /^(auto|scroll|hidden|overlay)$/.test(overflowY) && element.scrollHeight > element.clientHeight;
  };
  const elements = Array.from(document.querySelectorAll("*"));
  const canvases = Array.from(document.querySelectorAll("canvas"), (canvas) => {
    const { x, y, width, height } = canvas.getBoundingClientRect();
    const style = getComputedStyle(canvas);
    return {
      rect: { x, y, width, height },
      computedCssWidth: style.width,
      computedCssHeight: style.height,
      bitmapWidth: canvas.width,
      bitmapHeight: canvas.height,
    };
  });
  const scrollables = [];
  for (const [domIndex, element] of elements.entries()) {
    if (!scrollable(element)) continue;
    scrollables.push({ domIndex, ...describe(element) });
  }
  const targetScrollAncestors = [];
  for (let element = target && target.parentElement; element; element = element.parentElement) {
    if (scrollable(element)) targetScrollAncestors.push(describe(element));
  }
  const focused = document.activeElement;
  const selection = window.getSelection();
  const selectedRect = selection && selection.rangeCount ? selection.getRangeAt(0).getBoundingClientRect() : null;
  return {
    viewport: {
      width: window.innerWidth,
      height: window.innerHeight,
      deviceScaleFactor: window.devicePixelRatio,
      scrollX: window.scrollX,
      scrollY: window.scrollY,
    },
    readiness: {
      documentReadyState: document.readyState,
      fontStatus: document.fonts.status,
      incompleteImageCount: Array.from(document.images).filter((image) => !image.complete).length,
    },
    documentScroll: document.scrollingElement ? describe(document.scrollingElement) : null,
    canvases,
    focus: focused
      ? {
          ...describe(focused),
          inputType: focused instanceof HTMLInputElement ? focused.type : null,
          contentEditable: focused.isContentEditable,
          inputSelection:
            typeof focused.selectionStart === "number"
              ? {
                  start: focused.selectionStart,
                  end: focused.selectionEnd,
                  direction: focused.selectionDirection,
                }
              : null,
        }
      : null,
    selection: selection
      ? {
          collapsed: selection.isCollapsed,
          rangeCount: selection.rangeCount,
          rect: selectedRect
            ? { x: selectedRect.x, y: selectedRect.y, width: selectedRect.width, height: selectedRect.height }
            : null,
        }
      : null,
    target: target ? describe(target) : null,
    targetScrollAncestors,
    scrollables,
  };
};
