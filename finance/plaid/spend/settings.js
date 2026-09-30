"use strict";

const cardsContainer = document.querySelector("#cards");
const form = document.querySelector("#cards-form");
const statusText = document.querySelector("#status");
const controls = [];

function field(labelText, input) {
  const label = document.createElement("label");
  label.append(document.createTextNode(labelText), input);
  return label;
}

function numberInput(value, maximum = null) {
  const input = document.createElement("input");
  input.type = "number";
  input.min = "1";
  input.step = "1";
  if (maximum !== null) input.max = String(maximum);
  input.value = value ?? "";
  return input;
}

function renderAccount(account, saved) {
  const card = document.createElement("fieldset");
  card.dataset.accountId = account.account_id;
  const legend = document.createElement("legend");
  const mask = account.mask ? ` •••• ${account.mask}` : "";
  legend.textContent = `${account.institution_name}: ${account.account_name}${mask} (${account.currency})`;
  card.append(legend);

  const grid = document.createElement("div");
  grid.className = "fields";
  const enabled = document.createElement("input");
  enabled.type = "checkbox";
  enabled.checked = saved?.enabled ?? false;
  const label = document.createElement("input");
  label.type = "text";
  label.maxLength = 80;
  label.required = true;
  label.value = saved?.label ?? account.account_name;
  const limit = numberInput(saved?.limit_minor_units);
  const threshold = numberInput(saved?.alert_threshold_percent, 100);

  const enabledLabel = document.createElement("label");
  enabledLabel.className = "enabled";
  enabledLabel.append(enabled, document.createTextNode(" Show this card"));
  grid.append(
    enabledLabel,
    field("Card label", label),
    field("Credit limit (minor units)", limit),
    field("Alert threshold (%)", threshold),
  );
  card.append(grid);
  cardsContainer.append(card);
  controls.push({ account, enabled, label, limit, threshold });
}

async function loadSettings() {
  const response = await fetch("/settings-data/state");
  if (!response.ok) throw new Error(`Could not load settings (${response.status})`);
  const state = await response.json();
  const savedCards = new Map(state.config.cards.map((card) => [card.account_id, card]));
  for (const account of state.accounts) renderAccount(account, savedCards.get(account.account_id));
  if (state.accounts.length === 0) statusText.textContent = "No active Plaid credit accounts are linked.";
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  statusText.textContent = "Saving…";
  const cards = controls.map(({ account, enabled, label, limit, threshold }) => ({
    account_id: account.account_id,
    label: label.value.trim(),
    limit_minor_units: limit.value === "" ? null : Number(limit.value),
    alert_threshold_percent: threshold.value === "" ? null : Number(threshold.value),
    enabled: enabled.checked,
  }));
  try {
    const response = await fetch("/settings-data/config", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ cards }),
    });
    if (!response.ok) throw new Error(`Could not save settings (${response.status})`);
    statusText.textContent = "Settings saved for your account.";
  } catch (error) {
    statusText.textContent = error instanceof Error ? error.message : "Could not save settings.";
  }
});

loadSettings().catch((error) => {
  statusText.textContent = error instanceof Error ? error.message : "Could not load settings.";
});
