(function () {
  try {
    if (window.hljs) window.hljs.highlightAll();
  } catch (err) {
    console.warn("syntax highlighting failed", err);
  }

  const rows = JSON.parse(document.getElementById("matrix-rows").textContent);
  const trs = document.querySelectorAll(".matrix tbody tr");

  function inline(text) {
    const el = document.createElement("div");
    el.textContent = text;
    return el.innerHTML.replace(/`([^`]+)`/g, "<code>$1</code>").replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  }

  function select(index) {
    trs.forEach((tr) => tr.classList.toggle("sel", Number(tr.dataset.row) === index));
    const row = rows[index];
    document.getElementById("rd-title").textContent = row.title;
    document.getElementById("rd-today").innerHTML = inline(row.today);
    document.getElementById("rd-note").innerHTML = inline(row.note);
    document.getElementById("rd-conv").textContent = row.conventions;
  }

  trs.forEach((tr) => {
    tr.addEventListener("click", () => select(Number(tr.dataset.row)));
    tr.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter" || ev.key === " ") {
        ev.preventDefault();
        select(Number(tr.dataset.row));
      }
    });
  });
})();
