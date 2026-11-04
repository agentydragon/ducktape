(() => {
  const labelFor = (element, fallback) =>
    element.getAttribute("aria-label") ||
    element.labels?.[0]?.innerText.trim() ||
    element.name ||
    element.id ||
    fallback;

  const sameDocumentTargetExists = (target) => {
    let id;
    try {
      id = decodeURIComponent(target.hash.slice(1));
    } catch {
      return false;
    }
    return Boolean(document.getElementById(id) || document.querySelector(`[name="${CSS.escape(id)}"]`));
  };

  window.__ciReportAudit = {
    anchorAudit() {
      const anchors = Array.from(document.querySelectorAll("a[href]"));
      const missing = anchors.flatMap((anchor) => {
        const target = new URL(anchor.href, document.location.href);
        if (target.pathname !== document.location.pathname || !target.hash) return [];
        return sameDocumentTargetExists(target) ? [] : [anchor.getAttribute("href")];
      });
      return { link_count: anchors.length, missing };
    },

    expandDetails() {
      const details = Array.from(document.querySelectorAll("details"));
      details.forEach((element) => {
        element.open = true;
      });
      return details.length;
    },

    dimensions() {
      return {
        viewport_width: window.innerWidth,
        document_width: document.documentElement.scrollWidth,
        body_width: document.body.scrollWidth,
      };
    },

    wideTableScrollRegions() {
      const wrappers = Array.from(document.querySelectorAll(".table-scroll"));
      return Array.from(document.querySelectorAll(".table-scroll > table.wide-6, .table-scroll > table.wide-7")).map(
        (table) => {
          const wrapper = table.parentElement;
          return {
            wrapper_index: wrappers.indexOf(wrapper),
            label: wrapper.getAttribute("aria-label"),
            role: wrapper.getAttribute("role"),
            tab_index: wrapper.tabIndex,
            wrapper_client_width: wrapper.clientWidth,
            wrapper_scroll_width: wrapper.scrollWidth,
            table_scroll_width: table.scrollWidth,
            table_min_width: getComputedStyle(table).minWidth,
          };
        }
      );
    },

    controls() {
      const selects = Array.from(document.querySelectorAll("select"));
      const checkboxes = Array.from(document.querySelectorAll('input[type="checkbox"]'));
      const radios = Array.from(document.querySelectorAll('input[type="radio"]'));
      return {
        selects: selects.map((element) => ({
          label: labelFor(element, "select"),
          value: element.value,
          disabled: element.disabled,
          options: Array.from(element.options)
            .filter((option) => !option.disabled && option.value !== element.value)
            .map((option) => option.value),
        })),
        checkboxes: checkboxes.map((element) => ({
          label: labelFor(element, "checkbox"),
          checked: element.checked,
          disabled: element.disabled,
        })),
        radios: radios.map((element, index) => ({
          index,
          name: element.name,
          label: labelFor(element, "radio"),
          checked: element.checked,
          disabled: element.disabled,
        })),
      };
    },
  };
})();
