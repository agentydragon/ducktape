import Gio from "gi://Gio";
import GLib from "gi://GLib";
import GObject from "gi://GObject";
import St from "gi://St";
import Clutter from "gi://Clutter";

import { Extension } from "resource:///org/gnome/shell/extensions/extension.js";
import * as Main from "resource:///org/gnome/shell/ui/main.js";
import * as PanelMenu from "resource:///org/gnome/shell/ui/panelMenu.js";
import * as PopupMenu from "resource:///org/gnome/shell/ui/popupMenu.js";

const BUS_NAME = "works.allegedly.PlaidSpend";
const OBJECT_PATH = "/works/allegedly/PlaidSpend";
const INTERFACE_NAME = "works.allegedly.PlaidSpend1";
const TEST_START_NOW_MS = (() => {
  if (GLib.getenv("PLAID_SPEND_TEST") !== "1") return null;
  const value = GLib.getenv("PLAID_SPEND_TEST_NOW");
  if (value === null) return null;
  const timestamp = Date.parse(value);
  if (!Number.isFinite(timestamp) || new Date(timestamp).toISOString() !== value) {
    throw new Error("PLAID_SPEND_TEST_NOW must be a valid UTC timestamp in YYYY-MM-DDTHH:mm:ss.sssZ form");
  }
  return timestamp;
})();
let TEST_NOW_MS = TEST_START_NOW_MS;
const INTERFACE_INFO = Gio.DBusNodeInfo.new_for_xml(
  `<node><interface name="${INTERFACE_NAME}">
    <method name="GetView"><arg name="view" type="s" direction="out"/></method>
    <method name="GetPaceHistory"><arg name="history" type="s" direction="out"/></method>
    <method name="Login"/>
    <signal name="ViewChanged"><arg name="view" type="s"/></signal>
    <property name="Status" type="s" access="read"/>
    <property name="LastError" type="s" access="read"/>
    <property name="LastAttemptAt" type="d" access="read"/>
    <property name="NextRetryAt" type="d" access="read"/>
  </interface></node>`
).lookup_interface(INTERFACE_NAME);

function decodeBytes(bytes) {
  return new TextDecoder().decode(typeof bytes.get_data === "function" ? bytes.get_data() : bytes);
}

function unpackString(value, fallback = "") {
  try {
    return value?.deep_unpack() ?? fallback;
  } catch (_error) {
    return fallback;
  }
}

function unpackNumber(value, fallback = 0) {
  const unpacked = value?.deep_unpack();
  return typeof unpacked === "number" && Number.isFinite(unpacked) ? unpacked : fallback;
}

function relativeNowMs() {
  return TEST_NOW_MS ?? Date.now();
}

function formatMoney(minorUnits, currency) {
  if (minorUnits == null || !Number.isFinite(Number(minorUnits))) return "Unavailable";
  const code = typeof currency === "string" && currency.length === 3 ? currency.toUpperCase() : "USD";
  const value = Number(minorUnits);
  try {
    const precision = new Intl.NumberFormat(undefined, { style: "currency", currency: code }).resolvedOptions()
      .maximumFractionDigits;
    const amount = value / 10 ** (precision ?? 2);
    const digits = amount !== 0 && Math.abs(amount) < 10 && !Number.isInteger(amount) ? (precision ?? 2) : 0;
    const formatter = new Intl.NumberFormat(undefined, {
      style: "currency",
      currency: code,
      maximumFractionDigits: digits,
      minimumFractionDigits: digits,
    });
    if (amount !== 0 && Math.abs(amount) < 0.5 / 10 ** (precision ?? 2)) {
      return `${amount < 0 ? "-" : ""}<${formatter.format(1 / 10 ** (precision ?? 2))}`;
    }
    return formatter.format(amount);
  } catch (_error) {
    const amount = value / 100;
    if (amount !== 0 && Math.abs(amount) < 0.005) return `${amount < 0 ? "-" : ""}<${code} 0.01`;
    return `${code} ${amount.toFixed(amount !== 0 && Math.abs(amount) < 10 && !Number.isInteger(amount) ? 2 : 0)}`;
  }
}

function formatCompactMoney(minorUnits, currency) {
  if (minorUnits == null || !Number.isFinite(Number(minorUnits))) return "—";
  try {
    const code = typeof currency === "string" && currency.length === 3 ? currency.toUpperCase() : "USD";
    const precision = new Intl.NumberFormat(undefined, { style: "currency", currency: code }).resolvedOptions()
      .maximumFractionDigits;
    const amount = Number(minorUnits) / 10 ** (precision ?? 2);
    if (Math.abs(amount) < 1000) return formatMoney(minorUnits, currency);
    return new Intl.NumberFormat(undefined, {
      style: "currency",
      currency: code,
      notation: "compact",
      maximumFractionDigits: 1,
      minimumFractionDigits: 0,
    })
      .format(amount)
      .replace(/K$/, "k");
  } catch (_error) {
    return formatMoney(minorUnits, currency);
  }
}

function formatExactMoney(minorUnits, currency) {
  if (minorUnits == null || !Number.isFinite(Number(minorUnits))) return "Unavailable";
  try {
    const code = typeof currency === "string" && currency.length === 3 ? currency.toUpperCase() : "USD";
    const formatter = new Intl.NumberFormat(undefined, { style: "currency", currency: code });
    return formatter.format(Number(minorUnits) / 10 ** (formatter.resolvedOptions().maximumFractionDigits ?? 2));
  } catch (_error) {
    return formatMoney(minorUnits, currency);
  }
}

function needsExactMoneyDetail(minorUnits, currency) {
  if (minorUnits == null || !Number.isFinite(Number(minorUnits))) return false;
  const code = typeof currency === "string" && currency.length === 3 ? currency.toUpperCase() : "USD";
  try {
    const precision = new Intl.NumberFormat(undefined, { style: "currency", currency: code }).resolvedOptions()
      .maximumFractionDigits;
    const scale = 10 ** (precision ?? 2);
    const value = Number(minorUnits);
    return Math.abs(value) >= 10 * scale && value % scale !== 0;
  } catch (_error) {
    return false;
  }
}

function formatTimestamp(value, now = new Date()) {
  if (!value) return "Unknown";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "Unknown";
  const label = date.toLocaleDateString("en-US", {
    month: "short",
    day: "numeric",
    ...(date.getFullYear() !== now.getFullYear() ? { year: "numeric" } : {}),
  });
  const clock = date.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
  return Math.abs(date.getTime() - now.getTime()) <= 14 * 86400_000 ? `${label}, ${clock}` : label;
}

function cardTitle(card) {
  const base = card.label || card.account_name || "Card";
  return card.mask ? `${base} ···· ${card.mask}` : base;
}

function periodReport(allowance, field, periodId) {
  const reports = allowance?.[field];
  return Array.isArray(reports) ? reports.find((report) => report?.period?.id === periodId) : null;
}

function cardAlert(card) {
  switch (card.alert_state) {
    case "warning":
      return card.alert_threshold_percent == null
        ? "Warning threshold reached"
        : `Warning · ${card.alert_threshold_percent}% threshold reached`;
    case "exceeded":
      return "Limit exceeded";
    case "unavailable":
      return card.statement_period?.kind === "provisional"
        ? "First statement not yet reported"
        : "Alert state unavailable";
    case "normal":
    default:
      return "No alert";
  }
}

function totalLabel(view) {
  const cards = Array.isArray(view?.cards) ? view.cards : [];
  if (cards.length === 0) return "—";
  const available = cards.filter(
    (card) => card.spend_minor_units != null && Number.isFinite(Number(card.spend_minor_units))
  );
  if (cards.length === 1) return formatMoney(cards[0].spend_minor_units, cards[0].currency);
  // Do not add a provisional since-first-transaction total to statement-cycle totals.
  if (cards.some((card) => card.statement_period?.kind !== "statement")) return `${cards.length} cards`;

  const currencies = new Set(available.map((card) => (card.currency || "USD").toUpperCase()));
  if (available.length > 0 && currencies.size === 1) {
    const total = available.reduce((sum, card) => sum + Number(card.spend_minor_units), 0);
    const approximate = available.length !== cards.length ? "~" : "";
    return `${approximate}${formatMoney(total, [...currencies][0])}`;
  }
  return `${cards.length} cards`;
}

function selectedPace(allowance, window) {
  const periodId = window === "30d" ? "rolling_30d" : "rolling_7d";
  const amount = periodReport(allowance, "recorded_pace_periods", periodId)?.observed_daily_minor_units;
  const reference = allowance?.daily_reference_minor_units;
  if (amount == null || reference == null || !Number.isFinite(amount) || !Number.isFinite(reference) || reference <= 0)
    return null;
  return { amount, reference, ratio: amount / reference };
}

function panelSignal(allowance, window) {
  if (!allowance || allowance.status !== "active" || allowance.available_minor_units == null)
    return { level: "unknown", label: "Unavailable" };
  if (allowance.available_minor_units <= 0) return { level: "danger", label: "Allowance exhausted" };
  const pace = selectedPace(allowance, window);
  if (!pace) return { level: "unknown", label: "Pace unavailable" };
  if (pace.ratio > 1.2) return { level: "danger", label: "Pace over 120% of leash" };
  if (pace.ratio > 1) return { level: "caution", label: "Pace above leash" };
  return { level: "good", label: "Pace ≤ leash" };
}

const PANEL_ITEMS = ["status", "balance", "pace", "sparkline"];

function panelOrder(settings) {
  return [...new Set([...settings.get_strv("panel-order").filter((id) => PANEL_ITEMS.includes(id)), ...PANEL_ITEMS])];
}

function formatDaily(minorUnits, currency) {
  return minorUnits == null ? "n/a" : `${formatMoney(minorUnits, currency)}/day`;
}

function formatSyncAge(timestamp) {
  const elapsed = (relativeNowMs() - new Date(timestamp).getTime()) / 60000;
  if (!Number.isFinite(elapsed)) return "?";
  if (elapsed < 60) return `${Math.max(0, Math.floor(elapsed))}m`;
  if (elapsed < 2880) return `${Math.floor(elapsed / 60)}h`;
  return `${Math.floor(elapsed / 1440)}d`;
}

function formatDuration(seconds) {
  const total = Math.max(0, Math.floor(seconds));
  if (total < 60) return `${total}s`;
  if (total < 3600) return `${Math.floor(total / 60)}m ${total % 60}s`;
  if (total < 86400) return `${Math.floor(total / 3600)}h ${Math.floor((total % 3600) / 60)}m`;
  return `${Math.floor(total / 86400)}d ${Math.floor((total % 86400) / 3600)}h`;
}

function desktopTimingLabel(status, lastAttemptAt, nextRetryAt) {
  const now = relativeNowMs() / 1000;
  const attempt = lastAttemptAt > 0 ? `Last attempt ${formatDuration(now - lastAttemptAt)} ago` : "No desktop attempt";
  if (nextRetryAt > 0) return `${attempt} · Retry in ${formatDuration(nextRetryAt - now)}`;
  if (status === "authentication-required") return lastAttemptAt > 0 ? attempt : null;
  if (status === "authorizing")
    return lastAttemptAt > 0 ? `${attempt} · Signing in` : "Signing in · no automatic retry";
  if (lastAttemptAt <= 0) return null;
  if (status === "ready") return `${attempt} · Connected`;
  if (status === "connecting") return `${attempt} · Connecting`;
  return `${attempt} · No retry scheduled`;
}

const PlaidSpendIndicator = GObject.registerClass(
  class PlaidSpendIndicator extends PanelMenu.Button {
    _init(settings, openPreferences) {
      super._init(0.0, "Plaid Spend", false);

      this._settings = settings;
      this._openPreferences = openPreferences;
      this._destroyed = false;
      this._view = { cards: [], generated_at: null };
      this._status = "starting";
      this._error = "";
      this._lastAttemptAt = 0;
      this._nextRetryAt = 0;
      this._hasView = false;
      this._viewRequestId = 0;
      this._viewRequestCancellable = null;
      this._historyRequestId = 0;
      this._historyRequestCancellable = null;
      this._history = null;
      this._historyError = false;
      this._proxyHandlerIds = [];
      this._daemonConnection = null;
      this._daemonConnectCancellable = new Gio.Cancellable();
      const fixturePath = GLib.getenv("PLAID_SPEND_FIXTURE");
      this._fixtureMode = Boolean(fixturePath);
      this._testMode = GLib.getenv("PLAID_SPEND_TEST") === "1" || Boolean(fixturePath);
      this._testIface = null;
      this._testBusOwnerId = 0;
      this._timingItem = null;
      this._timingRefreshId = 0;

      this._box = new St.BoxLayout({ y_align: Clutter.ActorAlign.CENTER, style_class: "plaid-spend-panel" });
      this._icon = new St.Icon({ icon_name: "credit-card-symbolic", style_class: "system-status-icon" });
      this._label = new St.Label({ text: "Loading…", y_align: Clutter.ActorAlign.CENTER });
      this._paceLabel = new St.Label({ y_align: Clutter.ActorAlign.CENTER });
      this._sparklineUnavailable = new St.Label({ text: "—", y_align: Clutter.ActorAlign.CENTER });
      this._sparkline = new St.DrawingArea({ width: 60, height: 16, y_align: Clutter.ActorAlign.CENTER });
      this._sparklineColors = Object.fromEntries(
        ["good", "caution", "danger"].map((level) => {
          const actor = new St.Widget({ visible: false, style_class: `plaid-spend-${level}` });
          this._sparkline.add_child(actor);
          return [level, actor];
        })
      );
      this._sparkline.connect("repaint", (area) => this._drawSparkline(area));
      this._panelText = "Loading…";
      this.add_child(this._box);
      this._sparklineEnabled = this._settings.get_strv("panel-visible").includes("sparkline");
      this._settingsChangedId = this._settings.connect("changed", (_settings, key) => {
        if (key === "panel-visible") {
          const enabled = this._settings.get_strv("panel-visible").includes("sparkline");
          if (enabled !== this._sparklineEnabled) {
            this._sparklineEnabled = enabled;
            this._fetchHistory();
          }
        }
        this._render();
      });

      this._menuOpenId = this.menu.connect("open-state-changed", (_menu, open) => {
        if (open) {
          this._renderPopup();
          this._startTimingRefresh();
        } else this._stopTimingRefresh();
      });

      if (this._testMode) this._exportTestInterface();
      if (fixturePath) {
        this._loadFixtureFile(fixturePath);
        return;
      }

      const hostBusAddress = GLib.getenv("PLAID_SPEND_DEVKIT_HOST_BUS_ADDRESS");
      if (hostBusAddress) {
        Gio.DBusConnection.new_for_address(
          hostBusAddress,
          Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
          null,
          this._daemonConnectCancellable,
          (_source, result) => {
            try {
              const connection = Gio.DBusConnection.new_for_address_finish(result);
              if (this._destroyed) {
                connection.close(null, null);
                return;
              }
              connection.set_exit_on_close(false);
              this._daemonConnection = connection;
              connection.connect("closed", () => {
                if (this._destroyed) return;
                this._status = "error";
                this._error = "Host session bus disconnected";
                this._render();
              });
              Gio.DBusProxy.new(
                connection,
                Gio.DBusProxyFlags.NONE,
                INTERFACE_INFO,
                BUS_NAME,
                OBJECT_PATH,
                INTERFACE_NAME,
                this._daemonConnectCancellable,
                (_source, proxyResult) => this._proxyReady(proxyResult, false)
              );
            } catch (error) {
              this._daemonConnectFailed(error);
            }
          }
        );
      } else {
        Gio.DBusProxy.new_for_bus(
          Gio.BusType.SESSION,
          Gio.DBusProxyFlags.NONE,
          INTERFACE_INFO,
          BUS_NAME,
          OBJECT_PATH,
          INTERFACE_NAME,
          this._daemonConnectCancellable,
          (_source, result) => this._proxyReady(result, true)
        );
      }
      this._render();
    }

    _daemonConnectFailed(error) {
      if (this._destroyed) return;
      this._error = error.message;
      this._status = "error";
      this._render();
    }

    _proxyReady(result, sessionBus) {
      try {
        const proxy = sessionBus ? Gio.DBusProxy.new_for_bus_finish(result) : Gio.DBusProxy.new_finish(result);
        if (this._destroyed) return;
        this._proxy = proxy;
        this._proxyHandlerIds.push(
          this._proxy.connect("g-signal", (proxy, sender, name, parameters) => {
            if (name !== "ViewChanged") return;
            if (sender !== proxy.g_name_owner) return;
            const [rawView] = parameters.deep_unpack();
            this._acceptView(rawView);
          })
        );
        this._proxyHandlerIds.push(this._proxy.connect("g-properties-changed", () => this._readProperties()));
        this._proxyHandlerIds.push(
          this._proxy.connect("notify::g-name-owner", () => {
            this._cancelViewRequest();
            this._cancelHistoryRequest();
            this._history = null;
            this._readProperties();
            if (this._proxy.g_name_owner) this._fetchView();
          })
        );
        this._readProperties();
        if (this._proxy.g_name_owner) this._fetchView();
        this._render();
      } catch (error) {
        this._daemonConnectFailed(error);
      }
    }

    _readProperties() {
      if (!this._proxy) return;
      const previousStatus = this._status;
      this._status = unpackString(this._proxy.get_cached_property("Status"), this._status);
      this._error = unpackString(this._proxy.get_cached_property("LastError"), this._error);
      if (!this._proxy.g_name_owner) {
        this._lastAttemptAt = 0;
        this._nextRetryAt = 0;
      } else {
        this._lastAttemptAt = unpackNumber(this._proxy.get_cached_property("LastAttemptAt"));
        this._nextRetryAt = unpackNumber(this._proxy.get_cached_property("NextRetryAt"));
      }
      if (previousStatus !== "ready" && this._status === "ready") this._fetchHistory();
      this._render();
    }

    _startTimingRefresh() {
      if (this._timingRefreshId) return;
      this._timingRefreshId = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, 1, () => {
        if (this._destroyed) {
          this._timingRefreshId = 0;
          return GLib.SOURCE_REMOVE;
        }
        this._updateTimingItem();
        return GLib.SOURCE_CONTINUE;
      });
    }

    _stopTimingRefresh() {
      if (!this._timingRefreshId) return;
      GLib.source_remove(this._timingRefreshId);
      this._timingRefreshId = 0;
    }

    _updateTimingItem() {
      if (!this._timingItem) return;
      const text = desktopTimingLabel(this._status, this._lastAttemptAt, this._nextRetryAt);
      if (text) this._timingItem.label.set_text(text);
    }

    _fetchView() {
      const proxy = this._proxy;
      const owner = proxy?.g_name_owner;
      if (!owner || this._destroyed) return;
      this._cancelViewRequest();
      const requestId = ++this._viewRequestId;
      const cancellable = new Gio.Cancellable();
      this._viewRequestCancellable = cancellable;
      proxy.call("GetView", null, Gio.DBusCallFlags.NONE, -1, cancellable, (source, result) => {
        const isCurrent = () =>
          !this._destroyed &&
          source === proxy &&
          this._proxy === proxy &&
          owner === proxy.g_name_owner &&
          requestId === this._viewRequestId;
        try {
          const reply = source.call_finish(result);
          if (!isCurrent()) return;
          this._viewRequestCancellable = null;
          const [rawView] = reply.deep_unpack();
          this._acceptView(rawView);
        } catch (error) {
          if (!isCurrent()) return;
          this._viewRequestCancellable = null;
          this._error = error.message;
          this._render();
        }
      });
    }

    _cancelViewRequest() {
      this._viewRequestId += 1;
      if (this._viewRequestCancellable) {
        this._viewRequestCancellable.cancel();
        this._viewRequestCancellable = null;
      }
    }

    _cancelHistoryRequest() {
      this._historyRequestId += 1;
      if (this._historyRequestCancellable) {
        this._historyRequestCancellable.cancel();
        this._historyRequestCancellable = null;
      }
    }

    _fetchHistory() {
      if (this._fixtureMode) return;
      this._cancelHistoryRequest();
      this._history = null;
      this._historyError = false;
      if (!this._settings.get_strv("panel-visible").includes("sparkline")) return;
      const proxy = this._proxy;
      const owner = proxy?.g_name_owner;
      if (!owner || this._destroyed || !this._hasView || this._status !== "ready") return;
      const requestId = ++this._historyRequestId;
      const cancellable = new Gio.Cancellable();
      this._historyRequestCancellable = cancellable;
      proxy.call("GetPaceHistory", null, Gio.DBusCallFlags.NONE, -1, cancellable, (source, result) => {
        const isCurrent = () =>
          !this._destroyed &&
          source === proxy &&
          this._proxy === proxy &&
          owner === proxy.g_name_owner &&
          requestId === this._historyRequestId;
        try {
          const reply = source.call_finish(result);
          if (!isCurrent()) return;
          this._historyRequestCancellable = null;
          const [raw] = reply.deep_unpack();
          const history = JSON.parse(raw);
          if (!history || history.status !== "active" || !Array.isArray(history.windows))
            throw new Error("Pace history unavailable");
          this._history = history;
          this._historyError = false;
          this._render();
        } catch (_error) {
          if (!isCurrent()) return;
          this._historyRequestCancellable = null;
          this._history = null;
          this._historyError = true;
          this._render();
        }
      });
    }

    _historyPoints() {
      const periodId = this._settings.get_string("pace-window") === "30d" ? "rolling_30d" : "rolling_7d";
      const windows = Array.isArray(this._history?.windows) ? this._history.windows : [];
      const window = windows.find((candidate) => candidate?.period_id === periodId);
      return Array.isArray(window?.points)
        ? window.points.slice(-Number(this._settings.get_string("sparkline-history-days")))
        : [];
    }

    _drawSparkline(area) {
      if (!area.get_stage()) return;
      const cr = area.get_context();
      try {
        const points = this._historyPoints();
        const reference = this._history?.daily_reference_minor_units;
        const values = points.map((point) => point?.observed_daily_minor_units);
        const present = values.filter((value) => typeof value === "number" && Number.isFinite(value) && value >= 0);
        if (present.length < 2 || !Number.isFinite(reference) || reference <= 0) return;
        const [width, height] = area.get_surface_size();
        const pad = 2;
        const showLeash = this._settings.get_boolean("sparkline-show-leash");
        let min = this._settings.get_boolean("sparkline-anchor-zero")
          ? 0
          : Math.min(...present, ...(showLeash ? [reference] : []));
        let max = Math.max(...present, ...(showLeash ? [reference * 1.25] : []));
        if (max <= min) {
          const padding = Math.max(1, max * 0.05);
          if (min > 0) min = Math.max(0, min - padding);
          max += padding;
        }
        const x = (index) => pad + (index * Math.max(0, width - 2 * pad)) / Math.max(1, values.length - 1);
        const y = (value) => height - pad - ((value - min) / (max - min)) * Math.max(0, height - 2 * pad);
        if (showLeash) {
          const customColor = /^#([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i.exec(
            this._settings.get_string("sparkline-leash-color")
          );
          const color = this.get_theme_node().get_foreground_color();
          const [red, green, blue] = customColor
            ? customColor.slice(1).map((component) => parseInt(component, 16))
            : [color.red, color.green, color.blue];
          cr.setSourceRGBA(red / 255, green / 255, blue / 255, 0.85);
          cr.setLineWidth(1);
          cr.setDash([2, 2], 0);
          cr.moveTo(pad, y(reference));
          cr.lineTo(width - pad, y(reference));
          cr.stroke();
          cr.setDash([], 0);
        }
        cr.setLineWidth(1.5);
        const palette = Object.fromEntries(
          Object.entries(this._sparklineColors).map(([level, actor]) => [
            level,
            actor.get_theme_node().get_foreground_color(),
          ])
        );
        let previous = null;
        values.forEach((value, index) => {
          if (typeof value !== "number" || !Number.isFinite(value) || value < 0) {
            previous = null;
            return;
          }
          if (previous !== null) {
            const [previousIndex, previousValue] = previous;
            const delta = value - previousValue;
            const breaks = [0, 1];
            if (delta !== 0) {
              for (const threshold of [reference, reference * 1.2]) {
                const at = (threshold - previousValue) / delta;
                if (at > 0 && at < 1) breaks.push(at);
              }
            }
            breaks.sort((a, b) => a - b);
            for (let i = 1; i < breaks.length; i++) {
              const start = breaks[i - 1];
              const end = breaks[i];
              const midpoint = previousValue + (delta * (start + end)) / 2;
              const level = midpoint > reference * 1.2 ? "danger" : midpoint > reference ? "caution" : "good";
              const segmentColor = palette[level];
              cr.setSourceRGBA(segmentColor.red / 255, segmentColor.green / 255, segmentColor.blue / 255, 1);
              cr.moveTo(x(previousIndex + start), y(previousValue + delta * start));
              cr.lineTo(x(previousIndex + end), y(previousValue + delta * end));
              cr.stroke();
            }
          }
          previous = [index, value];
        });
      } finally {
        cr.$dispose();
      }
    }

    _acceptView(rawView) {
      try {
        const view = JSON.parse(rawView);
        if (!view || !Array.isArray(view.cards)) throw new Error("invalid Plaid Spend view");
        this._cancelViewRequest();
        this._view = view;
        this._hasView = true;
        this._fetchHistory();
        this._render();
      } catch (error) {
        this._error = error.message;
        this._status = "error";
        this._render();
      }
    }

    _render() {
      if (this._destroyed) return;
      const cards = Array.isArray(this._view?.cards) ? this._view.cards : [];
      const allowance = this._view?.allowance;
      const window = this._settings.get_string("pace-window");
      const ready = this._status === "ready" && allowance?.status === "active";
      let signal = ready ? panelSignal(allowance, window) : { level: "unknown", label: "Unavailable" };
      if (this._status === "ready" && !allowance && cards.some((card) => card.alert_state === "exceeded"))
        signal = { level: "danger", label: "Card limit exceeded" };
      else if (this._status === "ready" && !allowance && cards.some((card) => card.alert_state === "warning"))
        signal = { level: "caution", label: "Card warning threshold reached" };
      const pace = ready ? selectedPace(allowance, window) : null;
      const visible = new Set(this._settings.get_strv("panel-visible"));
      const labels = [];
      let fallback = null;
      if (this._status === "authentication-required") fallback = "Sign in";
      else if (this._status === "authorizing") fallback = "Signing in…";
      else if (!this._hasView && this._status === "error") fallback = "Offline";
      else if (!this._hasView && this._status !== "ready") fallback = "Connecting…";
      else if (this._status !== "ready") fallback = "Offline";
      else if (allowance && !ready) fallback = "Unavailable";
      else if (!allowance && cards.length === 0) fallback = "No cards";
      else if (!allowance) fallback = totalLabel(this._view);

      this._icon.icon_name = {
        good: "object-select-symbolic",
        caution: "dialog-warning-symbolic",
        danger: "dialog-error-symbolic",
        unknown: "dialog-question-symbolic",
      }[signal.level];
      for (const actor of [this._icon, this._label, this._paceLabel, this._sparkline, this._sparklineUnavailable]) {
        for (const level of ["good", "caution", "danger", "unknown"])
          actor.remove_style_class_name(`plaid-spend-${level}`);
        actor.add_style_class_name(`plaid-spend-${signal.level}`);
      }
      this._label.set_text(fallback ?? formatCompactMoney(allowance?.available_minor_units, allowance?.currency));
      this._paceLabel.set_text(`${window} ${pace ? `${Math.round(pace.ratio * 100)}%` : "—"}`);
      for (const child of this._box.get_children()) this._box.remove_child(child);
      if (fallback !== null) {
        if (visible.has("status")) this._box.add_child(this._icon);
        this._box.add_child(this._label);
        labels.push(fallback);
      } else {
        const points = this._historyPoints();
        const count = points.filter((point) => typeof point?.observed_daily_minor_units === "number").length;
        const actors = {
          status: this._icon,
          balance: this._label,
          pace: this._paceLabel,
          sparkline: count >= 2 ? this._sparkline : this._sparklineUnavailable,
        };
        for (const id of panelOrder(this._settings)) {
          if (!visible.has(id)) continue;
          this._box.add_child(actors[id]);
          if (id === "balance") labels.push(this._label.get_text());
          if (id === "pace") labels.push(this._paceLabel.get_text());
        }
        // An empty selection still leaves a way to open the menu and preferences.
        if (this._box.get_n_children() === 0) this._box.add_child(this._icon);
      }
      if (this._sparkline.get_stage()) this._sparkline.queue_repaint();
      this._panelText = labels.join(" ");
      this._panelLevel = signal.level;
      this.accessible_name = ready
        ? `Plaid Spend: ${signal.label}; ${formatExactMoney(allowance.available_minor_units, allowance.currency)} available; ${window} pace ${pace ? `${Math.round(pace.ratio * 100)}% of leash` : "unavailable"}`
        : `Plaid Spend: ${fallback ?? signal.label}`;
      this._renderPopup();
    }

    _loadFixtureFile(path) {
      if (!this._testMode) throw new Error("fixture mode is disabled");
      const [ok, contents] = GLib.file_get_contents(path);
      if (!ok) throw new Error(`fixture not readable: ${path}`);
      const fixture = JSON.parse(decodeBytes(contents));
      if (!fixture.view || !Array.isArray(fixture.view.cards)) {
        throw new Error("fixture must contain a view with a cards array");
      }
      this._status = fixture.status || "ready";
      this._error = fixture.error || "";
      TEST_NOW_MS = TEST_START_NOW_MS;
      const now = relativeNowMs() / 1000;
      this._lastAttemptAt =
        fixture.last_attempt_seconds_ago == null
          ? typeof fixture.last_attempt_at === "number" && Number.isFinite(fixture.last_attempt_at)
            ? fixture.last_attempt_at
            : 0
          : now - Number(fixture.last_attempt_seconds_ago);
      this._nextRetryAt =
        fixture.retry_in_seconds == null
          ? typeof fixture.next_retry_at === "number" && Number.isFinite(fixture.next_retry_at)
            ? fixture.next_retry_at
            : 0
          : now + Number(fixture.retry_in_seconds);
      this._view = fixture.view;
      this._history = fixture.pace_history ?? null;
      this._historyError = fixture.history_error === true;
      this._hasView = fixture.has_view !== false;
      this._render();
    }

    _exportTestInterface() {
      // Only exported in fixture mode. It lets the Bazel screenshot test
      // switch synthetic views and inspect the real panel/menu actors.
      this._testIface = Gio.DBusExportedObject.wrapJSObject(
        '<node><interface name="works.allegedly.PlaidSpendTest">' +
          '<method name="Reload"><arg type="s" direction="in" name="path"/></method>' +
          '<method name="AdvanceClock"><arg type="d" direction="in" name="seconds"/></method>' +
          '<method name="OpenMenu"/>' +
          '<method name="OpenPreferences"/>' +
          '<method name="HideOverview"/>' +
          '<method name="GetOverviewVisible"><arg type="b" direction="out" name="visible"/></method>' +
          '<method name="WaitForPaint"/>' +
          '<method name="CloseMenu"/>' +
          '<method name="GetPanelLabel"><arg type="s" direction="out" name="label"/></method>' +
          '<method name="GetPanelState"><arg type="s" direction="out" name="state"/></method>' +
          '<method name="SetPanelSettings"><arg type="s" direction="in" name="settings"/></method>' +
          '<method name="GetDaemonOwner"><arg type="s" direction="out" name="owner"/></method>' +
          '<method name="GetMenuText"><arg type="s" direction="out" name="text"/></method>' +
          '<method name="GetBalanceDetails"><arg type="s" direction="out" name="text"/></method>' +
          '<method name="OpenBalanceDetails"/>' +
          '<method name="GetMenuGeometry"><arg type="(iiii)" direction="out" name="rect"/></method>' +
          '<method name="GetSparklineGeometry"><arg type="(iiii)" direction="out" name="rect"/></method>' +
          "</interface></node>",
        {
          Reload: (path) => this._loadFixtureFile(path),
          AdvanceClock: (seconds) => {
            if (TEST_NOW_MS === null || !Number.isFinite(seconds) || seconds <= 0) {
              throw new Error("test clock can only advance by a positive number of seconds");
            }
            TEST_NOW_MS += seconds * 1000;
            this._renderPopup();
          },
          WaitForPaintAsync: (_params, invocation) => {
            const signal = global.stage.connect("after-paint", () => {
              global.stage.disconnect(signal);
              GLib.idle_add(GLib.PRIORITY_DEFAULT_IDLE, () => {
                invocation.return_value(null);
                return GLib.SOURCE_REMOVE;
              });
            });
            global.stage.queue_redraw();
          },
          OpenMenu: () => this.menu.open(false),
          OpenPreferences: () => this._openPreferences(),
          HideOverview: () => Main.overview.hide(),
          GetOverviewVisible: () => Main.overview.visible,
          CloseMenu: () => this.menu.close(false),
          GetPanelLabel: () => this._panelText,
          GetPanelState: () => {
            const actors = new Map([
              [this._icon, "status"],
              [this._label, "balance"],
              [this._paceLabel, "pace"],
              [this._sparkline, "sparkline"],
              [this._sparklineUnavailable, "sparkline-unavailable"],
            ]);
            return JSON.stringify({
              items: this._box.get_children().map((actor) => actors.get(actor)),
              level: this._panelLevel,
              window: this._settings.get_string("pace-window"),
            });
          },
          SetPanelSettings: (raw) => {
            const config = JSON.parse(raw);
            if (config.order) this._settings.set_strv("panel-order", config.order);
            if (config.visible) this._settings.set_strv("panel-visible", config.visible);
            if (config.window) this._settings.set_string("pace-window", config.window);
            if ("anchorZero" in config) this._settings.set_boolean("sparkline-anchor-zero", config.anchorZero);
            if ("showLeash" in config) this._settings.set_boolean("sparkline-show-leash", config.showLeash);
            if ("leashColor" in config) this._settings.set_string("sparkline-leash-color", config.leashColor);
            if (config.historyDays) this._settings.set_string("sparkline-history-days", String(config.historyDays));
          },
          GetDaemonOwner: () => this._proxy?.g_name_owner || "",
          GetMenuText: () =>
            this.menu
              ._getMenuItems()
              .map((item) => item.label?.get_text() || "")
              .join("\n"),
          GetBalanceDetails: () => {
            const item = this.menu._getMenuItems().find((entry) => entry.label?.get_text() === "Balance details");
            return (
              item?.menu
                ?._getMenuItems()
                .map((entry) => entry.label?.get_text() || "")
                .join("\n") ?? ""
            );
          },
          OpenBalanceDetails: () => {
            const item = this.menu._getMenuItems().find((entry) => entry.label?.get_text() === "Balance details");
            item?.menu?.open(false);
          },
          GetMenuGeometry: () => {
            const actor = this.menu.actor;
            const [x, y] = actor.get_transformed_position();
            const [width, height] = actor.get_transformed_size();
            return [Math.round(x), Math.round(y), Math.round(width), Math.round(height)];
          },
          GetSparklineGeometry: () => {
            const [x, y] = this._sparkline.get_transformed_position();
            const [width, height] = this._sparkline.get_transformed_size();
            return [Math.round(x), Math.round(y), Math.round(width), Math.round(height)];
          },
        }
      );
      this._testIface.export(Gio.DBus.session, "/works/allegedly/PlaidSpendTest");
      this._testBusOwnerId = Gio.bus_own_name(
        Gio.BusType.SESSION,
        "works.allegedly.PlaidSpendTest",
        Gio.BusNameOwnerFlags.NONE,
        null,
        null,
        null
      );
    }

    _unexportTestInterface() {
      if (this._testBusOwnerId) {
        Gio.bus_unown_name(this._testBusOwnerId);
        this._testBusOwnerId = 0;
      }
      if (this._testIface) {
        this._testIface.unexport();
        this._testIface = null;
      }
    }

    _addReadOnly(text, styleClass = null) {
      const item = new PopupMenu.PopupMenuItem(text, { reactive: false, can_focus: false });
      item.add_style_class_name("plaid-spend-read-only");
      if (styleClass) item.label.add_style_class_name(styleClass);
      this.menu.addMenuItem(item);
      return item;
    }

    _renderPopup() {
      if (this._destroyed || !this.menu) return;
      this.menu.removeAll();
      this._timingItem = null;
      const allowance = this._view?.allowance;
      const cards = Array.isArray(this._view?.cards) ? this._view.cards : [];
      const hasCachedCardSpend =
        !allowance &&
        cards.some((card) => card.spend_minor_units != null && Number.isFinite(Number(card.spend_minor_units)));
      const statusText =
        {
          starting: "Starting desktop client…",
          connecting: "Connecting to Plaid Spend…",
          authorizing: "Waiting for Authentik sign-in…",
          error: hasCachedCardSpend ? "Connection problem · figures may be stale" : "Connection problem",
          ready: allowance ? "Flexible spending" : "Statement-cycle spend",
        }[this._status] || "Plaid Spend";
      if (
        this._status !== "authentication-required" &&
        (!allowance || this._status !== "ready") &&
        (this._status !== "error" || hasCachedCardSpend || !this._error)
      )
        this._addReadOnly(statusText, "plaid-spend-header");

      if (this._status === "authentication-required") {
        const login = new PopupMenu.PopupMenuItem("Sign in with Authentik…");
        login.connect("activate", () => this._proxy?.call("Login", null, Gio.DBusCallFlags.NONE, -1, null, null));
        this.menu.addMenuItem(login);
      }

      const timingText = desktopTimingLabel(this._status, this._lastAttemptAt, this._nextRetryAt);
      if (timingText) this._timingItem = this._addReadOnly(timingText, "plaid-spend-caption");

      if (allowance) {
        if (allowance.status === "active" && this._status === "ready") {
          const window = this._settings.get_string("pace-window");
          const signal = panelSignal(allowance, window);
          const label = signal.label.startsWith("Pace") ? `${window} ${signal.label.toLowerCase()}` : signal.label;
          this._addReadOnly(
            `${formatMoney(allowance.available_minor_units, allowance.currency)} available · ${label}`,
            `plaid-spend-${signal.level}`
          );
          if (needsExactMoneyDetail(allowance.available_minor_units, allowance.currency)) {
            const details = new PopupMenu.PopupSubMenuMenuItem("Balance details");
            const exact = new PopupMenu.PopupMenuItem(
              `Exact available: ${formatExactMoney(allowance.available_minor_units, allowance.currency)}`,
              { reactive: false, can_focus: false }
            );
            exact.add_style_class_name("plaid-spend-read-only");
            details.menu.addMenuItem(exact);
            this.menu.addMenuItem(details);
          }
          if (allowance.spending_signal === "warning") {
            const basis = allowance.forecast?.basis_period?.id === "rolling_30d" ? "30d" : "7d";
            this._addReadOnly(`Overall warning · ${basis} forecast or pace`, "plaid-spend-caution");
          }
          this._addReadOnly(
            `7d ${formatDaily(periodReport(allowance, "recorded_pace_periods", "rolling_7d")?.observed_daily_minor_units, allowance.currency)} · ` +
              `30d ${formatDaily(periodReport(allowance, "recorded_pace_periods", "rolling_30d")?.observed_daily_minor_units, allowance.currency)}`
          );
          const daily = allowance.daily_reference_minor_units;
          this._addReadOnly(`Leash ~${formatDaily(daily, allowance.currency)}`, "plaid-spend-caption");
          const windowToggle = new PopupMenu.PopupSwitchMenuItem(
            "Use 30-day pace",
            this._settings.get_string("pace-window") === "30d"
          );
          windowToggle.connect("toggled", (_item, state) =>
            this._settings.set_string("pace-window", state ? "30d" : "7d")
          );
          this.menu.addMenuItem(windowToggle);
          if (this._settings.get_strv("panel-visible").includes("sparkline")) {
            const points = this._historyPoints();
            if (points.length > 0) this._addReadOnly(`Sparkline: last ${points.length} days of ${window} averages`);
            if (this._historyError) this._addReadOnly("Pace history unavailable", "plaid-spend-caution");
            else if (this._history && points.filter((point) => point?.observed_daily_minor_units != null).length < 2)
              this._addReadOnly("Pace history lacks coverage", "plaid-spend-caution");
            else if (points.some((point) => point?.observed_daily_minor_units == null))
              this._addReadOnly("Pace history has coverage gaps", "plaid-spend-caption");
          }
          for (const [periodId, label] of [
            ["rolling_7d", "7d"],
            ["rolling_30d", "30d"],
          ]) {
            const unmatched = periodReport(allowance, "recorded_pace_periods", periodId)?.unmatched_charges;
            if (unmatched?.count > 0)
              this._addReadOnly(
                `${label} unmatched ${unmatched.count} (${formatMoney(unmatched.amount_minor_units, allowance.currency)})`,
                "plaid-spend-caution"
              );
          }
        } else this._addReadOnly(allowance.note || "Allowance unavailable; check connection");
        if (allowance.last_synced_at)
          this._addReadOnly(`Sync ${formatSyncAge(allowance.last_synced_at)} ago`, "plaid-spend-caption");
      }
      if (this._view?.dashboard_url?.startsWith("https://")) {
        const link = new PopupMenu.PopupMenuItem("Check a purchase / dashboard…");
        link.connect("activate", () => Gio.AppInfo.launch_default_for_uri(this._view.dashboard_url, null));
        this.menu.addMenuItem(link);
      }
      if (!allowance) {
        if (cards.length === 0) {
          if (this._status !== "authentication-required") this._addReadOnly("No card data is available.");
        }
        for (const card of cards) {
          this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem(cardTitle(card)));
          const spend = formatMoney(card.spend_minor_units, card.currency);
          const limit =
            card.limit_minor_units == null ? "no limit set" : formatMoney(card.limit_minor_units, card.currency);
          const percent = card.spend_percent == null ? "" : ` · ${Number(card.spend_percent).toFixed(1)}%`;
          this._addReadOnly(
            card.statement_period?.kind === "statement"
              ? `Spend: ${spend} / ${limit}${percent}`
              : `Recorded spend: ${spend}`
          );
          const cycle =
            card.statement_period?.kind === "statement"
              ? `Cycle starts ${card.statement_period.start}`
              : card.statement_period?.kind === "provisional"
                ? `Since first recorded transaction ${card.statement_period.start} · statement date unavailable`
                : "Statement cycle unavailable";
          this._addReadOnly(cycle);
          this._addReadOnly(cardAlert(card));
          this._addReadOnly(`Last synced ${formatTimestamp(card.last_synced_at)}`);
        }
      }

      if (this._view?.generated_at && (!allowance || allowance.status !== "active" || this._status !== "ready")) {
        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        this._addReadOnly(`View updated ${formatTimestamp(this._view.generated_at)}`);
      }
      const genericSignIn =
        this._status === "authentication-required" &&
        ["Sign in to connect this computer", "Sign in to connect your Plaid account."].includes(this._error);
      if (this._error && !genericSignIn) this._addReadOnly(`Details: ${this._error.slice(0, 120)}`);
      const preferences = new PopupMenu.PopupMenuItem("Customize panel…");
      preferences.connect("activate", () => this._openPreferences());
      this.menu.addMenuItem(preferences);
    }

    destroy() {
      this._destroyed = true;
      this._stopTimingRefresh();
      if (this._menuOpenId) this.menu.disconnect(this._menuOpenId);
      this._cancelViewRequest();
      this._cancelHistoryRequest();
      this._daemonConnectCancellable.cancel();
      this._settings.disconnect(this._settingsChangedId);
      for (const handlerId of this._proxyHandlerIds) this._proxy.disconnect(handlerId);
      this._proxyHandlerIds = [];
      if (this._daemonConnection) this._daemonConnection.close(null, null);
      this._daemonConnection = null;
      this._unexportTestInterface();
      this._proxy = null;
      super.destroy();
    }
  }
);

export default class PlaidSpendExtension extends Extension {
  enable() {
    this._indicator = new PlaidSpendIndicator(this.getSettings(), () => this.openPreferences());
    Main.panel.addToStatusArea(this.uuid, this._indicator);
  }

  disable() {
    this._indicator?.destroy();
    this._indicator = null;
  }
}
