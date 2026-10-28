import GLib from "gi://GLib";
import GObject from "gi://GObject";
import Gio from "gi://Gio";
import St from "gi://St";
import Clutter from "gi://Clutter";

import { Extension } from "resource:///org/gnome/shell/extensions/extension.js";
import * as Main from "resource:///org/gnome/shell/ui/main.js";
import * as PanelMenu from "resource:///org/gnome/shell/ui/panelMenu.js";
import * as PopupMenu from "resource:///org/gnome/shell/ui/popupMenu.js";
import {
  bindingSummary,
  computePace as sharedComputePace,
  displayUsedPercent as sharedDisplayUsedPercent,
  elapsedFraction as sharedElapsedFraction,
  formatAge as sharedFormatAge,
  formatClock,
  formatDuration as sharedFormatDuration,
  formatKnownExpiry,
  formatMultiplier,
  formatPace,
  formatPaceForecast,
  formatPeakInterval,
  formatPrepaidBalance as sharedFormatPrepaidBalance,
  isExhausted as sharedIsExhausted,
  roundHalfToEven,
  tintFor,
  formatWindowLabel,
} from "./presentation.js";

const STALE_AFTER_SECONDS = 5 * 60;
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

function withLiveReset(state) {
  if (!state?.resetAtMs) return state;
  return {
    ...state,
    resetSeconds: Math.max(0, (state.resetAtMs - Date.now()) / 1000),
  };
}

function isStaleFetch(lastFetch) {
  return lastFetch != null && (Date.now() - lastFetch) / 1000 > STALE_AFTER_SECONDS;
}

function formatAge(seconds) {
  if (seconds == null || !Number.isFinite(seconds)) return "?";
  return sharedFormatAge(seconds);
}

// Pick the state to render: prefer the latest fetch, but fall back to the
// last successful snapshot (windows + extraSpend + reset credits) when the latest call
// returned nothing usable. `staleAge` is null when no fallback was needed.
function effectiveState(state) {
  if (
    state.windows.length > 0 ||
    state.availableResetCredits != null ||
    state.paidCredits != null ||
    state.prepaidBalance != null
  ) {
    const staleAge = state.error && state.lastFetch != null ? Math.max(0, (Date.now() - state.lastFetch) / 1000) : null;
    return {
      windows: state.windows,
      extraSpend: state.extraSpend,
      paidCredits: state.paidCredits,
      prepaidBalance: state.prepaidBalance,
      availableResetCredits: state.availableResetCredits,
      availableResetCreditExpiries: state.availableResetCreditExpiries,
      resetCreditExpiryLookupFailed: state.resetCreditExpiryLookupFailed,
      staleAge,
    };
  }
  const snap = state.lastSuccess;
  if (
    !snap ||
    (snap.windows.length === 0 &&
      snap.availableResetCredits == null &&
      snap.paidCredits == null &&
      snap.prepaidBalance == null)
  ) {
    return {
      windows: [],
      extraSpend: null,
      paidCredits: null,
      prepaidBalance: null,
      availableResetCredits: null,
      availableResetCreditExpiries: [],
      resetCreditExpiryLookupFailed: false,
      staleAge: null,
    };
  }
  const ageSeconds = snap.fetchedAt != null ? Math.max(0, (Date.now() - snap.fetchedAt) / 1000) : null;
  return {
    windows: snap.windows,
    extraSpend: snap.extraSpend,
    paidCredits: snap.paidCredits,
    prepaidBalance: snap.prepaidBalance,
    availableResetCredits: snap.availableResetCredits,
    availableResetCreditExpiries: snap.availableResetCreditExpiries,
    resetCreditExpiryLookupFailed: snap.resetCreditExpiryLookupFailed,
    staleAge: ageSeconds,
  };
}

function formatCheckFailure(error, lastCheck, haveWindows) {
  const prefix = haveWindows ? "last refresh failed" : "check failed";
  if (lastCheck == null) return `${prefix}: ${error}`;
  const ageSeconds = Math.max(0, (Date.now() - lastCheck) / 1000);
  return `${prefix} ${formatAge(ageSeconds)} ago: ${error}`;
}

// Pure pace computation. See DESIGN.md ("Pace math") for derivation.
function computePace({ usedPercent, resetSeconds, windowSeconds }) {
  if (usedPercent == null || resetSeconds == null || windowSeconds == null || windowSeconds <= 0) {
    return null;
  }
  return sharedComputePace({ usedPercent, resetSeconds, windowSeconds });
}

function isExhausted(state) {
  return state?.usedPercent != null && sharedIsExhausted(state);
}

function formatUsedPercent(state) {
  if (state?.usedPercent == null) return "?";
  return `${sharedDisplayUsedPercent(state.usedPercent)}%`;
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
  const used = extra.used_usd;
  const limit = extra.monthly_limit_usd;
  const pct = roundHalfToEven(extra.utilization);
  return `extra $${roundHalfToEven(used)}/$${roundHalfToEven(limit)} (${pct}%) this month`;
}

function formatBankedResets(count, expiries, expiryLookupFailed) {
  if (count == null) return null;
  const knownExpiries = (expiries ?? []).map(formatKnownExpiry).filter((expiry) => expiry != null);
  const label = `${count} banked reset${count === 1 ? "" : "s"}`;
  if (expiryLookupFailed) return `${label} · expiry dates unavailable`;
  return knownExpiries.length ? `${label} · known expiries: ${knownExpiries.join(", ")}` : label;
}

function formatPaidCredits(credits, active) {
  if (!credits) return null;
  let amount = "available";
  if (credits.unlimited) amount = "unlimited";
  else if (credits.balance != null) {
    const parsed = Number(credits.balance);
    if (Number.isFinite(parsed))
      amount = `${new Intl.NumberFormat(undefined, { maximumFractionDigits: 0 }).format(parsed)} left`;
  }
  return `${active ? "⚡ using paid credits" : "paid credits"} · ${amount}`;
}

function formatPrepaidBalance(balance) {
  if (balance == null) return null;
  if (balance.kind !== "available") return "Claude prepaid balance unavailable";
  return `Claude prepaid balance · ${sharedFormatPrepaidBalance(balance.amount_minor, balance.exponent, balance.currency)}`;
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
  const until = formatDuration(Math.max(0, (changesAt.getTime() - Date.now()) / 1000));
  if (burn.in_peak) {
    return `🔥 ${formatMultiplier(burn.multiplier)} burn until ${formatClock(changesAt)} (${until})`;
  }
  return `${formatMultiplier(burn.multiplier)} burn — next ${formatMultiplier(burn.peak_multiplier)} in ${until}`;
}

function formatPeakSchedule(burn) {
  const ahead = burn.in_peak ? burn.upcoming.slice(1) : burn.upcoming;
  if (!ahead.length) return null;
  return `${formatMultiplier(burn.peak_multiplier)}: ${ahead.map(formatPeakInterval).join("  ")}`;
}

function clamp01(value) {
  if (value == null || !Number.isFinite(value)) return null;
  return Math.max(0, Math.min(1, value));
}

function elapsedFraction(state) {
  if (state?.resetSeconds == null || state?.windowSeconds == null || state.windowSeconds <= 0) return null;
  return sharedElapsedFraction(state);
}

function emptyProviderState() {
  return {
    windows: [],
    lastFetch: null,
    lastCheck: null,
    error: null,
    extraSpend: null,
    paidCredits: null,
    prepaidBalance: null,
    availableResetCredits: null,
    availableResetCreditExpiries: [],
    resetCreditExpiryLookupFailed: false,
    currentlyOverPlan: false,
    extraStatus: "none",
    paidCreditsActive: false,
    // Peak-burn schedule from the Python view model; null when the provider has none.
    burn: null,
    // Last successful fetch — populated when the most recent attempt failed
    // but a prior good snapshot exists. {windows, extraSpend, fetchedAt}.
    lastSuccess: null,
  };
}

// Descriptor for each provider. All runtime state (UI elements, fetch state)
// is attached at init time and referenced by id.
const PROVIDER_DEFS = [
  { id: "claude", label: "Claude", iconFile: "claude-symbolic.svg" },
  { id: "codex", label: "Codex", iconFile: "openai-symbolic.svg" },
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
        // Provider visibility derived from which keys are present in the fixture.
        const [ok, bytes] = GLib.file_get_contents(fixturePath);
        if (!ok) throw new Error(`fixture not readable: ${fixturePath}`);
        const fixtureData = JSON.parse(decodeBytes(bytes));
        const shows = {};
        for (const { id } of PROVIDER_DEFS) shows[id] = id in fixtureData;
        this._initUI(shows);
        this._loadFixtureData(fixtureData);
        this._exportTestInterface();
        return;
      }

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

    _loadFixtureData(data) {
      this._streamStatus = "connected";
      this._streamError = "";
      this._lastAttemptAt = 0;
      this._nextRetryAt = 0;
      this._testNow = null;
      // Test fixtures specify currentlyOverPlan / extraStatus explicitly so
      // we never re-derive policy on the JS side (see aiquota/AGENTS.md).
      const loadLastSuccess = (snap) => {
        if (!snap) return null;
        // Fixtures express snapshot age as `ageSeconds` (relative to "now")
        // so the rendered "(stale Xm)" tag is stable across test runs.
        const fetchedAt = snap.ageSeconds != null ? Date.now() - snap.ageSeconds * 1000 : (snap.fetchedAt ?? null);
        return {
          windows: [snap.short, snap.long].filter((window) => window != null),
          extraSpend: snap.extraSpend ?? null,
          paidCredits: snap.paidCredits ?? null,
          prepaidBalance: snap.prepaidBalance ?? null,
          availableResetCredits: snap.availableResetCredits ?? null,
          availableResetCreditExpiries: snap.availableResetCreditExpiries ?? [],
          resetCreditExpiryLookupFailed: snap.resetCreditExpiryLookupFailed === true,
          fetchedAt,
        };
      };
      const provider = (node) => {
        const lastFetch = node?.lastFetch != null ? Date.now() : null;
        const lastCheck = node?.lastCheckAgeSeconds != null ? Date.now() - node.lastCheckAgeSeconds * 1000 : lastFetch;
        return {
          windows: [node?.short, node?.long].filter((window) => window != null),
          lastFetch,
          lastCheck,
          error: node?.error ?? null,
          extraSpend: node?.extraSpend ?? null,
          paidCredits: node?.paidCredits ?? null,
          prepaidBalance: node?.prepaidBalance ?? null,
          availableResetCredits: node?.availableResetCredits ?? null,
          availableResetCreditExpiries: node?.availableResetCreditExpiries ?? [],
          resetCreditExpiryLookupFailed: node?.resetCreditExpiryLookupFailed === true,
          currentlyOverPlan: node?.currentlyOverPlan === true,
          extraStatus: node?.extraStatus ?? "none",
          paidCreditsActive: node?.paidCreditsActive === true,
          burn: node?.burn ?? null,
          lastSuccess: loadLastSuccess(node?.lastSuccess),
        };
      };
      for (const p of this._providers) p.state = provider(data[p.id]);
      this._buildPopup();
      this._renderPanel();
      this._renderPopup();
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
          '<method name="GetPrepaidText"><arg type="s" direction="in" name="provider"/><arg type="s" direction="out" name="text"/></method>' +
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
            this._loadFixtureData(JSON.parse(decodeBytes(bytes)));
          },
          SetStreamStatus: (status, error) => this._setStreamState(status, error),
          GetPrepaidText: (provider) => {
            const item = this._providers.find(({ id }) => id === provider)?.prepaidRow;
            return item?._summaryLabel.get_text() ?? "";
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
      const label =
        this._streamStatus === "connected"
          ? "Quota stream live"
          : this._streamStatus === "disconnected"
            ? "Quota stream disconnected"
            : "Connecting to quota stream";
      const now = this._testNow ?? Date.now() / 1000;
      const lastAttempt =
        this._lastAttemptAt > 0
          ? `Desktop sync attempt ${formatAge(Math.max(0, now - this._lastAttemptAt))} ago`
          : "Desktop sync not attempted yet";
      const retry = this._nextRetryAt > 0 ? `Retry in ${formatAge(this._nextRetryAt - now)}` : "No retry scheduled";
      const error = this._streamError ? this._streamError.slice(0, 120) : "";
      const status = error ? `${label} · ${error}` : label;
      this._statusRow._summaryLabel.set_text(`${status}\n${lastAttempt} · ${retry}`);
    }

    _acceptView(rawView) {
      try {
        if (typeof rawView !== "string" || rawView.length === 0) return;
        const data = JSON.parse(rawView);
        if (!data || !Array.isArray(data.providers)) throw new Error("invalid AIQuota view");
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

    _buildPanel() {
      const box = new St.BoxLayout({
        style_class: "quota-indicator",
        y_align: Clutter.ActorAlign.CENTER,
      });
      for (const p of this._providers) {
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
      this._statusRow = this._makeTextRow();
      this._statusRow._summaryLabel.add_style_class_name("quota-stream-status");
      this.menu.addMenuItem(this._statusRow);
      for (const p of this._providers) {
        p.header = new PopupMenu.PopupSeparatorMenuItem(p.label);
        const { windows } = effectiveState(p.state);
        const rowCount = p.state.currentlyOverPlan || p.state.paidCreditsActive ? 1 : Math.max(1, windows.length);
        p.windowRows = Array.from({ length: rowCount }, () => this._makeQuotaRow("quota"));
        p.creditRow = this._makeTextRow();
        p.prepaidRow = this._makeTextRow();
        // Separate from windowRows so window indexing is unaffected; hidden
        // outright for providers with no published schedule.
        p.burnRow = this._makeTextRow();
        this.menu.addMenuItem(p.header);
        this.menu.addMenuItem(p.creditRow);
        this.menu.addMenuItem(p.prepaidRow);
        this.menu.addMenuItem(p.burnRow);
        for (const row of p.windowRows) this.menu.addMenuItem(row);
        p.header.label.add_style_class_name("quota-popup-header");
      }
    }

    _makeTextRow() {
      const item = new PopupMenu.PopupBaseMenuItem({ reactive: false, can_focus: false });
      item.add_style_class_name("quota-popup-bar-item");
      item._summaryLabel = new St.Label({ text: "", style_class: "quota-popup-row", x_expand: true });
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

    _mapLastSuccess(snap) {
      // snap = SuccessfulProviderFetch = {fetched_at, result: FetchSuccess}.
      if (!snap?.result) return null;
      return {
        windows: (snap.result.windows ?? [])
          .map((window) => this._mapWindow(window))
          .filter((window) => window.display),
        extraSpend: this._mapExtraSpend(snap.result.extra_spend),
        paidCredits: snap.result.paid_credits ?? null,
        prepaidBalance: snap.result.prepaid_balance ?? null,
        availableResetCredits: snap.result.available_reset_credits ?? null,
        availableResetCreditExpiries: snap.result.available_reset_credit_expiries ?? [],
        resetCreditExpiryLookupFailed: snap.result.reset_credit_expiry_lookup_failed === true,
        fetchedAt: snap.fetched_at ? new Date(snap.fetched_at).getTime() : null,
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
      for (const p of this._providers) {
        const pq = data.providers?.find((x) => x.provider === p.id);
        if (!pq) {
          p.state = emptyProviderState();
          continue;
        }
        // last_output.result is a tagged union: kind="success" carries window
        // data, kind="error" carries the error string.
        const result = pq.last_output?.result ?? {};
        const isSuccess = result.kind === "success";
        const lastSuccess = this._mapLastSuccess(pq.last_success);
        const lastCheck = pq.last_output?.fetched_at ? new Date(pq.last_output.fetched_at).getTime() : fetchedAt;
        p.state = {
          windows: isSuccess
            ? (result.windows ?? []).map((window) => this._mapWindow(window)).filter((window) => window.display)
            : [],
          lastFetch: isSuccess ? lastCheck : (lastSuccess?.fetchedAt ?? null),
          lastCheck,
          error: isSuccess ? null : (result.error ?? null),
          extraSpend: isSuccess ? this._mapExtraSpend(result.extra_spend) : null,
          paidCredits: isSuccess ? (result.paid_credits ?? null) : null,
          prepaidBalance: isSuccess ? (result.prepaid_balance ?? null) : null,
          availableResetCredits: isSuccess ? (result.available_reset_credits ?? null) : null,
          availableResetCreditExpiries: isSuccess ? (result.available_reset_credit_expiries ?? []) : [],
          resetCreditExpiryLookupFailed: isSuccess && result.reset_credit_expiry_lookup_failed === true,
          // Derived policy bits from the Python view model — single source of truth.
          currentlyOverPlan: pq.currently_over_plan === true,
          extraStatus: pq.extra_status ?? "none",
          paidCreditsActive: pq.paid_credits_active === true,
          burn: pq.burn ?? null,
          lastSuccess,
        };
      }
      this._buildPopup();
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
      this._emptyLabel.visible = this._providers.length === 0 && this._streamStatus !== "connected";
      for (const p of this._providers) this._renderProvider(p.state, p.icon, p.paceLabel);
    }

    _renderProvider(state, icon, paceLabel) {
      const { windows, staleAge } = effectiveState(state);
      if (state.error && windows.length === 0) {
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
      const paces = liveWindows.map((window) => (isExhausted(window) ? null : computePace(window)));
      const exhaustedWindows = liveWindows.filter((window) => isExhausted(window));
      const longestDuration = Math.max(...liveWindows.map((window) => window.windowSeconds));
      const tints = liveWindows.map((window, index) =>
        tintFor(paces[index] ?? null, window.usedPercent, window.windowSeconds < longestDuration)
      );
      const overPlan = state.currentlyOverPlan === true || state.paidCreditsActive === true;
      const stale = staleAge != null || isStaleFetch(state.lastFetch);
      const binding = bindingSummary(liveWindows.map((window, index) => ({ name: window.name, tint: tints[index] })));
      const tint = overPlan ? "hot" : stale ? "stale" : exhaustedWindows.length > 0 ? "hot" : binding.tint;
      this._setTint(icon, paceLabel, tint);
      const paceText = binding.index == null ? "" : (formatPace(paces[binding.index]) ?? "");
      // A peak window costs a multiple per token, so it belongs in the panel
      // where it is visible without opening the popup.
      const burning = state.burn?.in_peak === true ? "🔥" : "";
      if (state.paidCreditsActive === true) {
        paceLabel.set_text(`credits ⚡${burning}`);
      } else if (overPlan) {
        paceLabel.set_text(`${formatCompactDollars(state.extraSpend.used_usd)} ⚡${burning}`);
      } else if (exhaustedWindows.length > 0) {
        // Multiple exhausted windows keep the provider blocked until the last reset.
        const resetSeconds = Math.max(...exhaustedWindows.map((window) => window.resetSeconds));
        paceLabel.set_text(`↻${formatDuration(resetSeconds)}${burning}`);
      } else {
        paceLabel.set_text(burning ? `${paceText}${burning}`.trim() : paceText);
      }
    }

    _renderPopup() {
      this._renderStreamStatus();
      for (const p of this._providers) {
        this._renderProviderHeader(p.header, p.label, p.state);
        this._renderCreditRow(p.creditRow, p.state);
        this._renderPrepaidRow(p.prepaidRow, p.state);
        this._renderBurnRow(p.burnRow, p.state.burn);
        if (p.state.currentlyOverPlan === true || p.state.paidCreditsActive === true) {
          const { windows } = effectiveState(p.state);
          this._renderExtraActiveRow(p.windowRows[0], windows);
          p.windowRows.slice(1).forEach((row) => (row.visible = false));
        } else {
          const { windows, staleAge } = effectiveState(p.state);
          const longestDuration = Math.max(...windows.map((window) => window.windowSeconds));
          if (windows.length === 0) this._renderPopupRow(p.windowRows[0], null, staleAge, false);
          windows.forEach((window, index) =>
            this._renderPopupRow(p.windowRows[index], window, staleAge, window.windowSeconds < longestDuration)
          );
          p.windowRows.forEach((row, index) => (row.visible = index < Math.max(1, windows.length)));
        }
      }
    }

    _renderProviderHeader(item, title, state) {
      item.label.remove_style_class_name("quota-popup-header-error");
      item.label.remove_style_class_name("quota-popup-header-stale");

      const {
        windows,
        extraSpend,
        availableResetCredits,
        availableResetCreditExpiries,
        resetCreditExpiryLookupFailed,
        staleAge,
      } = effectiveState(state);
      const haveWindows = windows.length > 0;
      const parts = [title];
      if (state.error) {
        parts.push(formatCheckFailure(state.error, state.lastCheck, haveWindows));
        item.label.add_style_class_name("quota-popup-header-error");
      } else if (isStaleFetch(state.lastFetch)) {
        item.label.add_style_class_name("quota-popup-header-stale");
      }
      if (staleAge != null) parts.push(`(stale ${formatAge(staleAge)})`);
      const resets = formatBankedResets(
        availableResetCredits,
        availableResetCreditExpiries,
        resetCreditExpiryLookupFailed
      );
      if (resets) parts.push(resets);
      const extraStr = formatExtraSpend(extraSpend);
      if (extraStr) parts.push(extraStr);
      if (!state.error && state.lastFetch == null) parts.push("no successful refresh yet");
      else if (!state.error && isStaleFetch(state.lastFetch)) parts.push("stale");
      item.label.set_text(parts.join(" · "));
    }

    _renderCreditRow(item, state) {
      const { paidCredits } = effectiveState(state);
      const text = formatPaidCredits(paidCredits, state.paidCreditsActive);
      item.visible = text != null;
      if (text == null) return;
      item._summaryLabel.set_text(text);
      if (state.paidCreditsActive) item._summaryLabel.add_style_class_name("quota-credit-active");
      else item._summaryLabel.remove_style_class_name("quota-credit-active");
    }

    _renderPrepaidRow(item, state) {
      const text = formatPrepaidBalance(effectiveState(state).prepaidBalance);
      item.visible = text != null;
      if (text != null) item._summaryLabel.set_text(text);
    }

    _renderBurnRow(item, burn) {
      const summary = burn ? formatBurnSummary(burn) : null;
      item.visible = summary != null;
      if (summary == null) return;
      const schedule = formatPeakSchedule(burn);
      item._summaryLabel.set_text(schedule ? `${summary}\n${schedule}` : summary);
    }

    _renderExtraActiveRow(item, windows) {
      item._bars.visible = false;
      this._clearQuotaBar(item._quotaBar);
      const parts = windows.map((window) => this._formatExtraActiveWindow(window));
      item._summaryLabel.set_text(parts.join("  "));
    }

    _formatExtraActiveWindow(state) {
      const liveState = withLiveReset(state);
      const label = formatWindowLabel(liveState);
      const used = formatUsedPercent(liveState);
      return `${label}: ${used} ↻${formatDuration(liveState.resetSeconds)}`;
    }

    _renderPopupRow(item, state, staleAgeSeconds, isShort) {
      if (state == null) {
        item._bars.visible = false;
        item._summaryLabel.set_text("no data");
        this._clearQuotaBar(item._quotaBar);
        return;
      }
      item._bars.visible = true;
      const liveState = withLiveReset(state);
      const label = formatWindowLabel(liveState);
      const exhausted = isExhausted(liveState);
      const pace = exhausted ? null : computePace(liveState);
      const used = formatUsedPercent(liveState);
      const reset = `↻${formatDuration(liveState.resetSeconds)}`;
      const paceStr = formatPace(pace);
      const forecast = formatPaceForecast(pace, liveState.resetSeconds);
      const parts = [used, reset];
      if (exhausted) {
        parts.push("exhausted");
      } else {
        if (paceStr) parts.push(`Δ${paceStr}`);
        if (forecast) parts.push(forecast);
      }
      item._summaryLabel.set_text(`${label}: ${parts.join("  ")}`);

      const usageTint = staleAgeSeconds != null ? "stale" : tintFor(pace, liveState.usedPercent, isShort);
      this._setQuotaBar(
        item._quotaBar,
        liveState.usedPercent == null ? null : liveState.usedPercent / 100,
        elapsedFraction(liveState),
        usageTint
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
