"use strict";

const sortControl = document.querySelector("#test-sort");
const gallery = document.querySelector("main");

if (sortControl && gallery) {
  const tests = Array.from(gallery.children).filter((element) => element.matches("section"));
  const originalOrder = new Map(tests.map((test, index) => [test, index]));
  const number = (test, attribute) => Number(test.dataset[attribute] || 0);

  sortControl.addEventListener("change", () => {
    const sorted = [...tests].sort((left, right) => {
      if (sortControl.value === "name") {
        const titleOrder = left.dataset.title.localeCompare(right.dataset.title, "en", { sensitivity: "base" });
        if (titleOrder !== 0) return titleOrder;
      } else if (sortControl.value === "changed") {
        const countOrder = number(right, "changedCount") - number(left, "changedCount");
        if (countOrder !== 0) return countOrder;
        const impactOrder = number(right, "largestChange") - number(left, "largestChange");
        if (impactOrder !== 0) return impactOrder;
      }
      return originalOrder.get(left) - originalOrder.get(right);
    });

    gallery.append(...sorted);
  });
}
