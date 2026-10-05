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
const INTERFACE_INFO = Gio.DBusNodeInfo.new_for_xml(
  `<node><interface name="${INTERFACE_NAME}">
    <method name="GetView"><arg name="view" type="s" direction="out"/></method>
    <method name="Login"/>
    <signal name="ViewChanged"><arg name="view" type="s"/></signal>
    <property name="Status" type="s" access="read"/>
    <property name="LastError" type="s" access="read"/>
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

function formatMoney(minorUnits, currency) {
  if (minorUnits == null || !Number.isFinite(Number(minorUnits))) return "Unavailable";
  const code = typeof currency === "string" && currency.length === 3 ? currency.toUpperCase() : "USD";
  const value = Number(minorUnits);
  try {
    const formatter = new Intl.NumberFormat(undefined, { style: "currency", currency: code });
    const fractionDigits = formatter.resolvedOptions().maximumFractionDigits;
    return formatter.format(value / 10 ** fractionDigits);
  } catch (_error) {
    return `${code} ${(value / 100).toFixed(2)}`;
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

function cardAlert(card) {
  switch (card.alert_state) {
    case "warning":
      return card.alert_threshold_percent == null
        ? "Warning threshold reached"
        : `Warning · ${card.alert_threshold_percent}% threshold reached`;
    case "exceeded":
      return "Limit exceeded";
    case "unavailable":
      return !card.statement_available && card.cycle_start
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
  if (cards.some((card) => !card.statement_available)) return `${cards.length} cards`;

  const currencies = new Set(available.map((card) => (card.currency || "USD").toUpperCase()));
  if (available.length > 0 && currencies.size === 1) {
    const total = available.reduce((sum, card) => sum + Number(card.spend_minor_units), 0);
    const approximate = available.length !== cards.length ? "~" : "";
    return `${approximate}${formatMoney(total, [...currencies][0])}`;
  }
  return `${cards.length} cards`;
}

const PlaidSpendIndicator = GObject.registerClass(
  class PlaidSpendIndicator extends PanelMenu.Button {
    _init() {
      super._init(0.0, "Plaid Spend", false);

      this._destroyed = false;
      this._view = { cards: [], generated_at: null };
      this._status = "starting";
      this._error = "";
      this._hasView = false;
      this._testMode = GLib.getenv("PLAID_SPEND_TEST") === "1";
      this._testIface = null;
      this._testBusOwnerId = 0;

      const box = new St.BoxLayout({ y_align: Clutter.ActorAlign.CENTER });
      this._icon = new St.Icon({ icon_name: "credit-card-symbolic", style_class: "system-status-icon" });
      this._label = new St.Label({ text: "Loading…", y_align: Clutter.ActorAlign.CENTER });
      box.add_child(this._icon);
      box.add_child(this._label);
      this.add_child(box);

      this._menuOpenId = this.menu.connect("open-state-changed", (_menu, open) => {
        if (open) this._renderPopup();
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
            this._proxy.connect("g-signal", (_proxy, _sender, name, parameters) => {
              if (name !== "ViewChanged") return;
              const [rawView] = parameters.deep_unpack();
              this._acceptView(rawView);
            });
            this._proxy.connect("g-properties-changed", () => this._readProperties());
            this._proxy.connect("notify::g-name-owner", () => {
              this._readProperties();
              if (this._proxy.g_name_owner) this._fetchView();
            });
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
      this._render();
    }

    _fetchView() {
      if (!this._proxy?.g_name_owner || this._destroyed) return;
      this._proxy.call("GetView", null, Gio.DBusCallFlags.NONE, -1, null, (proxy, result) => {
        if (this._destroyed) return;
        try {
          const [rawView] = proxy.call_finish(result).deep_unpack();
          this._acceptView(rawView);
        } catch (error) {
          this._error = error.message;
          this._render();
        }
      });
    }

    _acceptView(rawView) {
      try {
        const view = JSON.parse(rawView);
        if (!view || !Array.isArray(view.cards)) throw new Error("invalid Plaid Spend view");
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
      else if (this._view?.allowance?.status === "active") {
        const a = this._view.allowance;
        label = formatMoney(a.available_minor_units, a.currency) + (a.alert_state === "normal" ? "" : " !");
      } else if (cards.length === 0) label = this._status === "error" ? "Offline" : "No cards";
      else label = totalLabel(this._view);
      if (cards.some((card) => card.alert_state === "warning" || card.alert_state === "exceeded")) {
        label = `${label} !`;
      }

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
          '<method name="OpenMenu"/>' +
          '<method name="CloseMenu"/>' +
          '<method name="GetPanelLabel"><arg type="s" direction="out" name="label"/></method>' +
          '<method name="GetMenuGeometry"><arg type="(iiii)" direction="out" name="rect"/></method>' +
          "</interface></node>",
        {
          Reload: (path) => this._loadFixtureFile(path),
          OpenMenu: () => this.menu.open(false),
          CloseMenu: () => this.menu.close(false),
          GetPanelLabel: () => this._label.get_text(),
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
      const statusText =
        {
          starting: "Starting desktop client…",
          connecting: "Connecting to Plaid Spend…",
          authorizing: "Waiting for Authentik sign-in…",
          "authentication-required": "Sign in to view your card spend",
          error: "Connection problem",
          ready: "Statement-cycle spend",
        }[this._status] || "Plaid Spend";
      this._addReadOnly(statusText, "plaid-spend-header");

      if (this._status === "authentication-required") {
        const login = new PopupMenu.PopupMenuItem("Sign in with Authentik…");
        login.connect("activate", () => this._proxy?.call("Login", null, Gio.DBusCallFlags.NONE, -1, null, null));
        this.menu.addMenuItem(login);
      }

      const allowance = this._view?.allowance;
      if (allowance) {
        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem("Flexible allowance · advisory"));
        if (allowance.status === "active") {
          this._addReadOnly(
            `${formatMoney(allowance.available_minor_units, allowance.currency)} available · ${allowance.alert_state}`
          );
          this._addReadOnly(`Next credit ${formatTimestamp(allowance.next_credit_at)}`);
          this._addReadOnly(
            `Projected exhaustion (no future credits): ${formatTimestamp(allowance.estimated_exhaustion_at)}`
          );
        } else this._addReadOnly(allowance.note || allowance.status);
      }
      if (this._view?.dashboard_url?.startsWith("https://")) {
        const link = new PopupMenu.PopupMenuItem("Open spending dashboard / purchase check…");
        link.connect("activate", () => Gio.AppInfo.launch_default_for_uri(this._view.dashboard_url, null));
        this.menu.addMenuItem(link);
      }
      const cards = Array.isArray(this._view?.cards) ? this._view.cards : [];
      if (cards.length === 0) {
        this._addReadOnly(
          this._status === "authentication-required" ? "Your account is not connected." : "No card data is available."
        );
      }

      for (const card of cards) {
        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem(cardTitle(card)));
        if (card.institution_name) this._addReadOnly(`Institution: ${card.institution_name}`);
        const spend = formatMoney(card.spend_minor_units, card.currency);
        const limit =
          card.limit_minor_units == null ? "no limit set" : formatMoney(card.limit_minor_units, card.currency);
        const percent = card.spend_percent == null ? "" : ` · ${Number(card.spend_percent).toFixed(1)}%`;
        this._addReadOnly(
          card.statement_available ? `Spend: ${spend} / ${limit}${percent}` : `Recorded spend: ${spend}`
        );

        if (card.posted_minor_units != null || card.pending_minor_units != null) {
          const posted = card.posted_minor_units == null ? "—" : formatMoney(card.posted_minor_units, card.currency);
          const pending = card.pending_minor_units == null ? "—" : formatMoney(card.pending_minor_units, card.currency);
          this._addReadOnly(`Posted: ${posted} · Pending: ${pending}`);
        }

        const cycle = card.statement_available
          ? `Cycle starts ${card.cycle_start}`
          : card.cycle_start
            ? `Since first recorded transaction ${card.cycle_start} · statement date unavailable`
            : "Statement cycle unavailable";
        this._addReadOnly(cycle);
        this._addReadOnly(cardAlert(card));
        this._addReadOnly(`Last synced ${formatTimestamp(card.last_synced_at)}`);
      }

      if (this._view?.generated_at) {
        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        this._addReadOnly(`View updated ${formatTimestamp(this._view.generated_at)}`);
      }
      if (this._error) this._addReadOnly(`Details: ${this._error.slice(0, 120)}`);
    }

    destroy() {
      this._destroyed = true;
      if (this._menuOpenId) this.menu.disconnect(this._menuOpenId);
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
