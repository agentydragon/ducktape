const pendingKey = "plaid-link-pending";
let webConfig = { transaction_days: 730, max_transaction_days: 730 };
// The institution drives everything: pick it, ask Plaid what it offers, request that. Requesting a
// product an institution does not support fails the whole Link, so a fixed product set chosen
// before knowing the bank was the one thing guaranteed to break on some of them.
let selectedInstitution = null;
let searchSeq = 0;

const statusEl = document.getElementById("status");

function setStatus(message, { error = false } = {}) {
  statusEl.textContent = message || "";
  statusEl.classList.toggle("error", Boolean(message) && error);
}
function escapeHtml(value) {
  return String(value || "").replace(
    /[&<>"']/g,
    (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]
  );
}
function pills(products) {
  if (!products || products.length === 0) return '<span class="muted">none recorded</span>';
  return `<div class="pill-row">${products.map((product) => `<span class="pill">${escapeHtml(product)}</span>`).join("")}</div>`;
}
// Rendered only when there is something to add, and labelled with what that is. A fixed
// "Add scopes" button silently degraded into a plain re-auth when nothing was missing, and never
// said which products consenting would actually grant.
function addProductsButton(link) {
  const addable = link.addable_products;
  if (!addable || addable.length === 0) return "";
  return `<button class="secondary" data-action="update" data-addable="${escapeHtml(addable.join(","))}">Add ${escapeHtml(addable.join(" + "))}</button>`;
}
function selectedProducts() {
  return Array.from(document.querySelectorAll("#products input:checked")).map((input) => input.value);
}
function historySummary(link) {
  const hasTransactions =
    (link.products_requested || []).includes("transactions") ||
    (link.products_authorized || []).includes("transactions");
  if (!hasTransactions) return '<span class="muted">No transaction history requested.</span>';
  const requested =
    link.transaction_days_requested === null || link.transaction_days_requested === undefined
      ? "Requested: unknown"
      : `Requested: ${escapeHtml(link.transaction_days_requested)} days`;
  const count = Number(link.synced_transaction_count || 0).toLocaleString();
  if (link.observed_transaction_history_days === null || link.observed_transaction_history_days === undefined) {
    return `${requested}<div class="meta muted">Observed: no synced transactions yet</div>`;
  }
  const dates =
    link.earliest_transaction_date && link.latest_transaction_date
      ? `, ${escapeHtml(link.earliest_transaction_date)} to ${escapeHtml(link.latest_transaction_date)}`
      : "";
  return `${requested}<div class="meta muted">Observed: ${escapeHtml(link.observed_transaction_history_days)} days${dates}, ${count} transactions</div>`;
}
async function apiFetch(url, options) {
  const response = await fetch(url, options);
  const contentType = response.headers.get("content-type") || "";
  const body = contentType.includes("application/json") ? await response.json() : await response.text();
  if (!response.ok) {
    throw new Error(apiErrorMessage(body, response));
  }
  return body;
}
function apiErrorMessage(body, response) {
  const detail = body && typeof body === "object" && "detail" in body ? body.detail : body;
  if (detail && typeof detail === "object") {
    const bits = [];
    if (detail.error_code) bits.push(detail.error_code);
    if (detail.error_message) bits.push(detail.error_message);
    if (detail.request_id) bits.push(`request ${detail.request_id}`);
    if (bits.length) return bits.join(": ");
    return JSON.stringify(detail);
  }
  return String(detail || `${response.status} ${response.statusText}`);
}
async function withStatus(message, work) {
  setStatus(message);
  document.querySelectorAll("button").forEach((button) => {
    button.disabled = true;
  });
  try {
    const result = await work();
    return result;
  } catch (error) {
    setStatus(error.message || String(error), { error: true });
    throw error;
  } finally {
    document.querySelectorAll("button").forEach((button) => {
      button.disabled = false;
    });
    // Connect's disabled state is derived, not a spinner: blanket re-enabling would arm it with no
    // institution chosen or no product checked, and submitting then dereferences a null selection.
    // Same for a row whose sync is still in flight -- re-arming it just buys a 409.
    setFormEnabled();
    document.querySelectorAll('#links button[data-action="sync"]').forEach((sync) => {
      sync.disabled = sync.textContent.trim() !== "Sync data";
    });
  }
}
async function loadConfig() {
  webConfig = await apiFetch("/api/config");
  const input = document.getElementById("transaction-days");
  input.max = String(webConfig.max_transaction_days);
  input.value = String(webConfig.transaction_days);
  renderProductChecks();
  setFormEnabled();
}

async function searchInstitutions(query) {
  const seq = ++searchSeq;
  const results = await apiFetch(`/api/institutions?q=${encodeURIComponent(query)}`);
  // Keystrokes race; a slow earlier request must not overwrite a newer result set.
  if (seq !== searchSeq) return;
  const box = document.getElementById("institution-results");
  box.innerHTML = results
    .map(
      (i) => `<button type="button" data-institution="${escapeHtml(i.institution_id)}">${escapeHtml(i.name)}</button>`
    )
    .join("");
  box.classList.toggle("hidden", results.length === 0);
}

// The checkbox set never changes -- only which entries are available. A control that disappears
// reads as "this app forgot about liabilities"; one that is present and greyed reads as "this bank
// does not offer it", which is the true statement.
function renderProductChecks() {
  document.getElementById("products").innerHTML = (webConfig.products || [])
    .map(
      (product) =>
        `<label class="check"><input type="checkbox" value="${escapeHtml(product)}" checked /><span>${escapeHtml(product)}</span></label>`
    )
    .join("");
}

function applyInstitutionSupport(supported) {
  // `supported === null` means no institution is chosen: nothing is known to be unavailable, so
  // everything is offered again.
  document.querySelectorAll("#products input").forEach((input) => {
    const available = supported === null || supported.includes(input.value);
    input.disabled = !available;
    input.checked = available;
  });
}

async function selectInstitution(institutionId) {
  document.getElementById("institution-results").classList.add("hidden");
  const detail = await apiFetch(`/api/institutions/${encodeURIComponent(institutionId)}`);
  selectedInstitution = detail;
  document.getElementById("institution").value = detail.name;
  applyInstitutionSupport(detail.syncable_products);
  const hint = document.getElementById("institution-hint");
  // Two things this sentence has to get right. "Also offered but not synced here" read as either
  // "not synced by this institution" or "not synced by this app" — opposite claims. And it is
  // about products this app does not *request*, not data it lacks: `balance` and
  // `transactions_refresh` are on-demand re-pull products, while balances and transactions are
  // already mirrored from /accounts/get and /transactions/get.
  if (detail.syncable_products.length === 0) {
    hint.textContent = `${detail.name} offers no Plaid product this app knows how to sync.`;
  } else if (detail.unsupported_products.length > 0) {
    hint.textContent = `${detail.name} also offers Plaid products this app does not request: ${detail.unsupported_products.join(", ")}.`;
  } else {
    hint.textContent = "";
  }
  setFormEnabled();
  // withStatus leaves its in-progress message up unless the work replaces it, so without this the
  // form sits under "Loading institution products..." forever after a successful load.
  setStatus(`Selected ${detail.name}.`);
}

function setFormEnabled() {
  const products = selectedProducts();
  document.getElementById("connect").disabled = selectedInstitution === null || products.length === 0;
  document.getElementById("transaction-days-wrap").classList.toggle("hidden", !products.includes("transactions"));
}

async function refreshLinks() {
  const links = await apiFetch("/api/links");
  const tbody = document.getElementById("links");
  tbody.innerHTML = "";
  if (links.length === 0) {
    tbody.innerHTML = '<tr><td class="empty" colspan="4">No active Plaid links.</td></tr>';
    return;
  }
  for (const link of links) {
    const tr = document.createElement("tr");
    tr.dataset.item = link.item_id;
    if (link.institution_id) tr.dataset.institution = link.institution_id;
    tr.innerHTML = `
      <td>
        <div class="name">${escapeHtml(link.label || link.institution_name || link.item_id)}</div>
        <div class="muted">${escapeHtml(link.institution_name || "")}</div>
        <div class="meta muted">${escapeHtml(link.item_id)}</div>
        <div class="meta">Status: ${escapeHtml(link.status)}</div>
      </td>
      <td>
        <div>Requested ${pills(link.products_requested)}</div>
        <div class="meta">${historySummary(link)}</div>
        <div class="meta muted">Authorized ${pills(link.products_authorized)}</div>
        <div class="meta muted">Billed ${pills(link.products_billed)}</div>
      </td>
      <td>
        <div>${link.sync_running ? "sync in progress…" : escapeHtml(link.last_synced_at || "not synced yet")}</div>
        <div class="meta muted">Secret: ${escapeHtml(link.access_token_secret)}</div>
      </td>
      <td>
        <div class="actions">
          ${addProductsButton(link)}
          <button class="secondary" data-action="repair">Repair link</button>
          <button class="secondary" data-action="sync" ${link.sync_running ? "disabled" : ""}>${
            link.sync_running ? "Syncing…" : "Sync data"
          }</button>
          <button class="danger" data-action="remove">Remove link</button>
        </div>
      </td>`;
    tbody.appendChild(tr);
  }
}
async function exchangePublicToken(public_token, metadata, pending) {
  try {
    await apiFetch("/api/exchange-public-token", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        public_token,
        products: pending.products,
        transaction_days_requested: pending.transaction_days_requested || null,
        label: pending.label,
        // Prefer what Link reports; fall back to the typeahead selection, which is what the token
        // was minted for.
        institution_id: metadata.institution?.institution_id || pending.institution_id || null,
        institution_name: metadata.institution?.name || pending.institution_name || null,
      }),
    });
    setStatus("Link connected and synced.");
  } catch (error) {
    // The link row is persisted before the post-link sync runs, so a sync
    // failure still leaves a usable link — surface the error but keep the row.
    setStatus(`Link connected, but sync failed: ${error.message || error}`);
  } finally {
    sessionStorage.removeItem(pendingKey);
    await refreshLinks();
  }
}
async function completeUpdate(metadata, pending) {
  try {
    await apiFetch(`/api/links/${encodeURIComponent(pending.item_id)}/complete-update`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ products: pending.products, sync: true }),
    });
    setStatus(metadata?.institution?.name ? `Updated ${metadata.institution.name}.` : "Link updated and synced.");
  } catch (error) {
    setStatus(`Link updated, but sync failed: ${error.message || error}`);
  } finally {
    sessionStorage.removeItem(pendingKey);
    await refreshLinks();
  }
}
function openPlaid(pending, receivedRedirectUri) {
  const handler = Plaid.create({
    token: pending.link_token,
    receivedRedirectUri,
    onSuccess: async (public_token, metadata) => {
      if (pending.mode === "update") {
        await completeUpdate(metadata, pending);
      } else {
        await exchangePublicToken(public_token, metadata, pending);
      }
    },
    onExit: (error) => {
      if (error) setStatus(error.display_message || error.error_message || "Plaid Link exited with an error.");
    },
  });
  handler.open();
}
document.getElementById("links").addEventListener("click", async (event) => {
  const button = event.target.closest("button[data-action]");
  if (!button) return;
  const row = button.closest("tr[data-item]");
  const item = row?.dataset?.item;
  const action = button.dataset.action;
  if (!item || !action) return;
  if (action === "remove") {
    if (!window.confirm("Remove this Plaid link and delete its access-token Secret?")) return;
    await withStatus("Removing link...", async () => {
      await apiFetch(`/api/links/${encodeURIComponent(item)}/remove`, { method: "POST" });
      await refreshLinks();
      setStatus("Link removed.");
    });
    return;
  }
  if (action === "sync") {
    await withStatus("Syncing link...", async () => {
      const result = await apiFetch(`/api/links/${encodeURIComponent(item)}/sync`, { method: "POST" });
      await refreshLinks();
      setStatus(`Sync completed: ${result.run_id}`);
    });
    return;
  }
  // The button carries exactly the products it named, so consenting grants what the label said.
  const body =
    action === "update" ? { reason: "add_scope", products: button.dataset.addable.split(",") } : { reason: "repair" };
  await withStatus(
    action === "repair" ? "Opening Plaid repair flow..." : "Opening Plaid consent for the new products...",
    async () => {
      const token = await apiFetch(`/api/links/${encodeURIComponent(item)}/update-link-token`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(body),
      });
      const pending = {
        mode: "update",
        item_id: item,
        products: token.products,
        link_token: token.link_token,
      };
      sessionStorage.setItem(pendingKey, JSON.stringify(pending));
      openPlaid(pending);
    }
  );
});
document.getElementById("products").addEventListener("change", setFormEnabled);
document.getElementById("institution-results").addEventListener("click", async (event) => {
  const button = event.target.closest("button[data-institution]");
  if (button) await withStatus("Loading institution products...", () => selectInstitution(button.dataset.institution));
});
document.getElementById("institution").addEventListener("input", async (event) => {
  // Typing after a selection invalidates it; the availability shown belongs to the old institution.
  selectedInstitution = null;
  applyInstitutionSupport(null);
  document.getElementById("institution-hint").textContent = "";
  setFormEnabled();
  const query = event.target.value.trim();
  if (query.length < 2) {
    document.getElementById("institution-results").classList.add("hidden");
    return;
  }
  await searchInstitutions(query);
});
document.getElementById("link-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  await withStatus("Creating Plaid Link session...", async () => {
    const label = document.getElementById("label").value || null;
    const selected_products = selectedProducts();
    const transaction_days_requested = selected_products.includes("transactions")
      ? Number(document.getElementById("transaction-days").value)
      : null;
    const token = await apiFetch("/api/link-token", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ products: selected_products, transaction_days_requested }),
    });
    const pending = {
      mode: "new",
      products: token.products,
      institution_id: selectedInstitution.institution_id,
      institution_name: selectedInstitution.name,
      transaction_days_requested: token.transaction_days_requested,
      label,
      link_token: token.link_token,
    };
    sessionStorage.setItem(pendingKey, JSON.stringify(pending));
    openPlaid(pending);
  });
});
async function init() {
  await loadConfig();
  await refreshLinks();
  const pending = JSON.parse(sessionStorage.getItem(pendingKey) || "null");
  if (pending && new URLSearchParams(window.location.search).has("oauth_state_id")) {
    setStatus("Completing Plaid redirect...");
    openPlaid(pending, window.location.href);
  }
}
init().catch((error) => setStatus(error.message || String(error)));
