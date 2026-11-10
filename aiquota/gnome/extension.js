import GLib from "gi://GLib";
import GObject from "gi://GObject";
import Gio from "gi://Gio";
import St from "gi://St";
import Clutter from "gi://Clutter";
import Pango from "gi://Pango";

import { Extension } from "resource:///org/gnome/shell/extensions/extension.js";
import * as Main from "resource:///org/gnome/shell/ui/main.js";
import * as PanelMenu from "resource:///org/gnome/shell/ui/panelMenu.js";
import * as PopupMenu from "resource:///org/gnome/shell/ui/popupMenu.js";
import {
  formatAge as sharedFormatAge,
  formatExtraSpend as sharedFormatExtraSpend,
  refreshWindowTiming,
  staleObservationAt,
  formatClock,
  formatDuration as sharedFormatDuration,
  formatDeadline,
  formatMultiplier,
  formatPeakInterval,
  formatResetCreditSummary,
  roundHalfToEven,
} from "./presentation.js";

let fixtureNowMs = null;

function nowMs() {
  return fixtureNowMs ?? Date.now();
}

const BUS_NAME = "works.allegedly.AiQuota";
const OBJECT_PATH = "/works/allegedly/AiQuota";
const INTERFACE_NAME = "works.allegedly.AiQuota1";
const INTERFACE_INFO = Gio.DBusNodeInfo.new_for_xml(
  `<node><interface name="${INTERFACE_NAME}">
    <method name="GetView"><arg name="view" type="s" direction="out"/></method>
    <signal name="ViewChanged"><arg name="view" type="s"/></signal>
    <property name="Status" type="s" access="read"/>
    <property name="Error" type="s" access="read"/>
    <property name="LastAttemptAt" type="d" access="read"/>
    <property name="NextRetryAt" type="d" access="read"/>
  </interface></node>`
).lookup_interface(INTERFACE_NAME);

const TINT_CLASSES = [
  "quota-cool",
  "quota-ok",
  "quota-warn",
  "quota-hot",
  "quota-unknown",
  "quota-stale",
  "quota-error",
];
function decodeBytes(bytes) {
  return new TextDecoder().decode(typeof bytes.get_data === "function" ? bytes.get_data() : bytes);
}

function errorMessage(error) {
  return error?.message ?? String(error);
}

function unpackString(value, fallback = null) {
  const unpacked = value?.deep_unpack();
  return typeof unpacked === "string" ? unpacked : fallback;
}

function unpackNumber(value, fallback = 0) {
  const unpacked = value?.deep_unpack();
  return typeof unpacked === "number" && Number.isFinite(unpacked) ? unpacked : fallback;
}

function formatDuration(seconds) {
  if (seconds == null || !Number.isFinite(seconds)) return "?";
  return sharedFormatDuration(seconds);
}

function withLiveReset(state, stale = false) {
  return state ? refreshWindowTiming(state, nowMs(), stale) : state;
}

function formatAge(seconds) {
  if (seconds == null || !Number.isFinite(seconds)) return "?";
  return sharedFormatAge(seconds);
}

// The server selects the display snapshot; the desktop only formats its age.
function effectiveState(state) {
  const staleSince = staleObservationAt(state.observedAtMs, state.staleSince, state.staleAtMs, nowMs());
  const staleAge = staleSince != null ? Math.max(0, (nowMs() - staleSince) / 1000) : null;
  return { ...state, staleAge };
}

function formatCompactDollars(usd) {
  if (usd >= 1000) {
    const k = usd / 1000;
    return `$${k >= 10 ? roundHalfToEven(k) : roundHalfToEven(k * 10) / 10}k`;
  }
  return `$${roundHalfToEven(usd)}`;
}

// The "currently burning extra" decision lives in the Python view model
// (see aiquota/render/view_model.py) and is delivered to us by the desktop
// daemon as state.currentlyOverPlan. Don't
// reintroduce a local copy: see aiquota/AGENTS.md.
function formatExtraSpend(extra) {
  if (!extra || !extra.is_enabled || !(extra.used_usd > 0)) return null;
  return sharedFormatExtraSpend(extra.used_usd, extra.monthly_limit_usd);
}

function formatBankedResets(count, expiries, expiryLookupFailed, now, details = false) {
  if (count == null) return null;
  return formatResetCreditSummary(count, expiries ?? [], expiryLookupFailed, now, details);
}

function isApiProvider(providerId) {
  return providerId === "openai_platform" || providerId === "anthropic_platform";
}

function formatExpiry(expiry, details = false) {
  if (expiry == null) return "expiry unknown";
  return `expires ${formatDeadline(expiry, new Date(nowMs()), details) ?? "expiry unknown"}`;
}

function formatGrant(grant, details = false, includeGroup = false) {
  const parts = [];
  if (includeGroup && grant.group) parts.push(grant.group);
  parts.push(`${grant.label}: ${grant.remaining} left`);
  parts.push(formatExpiry(grant.expires_at, details));
  if (details) parts.push(`original ${grant.original}`);
  const scope = details ? (grant.scope_detail ?? grant.scope) : grant.scope;
  if (scope) parts.push(`scope: ${scope}`);
  if (grant.status) parts.push(grant.status);
  return parts.join(" · ");
}

function readingGroups(state) {
  const groups = new Map();
  for (const [kind, readings] of [
    ["balances", state.balances],
    ["grants", state.grants],
  ]) {
    for (const reading of readings) {
      const name = reading.group || null;
      if (!groups.has(name)) groups.set(name, { name, balances: [], grants: [] });
      groups.get(name)[kind].push(reading);
    }
  }
  return groups.size ? [...groups.values()] : [{ name: null, balances: [], grants: [] }];
}

function headerBalance(group) {
  if (group.balances.length !== 1) return null;
  const balance = group.balances[0];
  return balance.stale_since || balance.error ? null : balance;
}

// Peak-burn formatting. The policy (is it peak, which windows are next) is
// decided in aiquota/peak_windows.py and delivered as state.burn; this only
// formats it. Instants arrive absolute and are shown in the viewer's local
// zone — the whole point, since vendors publish these in their own.
function burnChangesAt(burn) {
  const first = burn?.upcoming?.[0];
  if (!first) return null;
  return new Date(burn.in_peak ? first.end : first.start);
}

function formatBurnSummary(burn) {
  const changesAt = burnChangesAt(burn);
  if (!changesAt) return null;
  const until = formatDuration(Math.max(0, (changesAt.getTime() - nowMs()) / 1000));
  if (burn.in_peak) {
    const scope = burn.applies_to ? ` · ${burn.applies_to}` : "";
    return `🔥 ${formatMultiplier(burn.multiplier)} burn until ${formatClock(changesAt)} (${until})${scope}`;
  }
  return `${formatMultiplier(burn.multiplier)} burn — next ${formatMultiplier(burn.peak_multiplier)} in ${until}`;
}

function formatPeakSchedule(burn) {
  if (!burn?.upcoming.length) return null;
  return `${formatMultiplier(burn.peak_multiplier)}: ${burn.upcoming.map(formatPeakInterval).join("  ")}`;
}

function clamp01(value) {
  if (value == null || !Number.isFinite(value)) return null;
  return Math.max(0, Math.min(1, value));
}

function emptyProviderState() {
  return {
    label: "",
    balances: [],
    grants: [],
    windows: [],
    errorSummary: null,
    errorDetail: null,
    extraSpend: null,
    availableResetCredits: null,
    availableResetCreditExpiries: [],
    resetCreditExpiryLookupFailed: false,
    currentlyOverPlan: false,
    paidCreditsActive: false,
    status: null,
    // Peak-burn schedule from the Python view model; null when the provider has none.
    burn: null,
    staleSince: null,
    summaryTint: "unknown",
    summaryIndex: null,
  };
}

// Descriptor for each provider. All runtime state (UI elements, fetch state)
// is attached at init time and referenced by id.
const PROVIDER_DEFS = [
  { id: "claude", label: "Claude", iconFile: "claude-symbolic.svg" },
  { id: "codex", label: "Codex", iconFile: "openai-symbolic.svg" },
  { id: "openai_platform", label: "OpenAI API", iconFile: "openai-symbolic.svg" },
  { id: "anthropic_platform", label: "Anthropic API", iconFile: "claude-symbolic.svg" },
  { id: "zai", label: "z.ai", iconFile: "zai-symbolic.svg" },
];

const QuotaIndicator = GObject.registerClass(
  class QuotaIndicator extends PanelMenu.Button {
    _init(extension) {
      super._init(0.0, "AI Quota Tracker", false);

      this._iconsDir = `${extension.path}/icons`;
      this._displayTickId = null;
      this._destroyed = false;
      this._proxy = null;
      this._daemonConnectPending = false;
      this._proxySignalIds = [];
      this._getterCancellable = null;
      this._ownerGeneration = 0;
      this._viewSignalSequence = 0;
      this._streamStatus = "starting";
      this._streamError = "";
      this._lastAttemptAt = 0;
      this._nextRetryAt = 0;
      this._testNow = null;
      this._remoteView = "";
      this._statusRow = null;

      const fixturePath = GLib.getenv("AI_QUOTA_FIXTURE");
      if (fixturePath) {
        this._fixtureMode = true;
        this._streamStatus = "connected";
        this._streamError = "";
        const [ok, bytes] = GLib.file_get_contents(fixturePath);
        if (!ok) throw new Error(`fixture not readable: ${fixturePath}`);
        this._initUI({});
        this._loadFixtureView(decodeBytes(bytes));
        this._exportTestInterface();
        return;
      }

      fixtureNowMs = null;
      this._fixtureMode = false;
      // The daemon view is the source of truth for provider visibility.
      // Start empty so an unconfigured provider (for example z.ai when using
      // the remote API) is never shown as a misleading empty entry.
      this._initUI({});
      this._connectDaemon();
    }

    // Set up per-provider state, build panel + popup, wire menu open handler.
    _initUI(shows) {
      // _providers: array of enabled provider descriptors with their runtime state and UI refs.
      this._providers = PROVIDER_DEFS.filter(({ id }) => shows[id]).map((def) => ({
        ...def,
        state: emptyProviderState(),
        icon: null,
        paceLabel: null,
        header: null,
        windowRows: [],
      }));

      this._buildPanel();
      this._buildPopup();
      this._menuOpenId = this.menu.connect("open-state-changed", (_menu, open) => {
        if (open) {
          this._renderPopup();
        }
      });
      this._startDisplayTick();
    }

    _loadFixtureView(rawView) {
      this._streamStatus = "connected";
      this._streamError = "";
      this._lastAttemptAt = 0;
      this._nextRetryAt = 0;
      this._testNow = null;
      this._acceptView(rawView);
    }

    _exportTestInterface() {
      // Session-bus interface used only by the golden render tests
      // (AI_QUOTA_FIXTURE gates the entire path). The test driver
      // launches gnome-shell once per session and then swaps fixtures /
      // toggles the menu via this surface, so renders get amortized
      // over a single shell process.
      //   Reload(path)         — load fixture state from JSON, re-render.
      //   OpenMenu / CloseMenu — toggle popup, no animation.
      //   GetMenuGeometry      — screen-space (x,y,w,h) bounding box of
      //                          the open menu, for precise screenshot crop.
      this._testIface = Gio.DBusExportedObject.wrapJSObject(
        '<node><interface name="works.allegedly.AiQuotaTest">' +
          '<method name="Reload"><arg type="s" direction="in" name="path"/></method>' +
          '<method name="SetStreamStatus"><arg type="s" direction="in" name="status"/><arg type="s" direction="in" name="error"/></method>' +
          '<method name="SetSyncState"><arg type="d" direction="in" name="lastAttemptAt"/><arg type="d" direction="in" name="nextRetryAt"/><arg type="d" direction="in" name="now"/></method>' +
          '<method name="GetStreamStatus"><arg type="s" direction="out" name="status"/></method>' +
          '<method name="GetBalanceText"><arg type="s" direction="in" name="provider"/><arg type="s" direction="out" name="text"/></method>' +
          '<method name="GetGrantText"><arg type="s" direction="in" name="provider"/><arg type="s" direction="out" name="text"/></method>' +
          '<method name="GetReadingGroups"><arg type="s" direction="in" name="provider"/><arg type="s" direction="out" name="groups"/></method>' +
          '<method name="GetStatusText"><arg type="s" direction="in" name="provider"/><arg type="s" direction="out" name="text"/></method>' +
          '<method name="GetBurnText"><arg type="s" direction="in" name="provider"/><arg type="s" direction="out" name="text"/></method>' +
          '<method name="GetResetText"><arg type="s" direction="in" name="provider"/><arg type="s" direction="out" name="text"/></method>' +
          '<method name="GetPanelText"><arg type="s" direction="in" name="provider"/><arg type="s" direction="out" name="text"/></method>' +
          '<method name="GetPanelProviders"><arg type="s" direction="out" name="providers"/></method>' +
          '<method name="SetDetailsOpen"><arg type="s" direction="in" name="provider"/><arg type="b" direction="in" name="open"/></method>' +
          '<method name="GetDetailsText"><arg type="s" direction="in" name="provider"/><arg type="s" direction="out" name="text"/></method>' +
          '<method name="GetConnectionDetails"><arg type="s" direction="out" name="text"/></method>' +
          '<method name="GetEmptyLabelText"><arg type="s" direction="out" name="text"/></method>' +
          '<method name="GetHeaderText"><arg type="s" direction="in" name="provider"/><arg type="s" direction="out" name="text"/></method>' +
          '<method name="GetWindowRows"><arg type="s" direction="in" name="provider"/><arg type="s" direction="out" name="rows"/></method>' +
          '<method name="SetViewForTest"><arg type="s" direction="in" name="view"/></method>' +
          '<method name="ConnectDaemon"/>' +
          '<method name="FetchView"/>' +
          '<method name="GetCurrentView"><arg type="s" direction="out" name="view"/></method>' +
          '<method name="GetDaemonOwner"><arg type="s" direction="out" name="owner"/></method>' +
          '<method name="OpenMenu"/>' +
          '<method name="WaitForPaint"/>' +
          '<method name="CloseMenu"/>' +
          '<method name="GetMenuGeometry"><arg type="(iiii)" direction="out" name="rect"/></method>' +
          "</interface></node>",
        {
          Reload: (path) => {
            const [ok, bytes] = GLib.file_get_contents(path);
            if (!ok) throw new Error(`fixture not readable: ${path}`);
            this._loadFixtureView(decodeBytes(bytes));
          },
          SetStreamStatus: (status, error) => this._setStreamState(status, error),
          GetBalanceText: (provider) => this._readingText(provider, "balanceRow"),
          GetGrantText: (provider) => this._readingText(provider, "grantRow"),
          GetReadingGroups: (provider) =>
            JSON.stringify(
              this._providers
                .find(({ id }) => id === provider)
                ?.readingSections.map((section) => ({
                  header: section.header?.label.get_text() ?? "",
                  balance: section.balanceRow.visible ? section.balanceRow._summaryLabel.get_text() : "",
                  grants: section.grantRow.visible ? section.grantRow._summaryLabel.get_text() : "",
                })) ?? []
            ),
          GetStatusText: (provider) =>
            this._providers.find(({ id }) => id === provider)?.statusRow?._summaryLabel.get_text() ?? "",
          GetBurnText: (provider) =>
            this._providers.find(({ id }) => id === provider)?.burnRow?._summaryLabel.get_text() ?? "",
          GetResetText: (provider) =>
            this._providers.find(({ id }) => id === provider)?.resetRow?._summaryLabel.get_text() ?? "",
          GetPanelText: (provider) => this._providers.find(({ id }) => id === provider)?.paceLabel?.get_text() ?? "",
          GetPanelProviders: () =>
            JSON.stringify(this._providers.filter((provider) => provider.icon).map(({ id }) => id)),
          SetDetailsOpen: (provider, open) => {
            const menu = this._providers.find(({ id }) => id === provider)?.detailsItem.menu;
            if (open) menu.open(false);
            else menu.close(false);
          },
          GetDetailsText: (provider) =>
            this._providers
              .find(({ id }) => id === provider)
              ?.detailsItem?._detailRows.map((row) => row.label.get_text())
              .join("\n") ?? "",
          GetConnectionDetails: () =>
            this._connectionDetailsItem?._detailRows.map((row) => row.label.get_text()).join("\n") ?? "",
          GetEmptyLabelText: () => (this._emptyLabel.visible ? this._emptyLabel.get_text() : ""),
          GetHeaderText: (provider) => {
            const item = this._providers.find(({ id }) => id === provider)?.header;
            return item?.label.get_text() ?? "";
          },
          GetWindowRows: (provider) => {
            const rows = this._providers
              .find(({ id }) => id === provider)
              ?.windowRows.filter((row) => row.visible)
              .map((row) => row._summaryLabel.get_text());
            return JSON.stringify(rows ?? []);
          },
          SetSyncState: (lastAttemptAt, nextRetryAt, now) => {
            this._lastAttemptAt = lastAttemptAt;
            this._nextRetryAt = nextRetryAt;
            this._testNow = now;
            this._renderPopup();
          },
          GetStreamStatus: () => this._statusRow._summaryLabel.get_text(),
          SetViewForTest: (view) => this._acceptView(view),
          ConnectDaemon: () => this._connectDaemon(),
          FetchView: () => this._fetchView(),
          GetCurrentView: () => this._remoteView,
          GetDaemonOwner: () => this._proxy?.g_name_owner ?? "",
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
          GetMenuGeometry: () => {
            const actor = this.menu.actor;
            const [x, y] = actor.get_transformed_position();
            const [w, h] = actor.get_transformed_size();
            return [Math.round(x), Math.round(y), Math.round(w), Math.round(h)];
          },
        }
      );
      this._testIface.export(Gio.DBus.session, "/works/allegedly/AiQuotaTest");
      this._testBusOwnerId = Gio.bus_own_name(
        Gio.BusType.SESSION,
        "works.allegedly.AiQuotaTest",
        Gio.BusNameOwnerFlags.NONE,
        null,
        null,
        null
      );
    }

    _connectDaemon() {
      if (this._proxy || this._daemonConnectPending || this._destroyed) return;
      this._daemonConnectPending = true;
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
            const proxy = Gio.DBusProxy.new_for_bus_finish(result);
            this._daemonConnectPending = false;
            this._proxy = proxy;
            this._proxySignalIds.push(
              proxy.connect("g-signal", (_proxy, sender, name, parameters) => {
                if (name !== "ViewChanged" || (sender && sender !== proxy.g_name_owner)) return;
                this._viewSignalSequence++;
                const [rawView] = parameters.deep_unpack();
                this._acceptView(rawView);
              })
            );
            this._proxySignalIds.push(proxy.connect("g-properties-changed", () => this._readProperties()));
            this._proxySignalIds.push(
              proxy.connect("notify::g-name-owner", () => {
                this._ownerGeneration++;
                this._getterCancellable?.cancel();
                this._getterCancellable = null;
                if (proxy.g_name_owner) {
                  this._setStreamState("connecting", "");
                  this._readProperties();
                  this._fetchView();
                } else {
                  this._lastAttemptAt = 0;
                  this._nextRetryAt = 0;
                  this._setStreamState("disconnected", "AIQuota daemon is not running");
                }
              })
            );

            this._readProperties();
            if (proxy.g_name_owner) this._fetchView();
            else this._setStreamState("disconnected", "AIQuota daemon is not running");
          } catch (error) {
            this._daemonConnectPending = false;
            this._setStreamState("disconnected", errorMessage(error));
          }
        }
      );
    }

    _readProperties() {
      if (!this._proxy) return;
      const status = unpackString(this._proxy.get_cached_property("Status"));
      const error = unpackString(this._proxy.get_cached_property("Error"));
      this._lastAttemptAt = unpackNumber(this._proxy.get_cached_property("LastAttemptAt"));
      this._nextRetryAt = unpackNumber(this._proxy.get_cached_property("NextRetryAt"));
      if (status !== null) this._streamStatus = status;
      if (error !== null) this._streamError = error;
      this._renderPanel();
      this._renderPopup();
    }

    _setStreamState(status, error) {
      this._streamStatus = status;
      this._streamError = error;
      this._renderPanel();
      this._renderPopup();
    }

    _fetchView() {
      const proxy = this._proxy;
      const owner = proxy?.g_name_owner;
      if (!owner || this._destroyed) return;
      this._getterCancellable?.cancel();
      const cancellable = new Gio.Cancellable();
      const ownerGeneration = this._ownerGeneration;
      const signalSequence = this._viewSignalSequence;
      this._getterCancellable = cancellable;
      proxy.call("GetView", null, Gio.DBusCallFlags.NONE, 15000, cancellable, (source, result) => {
        let rawView;
        try {
          [rawView] = proxy.call_finish(result).deep_unpack();
        } catch (error) {
          const stale =
            this._destroyed ||
            this._getterCancellable !== cancellable ||
            cancellable.is_cancelled() ||
            this._proxy !== proxy ||
            this._ownerGeneration !== ownerGeneration ||
            proxy.g_name_owner !== owner ||
            this._viewSignalSequence !== signalSequence;
          if (this._getterCancellable === cancellable) this._getterCancellable = null;
          if (!stale) this._setStreamState("disconnected", errorMessage(error));
          return;
        }
        const stale =
          this._destroyed ||
          this._getterCancellable !== cancellable ||
          cancellable.is_cancelled() ||
          this._proxy !== proxy ||
          this._ownerGeneration !== ownerGeneration ||
          proxy.g_name_owner !== owner ||
          this._viewSignalSequence !== signalSequence;
        if (this._getterCancellable === cancellable) this._getterCancellable = null;
        if (stale) return;
        if (rawView) this._acceptView(rawView);
      });
    }

    _renderStreamStatus() {
      if (!this._statusRow) return;
      this._statusRow.visible = true;
      if (this._streamStatus === "connected") this._statusRow._summaryLabel.add_style_class_name("quota-stream-live");
      else this._statusRow._summaryLabel.remove_style_class_name("quota-stream-live");
      if (this._streamStatus === "connected") {
        this._statusRow._summaryLabel.set_text(
          this._viewConnectionError ? "Connected · quota refresh failed" : "Connected"
        );
      } else if (this._streamStatus === "disconnected") {
        const now = this._testNow ?? nowMs() / 1000;
        const lastAttempt =
          this._lastAttemptAt > 0
            ? `last attempt ${formatAge(Math.max(0, now - this._lastAttemptAt))} ago`
            : "no attempt yet";
        const retry = this._nextRetryAt > 0 ? `retry in ${formatAge(this._nextRetryAt - now)}` : "no retry scheduled";
        this._statusRow._summaryLabel.set_text(`Disconnected · ${lastAttempt} · ${retry}`);
      } else {
        this._statusRow._summaryLabel.set_text("Connecting");
      }
      this._renderDetails(
        this._connectionDetailsItem,
        [
          this._streamStatus === "disconnected" && this._streamError
            ? `Desktop sync error: ${this._streamError}`
            : null,
          this._viewConnectionError ? `Quota refresh error: ${this._viewConnectionError}` : null,
        ].filter(Boolean)
      );
    }

    _acceptView(rawView) {
      try {
        if (typeof rawView !== "string" || rawView.length === 0) return;
        const data = JSON.parse(rawView);
        if (!data || !Array.isArray(data.providers)) throw new Error("invalid AIQuota view");
        if (this._fixtureMode) {
          const fixtureTime = Date.parse(data.presentation_at ?? data.fetched_at);
          fixtureNowMs = Number.isFinite(fixtureTime) ? fixtureTime : null;
        }
        this._remoteView = rawView;
        this._loadViewData(data);
        this._renderPanel();
        this._renderPopup();
      } catch (error) {
        this._setStreamState("disconnected", errorMessage(error));
      }
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

    _readingText(provider, key) {
      return (
        this._providers
          .find(({ id }) => id === provider)
          ?.readingSections.map((section) => section[key])
          .filter((row) => row.visible)
          .map((row) => row._summaryLabel.get_text())
          .join("\n") ?? ""
      );
    }

    _buildPanel() {
      const box = new St.BoxLayout({
        style_class: "quota-indicator",
        y_align: Clutter.ActorAlign.CENTER,
      });
      for (const p of this._providers) {
        if (isApiProvider(p.id)) continue;
        p.icon = this._makeIcon(p.iconFile);
        p.paceLabel = new St.Label({ style_class: "quota-pace", y_align: Clutter.ActorAlign.CENTER });
        const provBox = new St.BoxLayout({ style_class: "quota-provider", y_align: Clutter.ActorAlign.CENTER });
        provBox.add_child(p.icon);
        provBox.add_child(p.paceLabel);
        box.add_child(provBox);
      }
      this._panelBox = box;
      this._emptyLabel = new St.Label({
        text: "AI quota",
        style_class: "quota-empty-label",
        y_align: Clutter.ActorAlign.CENTER,
      });
      box.add_child(this._emptyLabel);
      this.add_child(box);
    }

    _makeIcon(filename) {
      return new St.Icon({
        gicon: Gio.icon_new_for_string(`${this._iconsDir}/${filename}`),
        style_class: "quota-icon",
        y_align: Clutter.ActorAlign.CENTER,
      });
    }

    _buildPopup() {
      this.menu.removeAll();
      this.menu.box.add_style_class_name("quota-popup-content");
      this._statusRow = this._makeTextRow();
      this._statusRow._summaryLabel.add_style_class_name("quota-stream-status");
      this._statusRow._summaryLabel.clutter_text.line_wrap = true;
      this.menu.addMenuItem(this._statusRow);
      this._connectionDetailsItem = this._makeDetailsSubmenu("Connection details");
      this.menu.addMenuItem(this._connectionDetailsItem);
      const subscriptions = this._providers.filter(({ id }) => !isApiProvider(id));
      const apiProviders = this._providers.filter(({ id }) => isApiProvider(id));
      if (subscriptions.length) this._addProviderSection("Subscriptions", subscriptions);
      if (apiProviders.length) this._addProviderSection("API balances", apiProviders);
    }

    _addProviderSection(title, providers) {
      this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem(title));
      for (const p of providers) {
        p.header = new PopupMenu.PopupSeparatorMenuItem(p.state.label || p.label);
        const groups = readingGroups(p.state);
        p.readingSections = groups.map((group) => ({
          name: group.name,
          header: groups.length > 1 && group.name ? new PopupMenu.PopupSeparatorMenuItem(group.name) : null,
          balanceRow: this._makeTextRow(),
          grantRow: this._makeTextRow(),
        }));
        p.resetRow = this._makeTextRow();
        p.statusRow = this._makeTextRow();
        p.burnRow = this._makeTextRow();
        p.windowRows = Array.from({ length: Math.max(1, effectiveState(p.state).windows.length) }, () =>
          this._makeQuotaRow("quota")
        );
        p.detailsItem = this._makeDetailsSubmenu("Details");
        this.menu.addMenuItem(p.header);
        for (const section of p.readingSections) {
          if (section.header) {
            section.header.label.add_style_class_name("quota-popup-header");
            section.header.label.clutter_text.line_wrap = true;
            this.menu.addMenuItem(section.header);
          }
          this.menu.addMenuItem(section.balanceRow);
          this.menu.addMenuItem(section.grantRow);
        }
        this.menu.addMenuItem(p.resetRow);
        this.menu.addMenuItem(p.statusRow);
        this.menu.addMenuItem(p.burnRow);
        for (const row of p.windowRows) this.menu.addMenuItem(row);
        this.menu.addMenuItem(p.detailsItem);
        p.header.label.add_style_class_name("quota-popup-header");
        p.header.label.clutter_text.line_wrap = true;
      }
    }

    _makeDetailsSubmenu(title) {
      const item = new PopupMenu.PopupSubMenuMenuItem(title);
      item._detailRows = [];
      return item;
    }

    _renderDetails(item, details) {
      if (!item) return;
      const next = details.filter((detail) => typeof detail === "string" && detail.length > 0);
      if (item._detailRows.map((row) => row.label.get_text()).join("\n") === next.join("\n")) {
        item.visible = next.length > 0;
        return;
      }
      item.menu.removeAll();
      item._detailRows = next.map((detail) => {
        const row = new PopupMenu.PopupMenuItem(detail, { reactive: false, can_focus: false });
        row.label.clutter_text.line_wrap = true;
        row.label.clutter_text.line_wrap_mode = Pango.WrapMode.WORD_CHAR;
        row.label.clutter_text.ellipsize = 0;
        item.menu.addMenuItem(row);
        return row;
      });
      item.visible = next.length > 0;
    }

    _makeTextRow() {
      const item = new PopupMenu.PopupBaseMenuItem({ reactive: false, can_focus: false });
      item.add_style_class_name("quota-popup-bar-item");
      item._summaryLabel = new St.Label({ text: "", style_class: "quota-popup-row", x_expand: true });
      item._summaryLabel.clutter_text.line_wrap = true;
      item._summaryLabel.clutter_text.line_wrap_mode = Pango.WrapMode.WORD_CHAR;
      item._summaryLabel.clutter_text.ellipsize = 0;
      item.add_child(item._summaryLabel);
      return item;
    }

    _makeQuotaRow(label) {
      const item = new PopupMenu.PopupBaseMenuItem({ reactive: false, can_focus: false });
      item.add_style_class_name("quota-popup-bar-item");

      const content = new St.BoxLayout({
        style_class: "quota-popup-bar-content",
        orientation: Clutter.Orientation.VERTICAL,
        x_expand: true,
      });
      item._summaryLabel = new St.Label({
        text: `${label}: no data`,
        style_class: "quota-popup-row",
        x_expand: true,
      });
      item._summaryLabel.clutter_text.line_wrap = true;
      item._summaryLabel.clutter_text.line_wrap_mode = Pango.WrapMode.WORD_CHAR;
      item._summaryLabel.clutter_text.ellipsize = 0;
      item._bars = new St.BoxLayout({
        style_class: "quota-bar-container",
        orientation: Clutter.Orientation.VERTICAL,
        x_expand: true,
      });

      item._quotaBar = this._makeQuotaBar();

      item._bars.add_child(item._quotaBar.track);
      content.add_child(item._summaryLabel);
      content.add_child(item._bars);
      item.add_child(content);
      return item;
    }

    // Match the web board's two-marker meter: the colored fill is quota used,
    // the tick is time elapsed, and the shaded gap makes the pace deviation
    // visible without needing a second bar.
    _makeQuotaBar() {
      const track = new St.Widget({ style_class: "quota-bar-track", x_expand: true });
      const fill = new St.Widget({ style_class: "quota-bar-fill" });
      const deviation = new St.Widget({ style_class: "quota-bar-deviation" });
      const tick = new St.Widget({ style_class: "quota-bar-tick" });
      const bar = { track, fill, deviation, tick, usageFraction: null, elapsedFraction: null };

      track.add_child(fill);
      track.add_child(deviation);
      track.add_child(tick);
      track.connect("notify::allocation", () => this._applyQuotaBar(bar));
      this._applyQuotaBar(bar);
      return bar;
    }

    _applyQuotaBar(bar) {
      const box = bar.track.get_allocation_box();
      const trackWidth = box.x2 - box.x1;
      const trackHeight = box.y2 - box.y1;
      if (!(trackWidth > 0) || !(trackHeight > 0)) return;

      const usage = bar.usageFraction;
      const elapsed = bar.elapsedFraction;
      const usageWidth = usage == null ? 0 : Math.round(trackWidth * usage);
      const elapsedPosition = elapsed == null ? null : Math.round(trackWidth * elapsed);

      bar.fill.visible = usage != null;
      bar.fill.remove_style_class_name("quota-bar-fill-end");
      if (usage != null && usage >= 1) bar.fill.add_style_class_name("quota-bar-fill-end");
      bar.fill.set_position(0, 0);
      bar.fill.set_size(usageWidth, trackHeight);

      const hasDeviation = elapsedPosition != null && usage != null && usageWidth !== elapsedPosition;
      bar.deviation.visible = hasDeviation;
      if (hasDeviation) {
        const deviationStart = Math.min(usageWidth, elapsedPosition);
        bar.deviation.set_position(deviationStart, 0);
        bar.deviation.set_size(Math.abs(usageWidth - elapsedPosition), trackHeight);
        bar.deviation.remove_style_class_name("quota-bar-ahead");
        bar.deviation.remove_style_class_name("quota-bar-behind");
        bar.deviation.add_style_class_name(usageWidth > elapsedPosition ? "quota-bar-ahead" : "quota-bar-behind");
      }

      bar.tick.visible = elapsedPosition != null;
      if (elapsedPosition != null) {
        bar.tick.set_position(Math.max(0, Math.min(trackWidth - 2, elapsedPosition - 1)), 0);
        bar.tick.set_size(2, trackHeight);
      }
    }

    _setQuotaBar(bar, usageFraction, elapsedFractionValue, tint) {
      bar.usageFraction = clamp01(usageFraction);
      bar.elapsedFraction = clamp01(elapsedFractionValue);
      this._setBarTint(bar, tint);
      this._applyQuotaBar(bar);
    }

    _setBarTint(bar, tint) {
      for (const cls of TINT_CLASSES) bar.fill.remove_style_class_name(cls);
      bar.fill.add_style_class_name(`quota-${tint}`);
    }

    _clearQuotaBar(bar) {
      this._setQuotaBar(bar, null, null, "unknown");
    }

    _mapWindow(w) {
      if (!w) return null;
      const resetAtMs = w.reset_at ? new Date(w.reset_at).getTime() : null;
      return {
        name: w.name ?? null,
        display: w.display !== false,
        usedPercent: w.used_percent ?? null,
        resetAtMs,
        resetSeconds: w.reset_seconds ?? null,
        windowSeconds: w.window_seconds ?? null,
        label: w.label,
        used: w.used,
        reset: w.reset,
        pace: w.pace,
        forecast: w.forecast,
        tint: w.tint,
        elapsed: w.elapsed,
        exhausted: w.exhausted,
        resetPending: w.reset_pending === true,
      };
    }

    _mapExtraSpend(extra) {
      if (!extra) return null;
      return {
        is_enabled: extra.is_enabled,
        utilization: extra.utilization,
        used_usd: extra.used_usd,
        monthly_limit_usd: extra.monthly_limit_usd,
      };
    }

    _loadViewData(data) {
      const providerIds = new Set(
        (Array.isArray(data.providers) ? data.providers : [])
          .map((provider) => provider?.provider)
          .filter((id) => PROVIDER_DEFS.some((definition) => definition.id === id))
      );
      this._setProviderVisibility(providerIds);
      const fetchedAt = data.fetched_at ? new Date(data.fetched_at).getTime() : null;
      this._viewConnectionError = data.connection_error ?? null;
      for (const p of this._providers) {
        const pq = data.providers?.find((x) => x.provider === p.id);
        if (!pq) {
          p.state = emptyProviderState();
          continue;
        }
        const selected = pq.display;
        const result = selected.result;
        p.state = {
          label: pq.label || p.label,
          balances: pq.balances ?? [],
          grants: pq.grants ?? [],
          windows: (pq.display_windows ?? []).map((window) => this._mapWindow(window)),
          observedAtMs: selected.observed_at ? new Date(selected.observed_at).getTime() : null,
          staleSince: pq.stale_since ? new Date(pq.stale_since).getTime() : null,
          staleAtMs: pq.stale_at ? new Date(pq.stale_at).getTime() : null,
          errorSummary: pq.error_summary ?? null,
          errorDetail: pq.error_detail ?? null,
          extraSpend: this._mapExtraSpend(result?.extra_spend),
          availableResetCredits: result?.available_reset_credits ?? null,
          availableResetCreditExpiries: result?.available_reset_credit_expiries ?? [],
          resetCreditExpiryLookupFailed: result?.reset_credit_expiry_lookup_failed === true,
          currentlyOverPlan: pq.currently_over_plan === true,
          paidCreditsActive: pq.paid_credits_active === true,
          status: pq.status ?? null,
          burn: pq.burn ?? null,
          summaryTint: pq.summary_tint ?? "unknown",
          summaryIndex: pq.summary_index ?? null,
        };
      }
      const needsRebuild = this._providers.some((p) => {
        const groups = readingGroups(p.state);
        return (
          p.windowRows.length !== Math.max(1, p.state.windows.length) ||
          p.readingSections.length !== groups.length ||
          p.readingSections.some((section, index) => section.name !== groups[index].name)
        );
      });
      if (needsRebuild) this._buildPopup();
    }

    _setProviderVisibility(providerIds) {
      const currentIds = this._providers.map(({ id }) => id);
      const nextIds = PROVIDER_DEFS.filter(({ id }) => providerIds.has(id)).map(({ id }) => id);
      if (currentIds.length === nextIds.length && currentIds.every((id, index) => id === nextIds[index])) return;

      const priorState = new Map(this._providers.map((provider) => [provider.id, provider.state]));
      this._providers = PROVIDER_DEFS.filter(({ id }) => providerIds.has(id)).map((def) => ({
        ...def,
        state: priorState.get(def.id) ?? emptyProviderState(),
        icon: null,
        paceLabel: null,
        header: null,
        windowRows: [],
      }));

      if (this._panelBox) {
        this.remove_child(this._panelBox);
        this._panelBox.destroy();
      }
      this._buildPanel();
      this._buildPopup();
    }

    _setTint(icon, paceLabel, tint) {
      for (const cls of TINT_CLASSES) {
        icon.remove_style_class_name(cls);
        paceLabel.remove_style_class_name(cls);
      }
      icon.add_style_class_name(`quota-${tint}`);
      paceLabel.add_style_class_name(`quota-${tint}`);
    }

    _renderPanel() {
      const subscriptions = this._providers.filter(({ id }) => !isApiProvider(id));
      this._emptyLabel.visible = subscriptions.length === 0;
      for (const p of subscriptions) this._renderProvider(p.state, p.icon, p.paceLabel);
    }

    _renderProvider(state, icon, paceLabel) {
      if (!icon || !paceLabel) return;
      const { windows, staleAge } = effectiveState(state);
      if (state.errorSummary && windows.length === 0) {
        this._setTint(icon, paceLabel, "error");
        paceLabel.set_text("!");
        return;
      }
      if (windows.length === 0) {
        this._setTint(icon, paceLabel, "unknown");
        paceLabel.set_text("");
        return;
      }
      const liveWindows = windows.map((window) => withLiveReset(window));
      const exhaustedWindows = liveWindows.filter((window) => window.exhausted);
      const resetPending = liveWindows.some((window) => window.resetPending);
      const stale = staleAge != null;
      const overPlan = !resetPending && !stale && state.currentlyOverPlan === true;
      const paidCreditsActive = !resetPending && !stale && state.paidCreditsActive === true;
      const tint = resetPending || stale ? "stale" : overPlan || paidCreditsActive ? "hot" : state.summaryTint;
      this._setTint(icon, paceLabel, tint);
      const paceText = state.summaryIndex == null ? "" : (liveWindows[state.summaryIndex]?.pace ?? "");
      const burning = state.burn?.in_peak === true ? "🔥" : "";
      if (resetPending) {
        paceLabel.set_text(`↻?${stale ? " ~" : ""}${burning}`);
      } else if (stale) {
        paceLabel.set_text(`~${burning}`);
      } else if (paidCreditsActive) {
        paceLabel.set_text(`⚡${paceText}${burning}`);
      } else if (overPlan) {
        paceLabel.set_text(`${formatCompactDollars(state.extraSpend.used_usd)} ⚡${burning}`);
      } else if (exhaustedWindows.length > 0) {
        const resetSeconds = Math.max(...exhaustedWindows.map((window) => window.resetSeconds));
        paceLabel.set_text(`↻${formatDuration(resetSeconds)}${burning}`);
      } else {
        paceLabel.set_text(burning ? `${paceText}${burning}`.trim() : paceText);
      }
    }

    _renderPopup() {
      this._renderStreamStatus();
      for (const p of this._providers) {
        const groups = readingGroups(p.state);
        const single = groups.length === 1;
        this._renderProviderHeader(p.header, p.state.label || p.label, single ? groups[0] : null, single);
        p.readingSections.forEach((section, index) => {
          const group = groups[index];
          if (section.header) this._renderProviderHeader(section.header, group.name, group, false);
          this._renderBalanceRow(section.balanceRow, group, single || section.header !== null);
          this._renderGrantRow(section.grantRow, group);
        });
        this._renderResetRow(p.resetRow, p.state);
        this._renderStatusRow(p.statusRow, p.state);
        this._renderBurnRow(p.burnRow, p.state);
        const { windows, staleAge } = effectiveState(p.state);
        windows.forEach((window, index) => this._renderPopupRow(p.windowRows[index], window, staleAge));
        p.windowRows.forEach((row, index) => (row.visible = index < windows.length));
        this._renderProviderDetails(p.detailsItem, p.state);
      }
    }

    _renderProviderHeader(item, title, group, includeGroup) {
      const parts = [title];
      if (includeGroup && group?.name) parts.push(group.name);
      const balance = group && headerBalance(group);
      if (balance) parts.push(`${balance.label}: ${balance.value}`);
      item.label.set_text(parts.join(" — "));
    }

    _renderBalanceRow(item, group, hasHeader) {
      const lines = group.balances.map((balance) => {
        const staleSince = balance.stale_since ? new Date(balance.stale_since).getTime() : null;
        const stale = staleSince == null ? "" : ` · stale ${formatAge(Math.max(0, (nowMs() - staleSince) / 1000))} ago`;
        return `${balance.label}: ${balance.value}${stale}`;
      });
      item.visible = lines.length > 0 && (!hasHeader || headerBalance(group) === null);
      item._summaryLabel.set_text(lines.join("\n"));
    }

    _renderGrantRow(item, group) {
      const lines = group.grants.map((grant) => formatGrant(grant));
      item.visible = lines.length > 0;
      item._summaryLabel.set_text(lines.join("\n"));
    }

    _renderResetRow(item, state) {
      const exhausted = state.windows.some((window) => window.exhausted);
      const count = state.availableResetCredits;
      const summary =
        count > 0 || (count === 0 && exhausted)
          ? formatBankedResets(
              count,
              state.availableResetCreditExpiries,
              state.resetCreditExpiryLookupFailed,
              new Date(nowMs())
            )
          : null;
      item.visible = summary != null;
      item._summaryLabel.set_text(summary == null ? "" : `Banked resets: ${summary}`);
    }

    _renderStatusRow(item, state) {
      const { staleAge } = effectiveState(state);
      const hasData = state.windows.length > 0 || state.balances.length > 0 || state.grants.length > 0;
      const parts = [];
      const resetPending = state.windows.some((window) => withLiveReset(window).resetPending);
      const status = resetPending ? "Reset time passed · awaiting updated usage" : state.status;
      if (status) parts.push(status);
      if (state.errorSummary) parts.push(state.errorSummary);
      if (staleAge != null) parts.push(`Stale · observation ${formatAge(staleAge)} old`);
      const extraSpend = formatExtraSpend(state.extraSpend);
      if (extraSpend) parts.push(extraSpend);
      if (!hasData) parts.push("No quota or balance readings");
      item.visible = parts.length > 0;
      item._summaryLabel.set_text(parts.join(" · "));
      item._summaryLabel.remove_style_class_name("quota-popup-status-warning");
      if (state.errorSummary || staleAge != null || resetPending)
        item._summaryLabel.add_style_class_name("quota-popup-status-warning");
    }

    _renderBurnRow(item, state) {
      const hasSubstantiveData = state.windows.length > 0 || state.balances.length > 0 || state.grants.length > 0;
      const summary = hasSubstantiveData && state.burn ? formatBurnSummary(state.burn) : null;
      item.visible = summary != null;
      item._summaryLabel.set_text(summary ?? "");
    }

    _renderProviderDetails(item, state) {
      const details = [];
      if (state.errorDetail) details.push(`${state.errorSummary ?? "Refresh failed"}: ${state.errorDetail}`);
      const includeGroup = readingGroups(state).length > 1;
      for (const balance of state.balances) {
        if (balance.error)
          details.push(
            `${includeGroup && balance.group ? `${balance.group} · ` : ""}${balance.label}: ${balance.error}`
          );
      }
      details.push(...state.grants.map((grant) => formatGrant(grant, true, includeGroup)));
      const { staleAge } = effectiveState(state);
      for (const window of state.windows.map((window) => withLiveReset(window))) {
        if (staleAge == null && window.pace) details.push(`${window.label} pace: ${window.pace}`);
      }
      if (state.windows.length) {
        details.push("Forecasts use average usage since the window started.");
        details.push("Meter fill is quota used; tick is time elapsed.");
      }
      const resetDetails = formatBankedResets(
        state.availableResetCredits,
        state.availableResetCreditExpiries,
        state.resetCreditExpiryLookupFailed,
        new Date(nowMs()),
        true
      );
      if (resetDetails && (state.availableResetCredits > 0 || state.windows.some((window) => window.exhausted)))
        details.push(`Banked resets: ${resetDetails}`);
      const schedule = formatPeakSchedule(state.burn);
      if (schedule && (state.windows.length || state.balances.length || state.grants.length)) details.push(schedule);
      this._renderDetails(item, details);
    }

    _renderPopupRow(item, state, staleAgeSeconds) {
      item._bars.visible = true;
      const liveState = withLiveReset(state, staleAgeSeconds != null);
      const parts = [
        `${liveState.used}%`,
        liveState.reset === "awaiting update" ? liveState.reset : `resets in ${liveState.reset}`,
      ];
      if (staleAgeSeconds == null && liveState.forecast) parts.push(liveState.forecast);
      item._summaryLabel.set_text(`${state.label}: ${parts.join("  ")}`);
      this._setQuotaBar(
        item._quotaBar,
        state.usedPercent == null ? null : state.usedPercent / 100,
        state.elapsed,
        staleAgeSeconds != null || liveState.resetPending ? "stale" : state.tint
      );
    }

    _startDisplayTick() {
      if (this._displayTickId) return;
      this._displayTickId = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, 1, () => {
        if (this._destroyed) {
          this._displayTickId = null;
          return GLib.SOURCE_REMOVE;
        }
        this._renderPanel();
        if (this.menu.isOpen) this._renderPopup();
        return GLib.SOURCE_CONTINUE;
      });
    }

    _stopDisplayTick() {
      if (!this._displayTickId) return;
      GLib.source_remove(this._displayTickId);
      this._displayTickId = null;
    }

    destroy() {
      this._destroyed = true;
      if (this._fixtureMode) fixtureNowMs = null;
      this._getterCancellable?.cancel();
      this._getterCancellable = null;
      if (this._proxy) {
        for (const signalId of this._proxySignalIds) this._proxy.disconnect(signalId);
      }
      this._proxySignalIds = [];
      this._proxy = null;
      this._unexportTestInterface();
      this._stopDisplayTick();
      if (this._menuOpenId) {
        this.menu.disconnect(this._menuOpenId);
        this._menuOpenId = null;
      }
      super.destroy();
    }
  }
);

export default class QuotaExtension extends Extension {
  enable() {
    this._indicator = new QuotaIndicator(this);
    Main.panel.addToStatusArea(this.uuid, this._indicator);
  }

  disable() {
    this._indicator?.destroy();
    this._indicator = null;
  }
}
