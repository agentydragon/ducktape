const pendingKey = "plaid-link-pending";
let webConfig = { transaction_days: 730, max_transaction_days: 730 };
// Profiles come from /api/config, which derives them from link_profiles.py. They used to be
// duplicated here by hand and could disagree with what the backend actually requested.
let profileInfo = [];
function profileLabel(p) {
  if (p.value === "advanced") return p.label + " — choose products";
  return p.label + " — " + p.products.join(" + ");
}
function profileProductsFor(value) {
  const found = profileInfo.find((p) => p.value === value);
  return found ? found.products : [];
}
const statusEl = document.getElementById("status");

function setStatus(message) {
  statusEl.textContent = message || "";
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
function profileSelect(link) {
  const current = link.link_profile || "cashflow";
  return `<select data-role="scope-profile">${profileInfo
    .filter((p) => p.value !== "advanced")
    .map(
      (p) =>
        `<option value="${p.value}" ${p.value === current ? "selected" : ""}>${escapeHtml(profileLabel(p))}</option>`
    )
    .join("")}</select>`;
}
function selectedProducts() {
  const profile = document.getElementById("profile").value;
  if (profile === "advanced") return advancedProducts();
  return profileProductsFor(profile);
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
    setStatus(error.message || String(error));
    throw error;
  } finally {
    document.querySelectorAll("button").forEach((button) => {
      button.disabled = false;
    });
  }
}
async function loadConfig() {
  webConfig = await apiFetch("/api/config");
  const input = document.getElementById("transaction-days");
  input.max = String(webConfig.max_transaction_days);
  input.value = String(webConfig.transaction_days);
  profileInfo = webConfig.profiles || [];
  const select = document.getElementById("profile");
  select.innerHTML = profileInfo
    .map((p) => `<option value="${p.value}">${escapeHtml(profileLabel(p))}</option>`)
    .join("");
  document.getElementById("profile-hint").textContent =
    "Plaid fails the whole Link if the institution does not support every product listed, " +
    "so a wider surface is more likely to fail, not more complete. Pick the narrowest that " +
    "covers what you need.";
  setAdvancedVisibility();
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
        <div>${escapeHtml(link.last_synced_at || "not synced yet")}</div>
        <div class="meta muted">Secret: ${escapeHtml(link.access_token_secret)}</div>
      </td>
      <td>
        <div class="actions">
          ${profileSelect(link)}
          <button class="secondary" data-action="update">Add scopes</button>
          <button class="secondary" data-action="repair">Repair</button>
          <button class="secondary" data-action="sync">Sync</button>
          <button class="danger" data-action="remove">Remove</button>
        </div>
      </td>`;
    tbody.appendChild(tr);
  }
}
function advancedProducts() {
  return Array.from(document.querySelectorAll("#advanced-products input:checked")).map((input) => input.value);
}
function setAdvancedVisibility() {
  const isAdvanced = document.getElementById("profile").value === "advanced";
  document.getElementById("advanced-products").classList.toggle("visible", isAdvanced);
  document
    .getElementById("transaction-days-wrap")
    .classList.toggle("hidden", !selectedProducts().includes("transactions"));
}
async function exchangePublicToken(public_token, metadata, pending) {
  try {
    await apiFetch("/api/exchange-public-token", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        public_token,
        profile: pending.profile,
        products: pending.products,
        transaction_days_requested: pending.transaction_days_requested || null,
        label: pending.label,
        institution_id: metadata.institution?.institution_id || null,
        institution_name: metadata.institution?.name || null,
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
      body: JSON.stringify({ profile: pending.profile, products: pending.products, sync: true }),
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
  const body =
    action === "repair"
      ? { reason: "repair" }
      : { reason: "add_scope", profile: row.querySelector('[data-role="scope-profile"]').value };
  await withStatus(
    action === "repair" ? "Opening Plaid repair flow..." : "Opening Plaid scope request...",
    async () => {
      const token = await apiFetch(`/api/links/${encodeURIComponent(item)}/update-link-token`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(body),
      });
      const pending = {
        mode: "update",
        item_id: item,
        profile: body.profile || null,
        products: token.products,
        link_token: token.link_token,
      };
      sessionStorage.setItem(pendingKey, JSON.stringify(pending));
      openPlaid(pending);
    }
  );
});
document.getElementById("profile").addEventListener("change", setAdvancedVisibility);
document.getElementById("advanced-products").addEventListener("change", setAdvancedVisibility);
document.getElementById("link-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  await withStatus("Creating Plaid Link session...", async () => {
    const profile = document.getElementById("profile").value;
    const label = document.getElementById("label").value || null;
    const advanced_products = profile === "advanced" ? advancedProducts() : null;
    const selected_products = selectedProducts();
    const transaction_days_requested = selected_products.includes("transactions")
      ? Number(document.getElementById("transaction-days").value)
      : null;
    const token = await apiFetch("/api/link-token", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ profile, advanced_products, transaction_days_requested }),
    });
    const pending = {
      mode: "new",
      profile,
      products: token.products,
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
  setAdvancedVisibility();
  await refreshLinks();
  const pending = JSON.parse(sessionStorage.getItem(pendingKey) || "null");
  if (pending && new URLSearchParams(window.location.search).has("oauth_state_id")) {
    setStatus("Completing Plaid redirect...");
    openPlaid(pending, window.location.href);
  }
}
init().catch((error) => setStatus(error.message || String(error)));
