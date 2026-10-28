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
    const formatter = new Intl.NumberFormat(undefined, {
      style: "currency",
      currency: code,
      maximumFractionDigits: 0,
      minimumFractionDigits: 0,
    });
    const amount = value / 10 ** (precision ?? 2);
    if (amount !== 0 && Math.abs(amount) < 0.5) return `${amount < 0 ? "-" : ""}<${formatter.format(1)}`;
    return formatter.format(amount);
  } catch (_error) {
    const amount = value / 100;
    if (amount !== 0 && Math.abs(amount) < 0.5) return `${amount < 0 ? "-" : ""}<${code} 1`;
    return `${code} ${amount.toFixed(0)}`;
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

function allowanceSignal(allowance) {
  if (allowance.status !== "active" || allowance.available_minor_units == null)
    return { level: "unknown", label: "Unavailable", marker: "" };
  if (allowance.available_minor_units <= 0) return { level: "danger", label: "Exhausted", marker: "!!" };
  if (allowance.spending_signal === "warning") return { level: "caution", label: "Pace high", marker: "!" };
  if (allowance.spending_signal === "unavailable") return { level: "unknown", label: "Pace warming up", marker: "" };
  return { level: "good", label: "Below leash", marker: "" };
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
  if (status === "authentication-required")
    return lastAttemptAt > 0 ? `${attempt} · Sign-in required` : "Sign-in required · no automatic retry";
  if (status === "authorizing")
    return lastAttemptAt > 0 ? `${attempt} · Signing in` : "Signing in · no automatic retry";
  if (lastAttemptAt <= 0) return null;
  if (status === "ready") return `${attempt} · Connected`;
  if (status === "connecting") return `${attempt} · Connecting`;
  return `${attempt} · No retry scheduled`;
}

const PlaidSpendIndicator = GObject.registerClass(
  class PlaidSpendIndicator extends PanelMenu.Button {
    _init() {
      super._init(0.0, "Plaid Spend", false);

      this._destroyed = false;
      this._view = { cards: [], generated_at: null };
      this._status = "starting";
      this._error = "";
      this._lastAttemptAt = 0;
      this._nextRetryAt = 0;
      this._hasView = false;
      this._viewRequestId = 0;
      this._viewRequestCancellable = null;
      this._proxyHandlerIds = [];
      this._testMode = GLib.getenv("PLAID_SPEND_TEST") === "1";
      this._testIface = null;
      this._testBusOwnerId = 0;
      this._timingItem = null;
      this._timingRefreshId = 0;

      const box = new St.BoxLayout({ y_align: Clutter.ActorAlign.CENTER });
      this._icon = new St.Icon({ icon_name: "credit-card-symbolic", style_class: "system-status-icon" });
      this._label = new St.Label({ text: "Loading…", y_align: Clutter.ActorAlign.CENTER });
      box.add_child(this._icon);
      box.add_child(this._label);
      this.add_child(box);

      this._menuOpenId = this.menu.connect("open-state-changed", (_menu, open) => {
        if (open) {
          this._renderPopup();
          this._startTimingRefresh();
        } else this._stopTimingRefresh();
      });

      if (this._testMode) this._exportTestInterface();

      Gio.DBusProxy.new_for_bus(
        Gio.BusType.SESSION,
        Gio.DBusProxyFlags.NONE,
        INTERFACE_INFO,
        BUS_NAME,
        OBJECT_PATH,
        INTERFACE_NAME,
        null,
        (source, result) => {
          if (this._destroyed) return;
          try {
            this._proxy = Gio.DBusProxy.new_for_bus_finish(result);
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
                this._readProperties();
                if (this._proxy.g_name_owner) this._fetchView();
              })
            );
            this._readProperties();
            if (this._proxy.g_name_owner) this._fetchView();
            this._render();
          } catch (error) {
            this._error = error.message;
            this._status = "error";
            this._render();
          }
        }
      );

      this._render();
    }

    _readProperties() {
      if (!this._proxy) return;
      this._status = unpackString(this._proxy.get_cached_property("Status"), this._status);
      this._error = unpackString(this._proxy.get_cached_property("LastError"), this._error);
      if (!this._proxy.g_name_owner) {
        this._lastAttemptAt = 0;
        this._nextRetryAt = 0;
      } else {
        this._lastAttemptAt = unpackNumber(this._proxy.get_cached_property("LastAttemptAt"));
        this._nextRetryAt = unpackNumber(this._proxy.get_cached_property("NextRetryAt"));
      }
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

    _acceptView(rawView) {
      try {
        const view = JSON.parse(rawView);
        if (!view || !Array.isArray(view.cards)) throw new Error("invalid Plaid Spend view");
        this._cancelViewRequest();
        this._view = view;
        this._hasView = true;
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
      let label;
      if (this._status === "authentication-required") label = "Sign in";
      else if (this._status === "authorizing") label = "Signing in…";
      else if (!this._hasView && this._status === "error") label = "Offline";
      else if (!this._hasView && this._status !== "ready") label = "Connecting…";
      else if (this._view?.allowance?.status === "active" && this._status === "ready") {
        const a = this._view.allowance;
        const signal = allowanceSignal(a);
        label = `Flex ${formatMoney(a.available_minor_units, a.currency)} ${signal.marker}`.trim();
      } else if (this._view?.allowance) label = "Flex unavailable";
      else if (cards.length === 0) label = this._status === "error" ? "Offline" : "No cards";
      else label = totalLabel(this._view);
      if (
        !this._view?.allowance &&
        cards.some((card) => card.alert_state === "warning" || card.alert_state === "exceeded")
      ) {
        label = `${label} !`;
      }

      for (const level of ["good", "caution", "danger", "unknown"])
        this._label.remove_style_class_name(`plaid-spend-${level}`);
      if (this._view?.allowance) {
        const signal = this._status === "ready" ? allowanceSignal(this._view.allowance) : { level: "unknown" };
        this._label.add_style_class_name(`plaid-spend-${signal.level}`);
      }

      const level =
        this._view?.allowance && this._status === "ready" ? allowanceSignal(this._view.allowance).level : "unknown";
      // Keep the card glyph in ordinary operation; an alert glyph makes a high pace
      // or exhausted cushion recognizable before reading the compact number.
      this._icon.icon_name =
        level === "danger"
          ? "dialog-error-symbolic"
          : level === "caution"
            ? "dialog-warning-symbolic"
            : "credit-card-symbolic";
      for (const state of ["good", "caution", "danger", "unknown"])
        this._icon.remove_style_class_name(`plaid-spend-${state}`);
      this._icon.add_style_class_name(`plaid-spend-${level}`);
      this._label.set_text(label);
      this.accessible_name = `Plaid Spend ${label}`;
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
          '<method name="WaitForPaint"/>' +
          '<method name="CloseMenu"/>' +
          '<method name="GetPanelLabel"><arg type="s" direction="out" name="label"/></method>' +
          '<method name="GetDaemonOwner"><arg type="s" direction="out" name="owner"/></method>' +
          '<method name="GetMenuText"><arg type="s" direction="out" name="text"/></method>' +
          '<method name="GetMenuGeometry"><arg type="(iiii)" direction="out" name="rect"/></method>' +
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
          CloseMenu: () => this.menu.close(false),
          GetPanelLabel: () => this._label.get_text(),
          GetDaemonOwner: () => this._proxy?.g_name_owner || "",
          GetMenuText: () =>
            this.menu
              ._getMenuItems()
              .map((item) => item.label?.get_text() || "")
              .join("\n"),
          GetMenuGeometry: () => {
            const actor = this.menu.actor;
            const [x, y] = actor.get_transformed_position();
            const [width, height] = actor.get_transformed_size();
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
      if (styleClass) item.label.add_style_class_name(styleClass);
      this.menu.addMenuItem(item);
      return item;
    }

    _renderPopup() {
      if (this._destroyed || !this.menu) return;
      this.menu.removeAll();
      this._timingItem = null;
      const allowance = this._view?.allowance;
      const statusText =
        {
          starting: "Starting desktop client…",
          connecting: "Connecting to Plaid Spend…",
          authorizing: "Waiting for Authentik sign-in…",
          "authentication-required": "Sign in to see flexible spending",
          error: "Connection problem · figures may be stale",
          ready: allowance ? "Flexible spending" : "Statement-cycle spend",
        }[this._status] || "Plaid Spend";
      if (!allowance || this._status !== "ready") this._addReadOnly(statusText, "plaid-spend-header");

      if (this._status === "authentication-required") {
        const login = new PopupMenu.PopupMenuItem("Sign in with Authentik…");
        login.connect("activate", () => this._proxy?.call("Login", null, Gio.DBusCallFlags.NONE, -1, null, null));
        this.menu.addMenuItem(login);
      }

      const timingText = desktopTimingLabel(this._status, this._lastAttemptAt, this._nextRetryAt);
      if (timingText) this._timingItem = this._addReadOnly(timingText, "plaid-spend-caption");

      if (allowance) {
        if (allowance.status === "active" && this._status === "ready") {
          const signal = allowanceSignal(allowance);
          this._addReadOnly(
            `${formatMoney(allowance.available_minor_units, allowance.currency)} available · ${signal.label}`,
            `plaid-spend-${signal.level}`
          );
          this._addReadOnly(
            `7d ${formatDaily(periodReport(allowance, "recorded_pace_periods", "rolling_7d")?.observed_daily_minor_units, allowance.currency)} · ` +
              `30d ${formatDaily(periodReport(allowance, "recorded_pace_periods", "rolling_30d")?.observed_daily_minor_units, allowance.currency)}`
          );
          const daily = allowance.daily_reference_minor_units;
          this._addReadOnly(`Leash ~${formatDaily(daily, allowance.currency)}`, "plaid-spend-caption");
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
        const cards = Array.isArray(this._view?.cards) ? this._view.cards : [];
        if (cards.length === 0) {
          this._addReadOnly(
            this._status === "authentication-required" ? "Your account is not connected." : "No card data is available."
          );
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
      if (this._error) this._addReadOnly(`Details: ${this._error.slice(0, 120)}`);
    }

    destroy() {
      this._destroyed = true;
      this._stopTimingRefresh();
      if (this._menuOpenId) this.menu.disconnect(this._menuOpenId);
      this._cancelViewRequest();
      for (const handlerId of this._proxyHandlerIds) this._proxy.disconnect(handlerId);
      this._proxyHandlerIds = [];
      this._unexportTestInterface();
      this._proxy = null;
      super.destroy();
    }
  }
);

export default class PlaidSpendExtension extends Extension {
  enable() {
    this._indicator = new PlaidSpendIndicator();
    Main.panel.addToStatusArea(this.uuid, this._indicator);
  }

  disable() {
    this._indicator?.destroy();
    this._indicator = null;
  }
}
