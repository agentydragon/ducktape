const allowanceSection = document.querySelector("#allowance-section");
const allowanceSummary = document.querySelector("#allowance-summary");
const allowanceDetails = document.querySelector("#allowance-details");
const purchaseInput = document.querySelector("#purchase-amount");
const purchaseResult = document.querySelector("#purchase-result");
let currentAllowance = null;

function renderPurchase() {
  const a = currentAllowance;
  if (a?.status !== "active" || a.available_minor_units == null) {
    purchaseResult.textContent = "Activate the allowance and sync accounts before checking a purchase.";
    return;
  }
  const dollars = Number(purchaseInput.value);
  if (
    purchaseInput.value === "" ||
    !Number.isFinite(dollars) ||
    dollars < 0 ||
    !Number.isSafeInteger(Math.round(dollars * 100))
  ) {
    purchaseResult.textContent = "Enter a positive purchase amount.";
    return;
  }
  const left = a.available_minor_units - Math.round(dollars * 100);
  purchaseResult.textContent =
    `${formatMoney(left, a.currency)} after purchase. ` +
    (left < 0
      ? "Over the advisory allowance; make a conscious exception."
      : a.projected_cycle_end_minor_units - Math.round(dollars * 100) < 0
        ? "Current pace projects a shortfall before next credit."
        : "Within the allowance at current estimated pace.");
}
purchaseInput.addEventListener("input", renderPurchase);

function renderAllowance(a) {
  currentAllowance = a;
  allowanceSection.hidden = !a;
  if (!a) return;
  const money = (v) => formatMoney(v, a.currency);
  allowanceSummary.replaceChildren();
  allowanceDetails.replaceChildren();
  if (a.status !== "active") {
    allowanceSummary.textContent = `Unavailable: ${a.note || "Allowance data unavailable"}`;
    purchaseInput.disabled = true;
    renderPurchase();
    return;
  }
  purchaseInput.disabled = false;
  allowanceSummary.textContent = `${money(a.available_minor_units)} available · ${a.alert_state === "warning" ? "pace warning" : a.alert_state === "exceeded" ? "over allowance" : "on pace"}`;
  const items = [
    ["Monthly credit", money(a.monthly_minor_units)],
    ["Carry from earlier cycles", money(a.prior_carry_minor_units)],
    ["This credit cycle", money(a.windows_minor_units.current_credit_cycle_minor_units)],
    ["Pending (included)", money(a.pending_minor_units)],
    ["Posted (included)", money(a.posted_minor_units)],
    ["Needs classification review (included)", money(a.review_minor_units)],
    ["Unmatched refunds (excluded)", money(a.unmatched_refunds_minor_units)],
    ["Trailing 7 days", money(a.windows_minor_units.trailing_7_days_minor_units)],
    ["Trailing 30 days", money(a.windows_minor_units.trailing_30_days_minor_units)],
    ["Calendar month since activation", money(a.windows_minor_units.calendar_month_minor_units)],
    ["Year since activation", money(a.windows_minor_units.year_to_date_minor_units)],
    ["7-day daily pace", money(a.trailing_7_daily_minor_units)],
    [
      "Projected exhaustion at that pace, ignoring future credits",
      a.estimated_exhaustion_at ? formatTimestamp(a.estimated_exhaustion_at) : "No recent spend",
    ],
    ["Estimated balance before next credit", money(a.projected_cycle_end_minor_units)],
    ["Next credit", formatTimestamp(a.next_credit_at)],
    ["Oldest account sync", formatTimestamp(a.last_synced_at)],
  ];
  for (const [key, value] of items) {
    const row = document.createElement("p");
    row.textContent = `${key}: ${value}`;
    allowanceDetails.append(row);
  }
  renderPurchase();
}

const cardsNode = document.querySelector("#cards");
const countNode = document.querySelector("#card-count");
const combinedNode = document.querySelector("#combined-spend");
const generatedNode = document.querySelector("#generated-at");
const liveNode = document.querySelector("#live-state");

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function formatMoney(minorUnits, currency) {
  if (minorUnits == null || !Number.isFinite(Number(minorUnits))) return "Unavailable";
  const code = typeof currency === "string" && currency.length === 3 ? currency.toUpperCase() : "USD";
  try {
    const formatter = new Intl.NumberFormat(undefined, { style: "currency", currency: code });
    const digits = formatter.resolvedOptions().maximumFractionDigits;
    return formatter.format(Number(minorUnits) / 10 ** digits);
  } catch (_error) {
    return `${code} ${(Number(minorUnits) / 100).toFixed(2)}`;
  }
}

function formatTimestamp(value) {
  if (!value) return "Unknown";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "Unknown";
  return date.toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
}

function cardTitle(card) {
  const base = card.label || card.account_name || "Card";
  return card.mask ? `${base} ···· ${card.mask}` : base;
}

function alertLabel(card) {
  switch (card.alert_state) {
    case "warning":
      return card.alert_threshold_percent == null
        ? "Warning threshold reached"
        : `Warning · ${card.alert_threshold_percent}% threshold reached`;
    case "exceeded":
      return "Limit exceeded";
    case "unavailable":
      return "Alert state unavailable";
    default:
      return "No alert";
  }
}

function combinedSpend(view) {
  const cards = Array.isArray(view?.cards) ? view.cards : [];
  if (cards.length === 0) return "—";
  const available = cards.filter((card) => card.spend_minor_units != null);
  if (cards.length === 1) return formatMoney(cards[0].spend_minor_units, cards[0].currency);
  const currencies = new Set(available.map((card) => (card.currency || "USD").toUpperCase()));
  if (available.length > 0 && currencies.size === 1) {
    const total = available.reduce((sum, card) => sum + Number(card.spend_minor_units), 0);
    return `${available.length !== cards.length ? "~" : ""}${formatMoney(total, [...currencies][0])}`;
  }
  return `${cards.length} cards`;
}

function detail(label, value) {
  const row = element("div", "detail-row");
  row.append(element("span", "", label), element("span", "", value));
  return row;
}

function renderCard(card) {
  const panel = element("article", "card");
  const header = element("header", "card-header");
  const identity = element("div");
  identity.append(element("h3", "card-title", cardTitle(card)));
  if (card.institution_name) identity.append(element("p", "institution", card.institution_name));
  const alert = element("span", "alert", alertLabel(card));
  alert.dataset.state = card.alert_state || "normal";
  header.append(identity, alert);

  const spendBlock = element("div", "spend-block");
  spendBlock.append(element("p", "spend-label", "Spend this cycle"));
  const spend = element("p", "spend-amount");
  const spendValue = element("strong", "", formatMoney(card.spend_minor_units, card.currency));
  const limit =
    card.limit_minor_units == null ? "No limit set" : `/ ${formatMoney(card.limit_minor_units, card.currency)}`;
  spend.append(spendValue, element("span", "", limit));
  if (card.spend_percent != null) {
    spend.append(element("span", "", `${Number(card.spend_percent).toFixed(1)}%`));
  }
  spendBlock.append(spend);
  if (card.limit_minor_units != null && card.spend_percent != null) {
    const track = element("div", "progress-track");
    track.setAttribute("role", "progressbar");
    track.setAttribute("aria-label", `${cardTitle(card)} limit used`);
    track.setAttribute("aria-valuemin", "0");
    track.setAttribute("aria-valuemax", "100");
    track.setAttribute("aria-valuenow", String(Math.max(0, Number(card.spend_percent))));
    const fill = element("div", "progress-fill");
    fill.dataset.state = card.alert_state || "normal";
    fill.style.width = `${Math.min(100, Math.max(0, Number(card.spend_percent)))}%`;
    track.append(fill);
    spendBlock.append(track);
  }

  const details = element("div", "details");
  if (card.posted_minor_units != null || card.pending_minor_units != null) {
    details.append(
      detail("Posted", card.posted_minor_units == null ? "—" : formatMoney(card.posted_minor_units, card.currency))
    );
    details.append(
      detail("Pending", card.pending_minor_units == null ? "—" : formatMoney(card.pending_minor_units, card.currency))
    );
  }
  details.append(detail("Statement cycle", card.cycle_start ? `Starts ${card.cycle_start}` : "Unavailable"));
  const footer = element("footer", "card-footer");
  footer.append(element("span", "", "Last synced"), element("time", "", formatTimestamp(card.last_synced_at)));
  panel.append(header, spendBlock, details, footer);
  return panel;
}

function showView(view) {
  renderAllowance(view?.allowance);
  const cards = Array.isArray(view?.cards) ? view.cards : [];
  cardsNode.replaceChildren();
  countNode.textContent = `${cards.length} ${cards.length === 1 ? "card" : "cards"}`;
  combinedNode.textContent = combinedSpend(view);
  generatedNode.textContent = formatTimestamp(view?.generated_at);
  if (cards.length === 0) {
    cardsNode.append(element("p", "empty-state", "No card data is configured yet."));
  } else {
    cardsNode.append(...cards.map(renderCard));
  }
  cardsNode.setAttribute("aria-busy", "false");
}

let receivedView = false;

function setLiveState(state, label) {
  liveNode.dataset.state = state;
  liveNode.lastChild.textContent = label;
}

async function loadView() {
  try {
    const response = await fetch("/api/v1/web/view", { cache: "no-store", credentials: "same-origin" });
    if (response.status === 401) {
      window.location.assign("/auth/login");
      return;
    }
    if (!response.ok) throw new Error(`View request returned ${response.status}`);
    showView(await response.json());
  } catch (_error) {
    setLiveState("error", "Waiting for card data");
    cardsNode.setAttribute("aria-busy", "false");
    cardsNode.replaceChildren(
      element("p", "empty-state", "The card view could not be loaded. It will retry when live updates reconnect.")
    );
  }
}

const events = new EventSource("/api/v1/web/events");
events.addEventListener("view", (event) => {
  try {
    receivedView = true;
    showView(JSON.parse(event.data));
    setLiveState("live", "Live updates on");
  } catch (_error) {
    setLiveState("error", "Could not read update");
  }
});
events.onopen = () => {
  setLiveState(receivedView ? "live" : "connecting", receivedView ? "Live updates on" : "Connected");
};
events.onerror = async () => {
  setLiveState("connecting", "Reconnecting");
  try {
    const response = await fetch("/api/v1/web/view", { cache: "no-store", credentials: "same-origin" });
    if (response.status === 401) window.location.assign("/auth/login");
  } catch (_error) {
    // EventSource keeps retrying; its next view event will restore the live state.
  }
};

loadView();
