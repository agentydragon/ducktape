import GLib from "gi://GLib";
import GObject from "gi://GObject";
import Gio from "gi://Gio";
import St from "gi://St";
import Clutter from "gi://Clutter";

import { Extension } from "resource:///org/gnome/shell/extensions/extension.js";
import * as Main from "resource:///org/gnome/shell/ui/main.js";
import * as PanelMenu from "resource:///org/gnome/shell/ui/panelMenu.js";
import * as PopupMenu from "resource:///org/gnome/shell/ui/popupMenu.js";

const POLL_INTERVAL_SECONDS = 120;
const STALE_AFTER_SECONDS = 5 * 60;

// Pace deviation thresholds, in signed percentage points (used% − expected%).
const PACE_COOL_BELOW = -10;
const PACE_WARN_ABOVE = 5;
const PACE_HOT_ABOVE = 15;
const SHORT_WIN_HOT_PERCENT = 85;
const STABLE_FRACTION = 0.05;

const TINT_CLASSES = [
  "quota-cool",
  "quota-ok",
  "quota-warn",
  "quota-hot",
  "quota-unknown",
  "quota-stale",
  "quota-error",
];
const TINT_RANK = { unknown: 0, stale: 0, ok: 1, cool: 1, warn: 2, hot: 3 };

function decodeBytes(bytes) {
  return new TextDecoder().decode(typeof bytes.get_data === "function" ? bytes.get_data() : bytes);
}

function errorMessage(error) {
  return error?.message ?? String(error);
}

function aiQuotaBinPath(extensionPath) {
  const override = GLib.getenv("AI_QUOTA_BIN");
  if (override) return override;
  const sibling = `${extensionPath}/aiquota`;
  if (GLib.file_test(sibling, GLib.FileTest.IS_EXECUTABLE)) return sibling;
  return "aiquota";
}

function executablePath(path) {
  if (path.includes("/")) return path;
  return GLib.find_program_in_path(path) ?? path;
}

function formatDuration(seconds) {
  if (seconds == null || !Number.isFinite(seconds)) return "?";
  const s = Math.max(0, Math.round(seconds));
  const d = Math.floor(s / 86400);
  const h = Math.floor((s % 86400) / 3600);
  const m = Math.floor((s % 3600) / 60);
  if (d > 0) return `${d}d${h}h`;
  if (h > 0) return `${h}h${m}m`;
  return `${m}m`;
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
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) return `${s}s`;
  return formatDuration(s);
}

// Pick the state to render: prefer the latest fetch, but fall back to the
// last successful snapshot (windows + extraUsage) when the latest call
// returned nothing usable. `staleAge` is null when no fallback was needed.
function effectiveState(state) {
  if (state.short != null || state.long != null) {
    return { short: state.short, long: state.long, extraUsage: state.extraUsage, staleAge: null };
  }
  const snap = state.lastSuccess;
  if (!snap || (snap.short == null && snap.long == null)) {
    return { short: null, long: null, extraUsage: null, staleAge: null };
  }
  const ageSeconds = snap.fetchedAt != null ? Math.max(0, (Date.now() - snap.fetchedAt) / 1000) : null;
  return { short: snap.short, long: snap.long, extraUsage: snap.extraUsage, staleAge: ageSeconds };
}

function formatFreshness(lastFetch) {
  if (lastFetch == null) return "no successful refresh yet";
  const ageSeconds = Math.max(0, (Date.now() - lastFetch) / 1000);
  const age = ageSeconds < 60 ? `${Math.round(ageSeconds)}s` : formatDuration(ageSeconds);
  return `${isStaleFetch(lastFetch) ? "stale, " : ""}updated ${age} ago`;
}

// Pure pace computation. See DESIGN.md ("Pace math") for derivation.
function computePace({ usedPercent, resetSeconds, windowSeconds }) {
  if (usedPercent == null || resetSeconds == null || windowSeconds == null || windowSeconds <= 0) {
    return null;
  }
  const elapsedSecs = windowSeconds - resetSeconds;
  const elapsedFrac = elapsedSecs / windowSeconds;
  const expectedPercent = elapsedFrac * 100;
  const deviation = usedPercent - expectedPercent;
  let projectedAtReset = null;
  let secondsToExhaust = null;
  if (elapsedSecs > 0 && usedPercent > 0) {
    const ratePerSec = usedPercent / elapsedSecs;
    secondsToExhaust = (100 - usedPercent) / ratePerSec;
    projectedAtReset = usedPercent + ratePerSec * resetSeconds;
  }
  const stable = elapsedFrac > STABLE_FRACTION && elapsedFrac < 1 - STABLE_FRACTION;
  return { elapsedFrac, deviation, projectedAtReset, secondsToExhaust, stable };
}

function tintFor({ pace, usedPercent, isShort }) {
  if (usedPercent == null) return "unknown";
  if (isShort && usedPercent >= SHORT_WIN_HOT_PERCENT) return "hot";
  if (!pace || !pace.stable) {
    if (usedPercent >= 95) return "hot";
    if (usedPercent >= 80) return "warn";
    return "ok";
  }
  if (pace.deviation >= PACE_HOT_ABOVE) return "hot";
  if (pace.deviation >= PACE_WARN_ABOVE) return "warn";
  if (pace.deviation <= PACE_COOL_BELOW) return "cool";
  return "ok";
}

// Hot short window always wins (urgent throttle). Otherwise take the worse of
// the two tints, with the long window's "ok"/"cool" as the default.
function bindingTint(shortTint, longTint) {
  if (shortTint === "hot") return "hot";
  return TINT_RANK[shortTint] > TINT_RANK[longTint] ? shortTint : longTint;
}

function formatPace(pace) {
  if (!pace || !pace.stable) return null;
  const sign = pace.deviation >= 0 ? "+" : "−";
  return `${sign}${Math.abs(Math.round(pace.deviation))}%`;
}

function formatForecast(pace, resetSeconds) {
  if (!pace || !pace.stable || pace.projectedAtReset == null) return null;
  const projected = pace.projectedAtReset;
  if (projected > 100.5) {
    const shortfall = resetSeconds - pace.secondsToExhaust;
    return `exhausts ~${formatDuration(shortfall)} before reset`;
  }
  if (projected < 95) {
    return `leaves ~${Math.round(100 - projected)}% unused at reset`;
  }
  return "on pace";
}

function formatCompactDollars(usd) {
  if (usd >= 1000) {
    const k = usd / 1000;
    return `$${k >= 10 ? Math.round(k) : Math.round(k * 10) / 10}k`;
  }
  return `$${Math.round(usd)}`;
}

// The "currently burning extra" decision lives in the Python view model
// (see aiquota/render/view_model.py) and is delivered to us by
// `aiquota gnome-extension-json` as state.currentlyOverPlan. Don't
// reintroduce a local copy: see aiquota/AGENTS.md.
function formatExtraUsage(extra) {
  if (!extra || !extra.is_enabled || !(extra.used_usd > 0)) return null;
  const used = extra.used_usd;
  const limit = extra.monthly_limit_usd;
  const pct = Math.round(extra.utilization);
  return `extra $${Math.round(used)}/$${Math.round(limit)} (${pct}%) this month`;
}

function clamp01(value) {
  if (value == null || !Number.isFinite(value)) return null;
  return Math.max(0, Math.min(1, value));
}

function elapsedFraction(state) {
  if (state?.resetSeconds == null || state?.windowSeconds == null || state.windowSeconds <= 0) return null;
  return clamp01((state.windowSeconds - state.resetSeconds) / state.windowSeconds);
}

function emptyProviderState() {
  return {
    short: null,
    long: null,
    lastFetch: null,
    error: null,
    extraUsage: null,
    currentlyOverPlan: false,
    extraStatus: "none",
    // Last successful fetch — populated when the most recent attempt failed
    // but a prior good snapshot exists. {short, long, extraUsage, fetchedAt}.
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
      this._binPath = aiQuotaBinPath(extension.path);
      this._execPath = executablePath(this._binPath);
      this._popupTickId = null;
      this._refreshInFlight = false;
      this._refreshProc = null;
      this._destroyed = false;

      const fixturePath = GLib.getenv("AI_QUOTA_FIXTURE");
      if (fixturePath) {
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

      // Show all providers; Python config.toml controls which are enabled.
      const shows = {};
      for (const { id } of PROVIDER_DEFS) shows[id] = true;
      this._initUI(shows);
      this._refresh();
      this._timerId = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, POLL_INTERVAL_SECONDS, () => {
        this._refresh();
        return GLib.SOURCE_CONTINUE;
      });
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
        shortRow: null,
        longRow: null,
      }));

      this._buildPanel();
      this._buildPopup();
      this._menuOpenId = this.menu.connect("open-state-changed", (_menu, open) => {
        if (open) {
          this._renderPopup();
          this._startPopupTick();
        } else {
          this._stopPopupTick();
        }
      });
    }

    _loadFixtureData(data) {
      // Test fixtures specify currentlyOverPlan / extraStatus explicitly so
      // we never re-derive policy on the JS side (see aiquota/AGENTS.md).
      const loadLastSuccess = (snap) => {
        if (!snap) return null;
        // Fixtures express snapshot age as `ageSeconds` (relative to "now")
        // so the rendered "(stale Xm)" tag is stable across test runs.
        const fetchedAt = snap.ageSeconds != null ? Date.now() - snap.ageSeconds * 1000 : (snap.fetchedAt ?? null);
        return {
          short: snap.short ?? null,
          long: snap.long ?? null,
          extraUsage: snap.extraUsage ?? null,
          fetchedAt,
        };
      };
      const provider = (node) => ({
        short: node?.short ?? null,
        long: node?.long ?? null,
        lastFetch: node?.lastFetch != null ? Date.now() : null,
        error: node?.error ?? null,
        extraUsage: node?.extraUsage ?? null,
        currentlyOverPlan: node?.currentlyOverPlan === true,
        extraStatus: node?.extraStatus ?? "none",
        lastSuccess: loadLastSuccess(node?.lastSuccess),
      });
      for (const p of this._providers) p.state = provider(data[p.id]);
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
          '<method name="OpenMenu"/>' +
          '<method name="CloseMenu"/>' +
          '<method name="GetMenuGeometry"><arg type="(iiii)" direction="out" name="rect"/></method>' +
          "</interface></node>",
        {
          Reload: (path) => {
            const [ok, bytes] = GLib.file_get_contents(path);
            if (!ok) throw new Error(`fixture not readable: ${path}`);
            this._loadFixtureData(JSON.parse(decodeBytes(bytes)));
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
      for (const p of this._providers) {
        p.header = new PopupMenu.PopupSeparatorMenuItem(p.label);
        p.shortRow = this._makeQuotaRow("5h");
        p.longRow = this._makeQuotaRow("7d");
        this.menu.addMenuItem(p.header);
        this.menu.addMenuItem(p.shortRow);
        this.menu.addMenuItem(p.longRow);
        p.header.label.add_style_class_name("quota-popup-header");
      }
    }

    _makeQuotaRow(label) {
      const item = new PopupMenu.PopupBaseMenuItem({ reactive: false, can_focus: false });
      item.add_style_class_name("quota-popup-bar-item");

      const content = new St.BoxLayout({
        style_class: "quota-popup-bar-content",
        vertical: true,
        x_expand: true,
      });
      item._summaryLabel = new St.Label({
        text: `${label}: no data`,
        style_class: "quota-popup-row",
        x_expand: true,
      });
      item._bars = new St.BoxLayout({
        style_class: "quota-bars",
        vertical: true,
        x_expand: true,
      });

      const timeBar = this._makeQuotaBar("quota-bar-time");
      const usageBar = this._makeQuotaBar("quota-unknown");
      item._timeFill = timeBar.fill;
      item._usageFill = usageBar.fill;

      item._bars.add_child(timeBar.track);
      item._bars.add_child(usageBar.track);
      content.add_child(item._summaryLabel);
      content.add_child(item._bars);
      item.add_child(content);
      return item;
    }

    _makeQuotaBar(fillClass) {
      const track = new St.BoxLayout({ style_class: "quota-bar-track", x_expand: true });

      const fill = new St.Widget({ style_class: `quota-bar-fill ${fillClass}` });
      fill._quotaFraction = null;
      fill._quotaTrack = track;
      fill.set_width(0);
      track.connect("notify::allocation", () => this._applyBarFill(fill));
      track.add_child(fill);
      return { track, fill };
    }

    _mapWindow(w) {
      if (!w) return null;
      const resetAtMs = w.reset_at ? new Date(w.reset_at).getTime() : null;
      return {
        usedPercent: w.used_percent ?? null,
        resetAtMs,
        resetSeconds: w.reset_seconds ?? null,
        windowSeconds: w.window_seconds ?? null,
      };
    }

    _mapExtraUsage(extra) {
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
        short: this._mapWindow(snap.result.short_window),
        long: this._mapWindow(snap.result.long_window),
        extraUsage: this._mapExtraUsage(snap.result.extra_usage),
        fetchedAt: snap.fetched_at ? new Date(snap.fetched_at).getTime() : null,
      };
    }

    _loadSubprocessData(data) {
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
        p.state = {
          short: isSuccess ? this._mapWindow(result.short_window) : null,
          long: isSuccess ? this._mapWindow(result.long_window) : null,
          lastFetch: fetchedAt,
          error: isSuccess ? null : (result.error ?? null),
          extraUsage: isSuccess ? this._mapExtraUsage(result.extra_usage) : null,
          // Derived policy bits from the Python view model — single source of truth.
          currentlyOverPlan: pq.currently_over_plan === true,
          extraStatus: pq.extra_status ?? "none",
          lastSuccess: this._mapLastSuccess(pq.last_success),
        };
      }
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
      for (const p of this._providers) this._renderProvider(p.state, p.icon, p.paceLabel);
    }

    _renderProvider(state, icon, paceLabel) {
      const { short, long, staleAge } = effectiveState(state);
      if (state.error && short == null && long == null) {
        this._setTint(icon, paceLabel, "error");
        paceLabel.set_text("!");
        return;
      }
      if (short == null && long == null) {
        this._setTint(icon, paceLabel, "unknown");
        paceLabel.set_text("");
        return;
      }
      const shortState = withLiveReset(short);
      const longState = withLiveReset(long);
      const shortPace = shortState ? computePace(shortState) : null;
      const longPace = longState ? computePace(longState) : null;
      const shortTint = shortState
        ? tintFor({ pace: shortPace, usedPercent: shortState.usedPercent, isShort: true })
        : "unknown";
      const longTint = longState
        ? tintFor({ pace: longPace, usedPercent: longState.usedPercent, isShort: false })
        : "unknown";
      const overPlan = state.currentlyOverPlan === true;
      const stale = staleAge != null || isStaleFetch(state.lastFetch);
      const tint = overPlan ? "hot" : stale ? "stale" : bindingTint(shortTint, longTint);
      this._setTint(icon, paceLabel, tint);
      const paceText = formatPace(longPace) ?? "";
      if (overPlan) {
        paceLabel.set_text(`${formatCompactDollars(state.extraUsage.used_usd)} ⚡`);
      } else {
        paceLabel.set_text(paceText);
      }
    }

    _renderPopup() {
      for (const p of this._providers) {
        this._renderProviderHeader(p.header, p.label, p.state);
        if (p.state.currentlyOverPlan === true) {
          const { short, long, staleAge } = effectiveState(p.state);
          p.shortRow.visible = true;
          p.longRow.visible = false;
          this._renderExtraActiveRow(p.shortRow, short, long, staleAge);
        } else {
          const { short, long, staleAge } = effectiveState(p.state);
          p.shortRow.visible = true;
          p.longRow.visible = true;
          this._renderPopupRow(p.shortRow, "5h", short, staleAge);
          this._renderPopupRow(p.longRow, "7d", long, staleAge);
        }
      }
    }

    _renderProviderHeader(item, title, state) {
      item.label.remove_style_class_name("quota-popup-header-error");
      item.label.remove_style_class_name("quota-popup-header-stale");

      const { short, long, extraUsage } = effectiveState(state);
      const haveWindows = short != null || long != null;
      const parts = [title];
      if (state.error) {
        // "last refresh failed" makes more sense when stale rows render below;
        // "error" is the standalone form when no fallback is available either.
        const prefix = haveWindows ? "last refresh failed" : "error";
        parts.push(`${prefix}: ${state.error}`);
        item.label.add_style_class_name("quota-popup-header-error");
      } else if (isStaleFetch(state.lastFetch)) {
        item.label.add_style_class_name("quota-popup-header-stale");
      }
      const extraStr = formatExtraUsage(extraUsage);
      if (extraStr) parts.push(extraStr);
      parts.push(formatFreshness(state.lastFetch));
      item.label.set_text(parts.join(" · "));
    }

    _renderExtraActiveRow(item, short, long, staleAgeSeconds) {
      item._bars.visible = false;
      this._setBarFill(item._timeFill, null);
      this._setBarFill(item._usageFill, null);
      this._setBarTint(item._usageFill, "unknown");
      const parts = [this._formatExtraActiveWindow("5h", short), this._formatExtraActiveWindow("7d", long)];
      if (staleAgeSeconds != null) parts.push(`(stale ${formatAge(staleAgeSeconds)})`);
      item._summaryLabel.set_text(parts.join("  "));
    }

    _formatExtraActiveWindow(label, state) {
      if (state == null) return `${label}: no data`;
      const liveState = withLiveReset(state);
      const used = liveState.usedPercent != null ? `${Math.round(liveState.usedPercent)}%` : "?";
      return `${label}: ${used} ↻${formatDuration(liveState.resetSeconds)}`;
    }

    _renderPopupRow(item, label, state, staleAgeSeconds) {
      item._bars.visible = true;
      if (state == null) {
        item._summaryLabel.set_text(`${label}: no data`);
        this._setBarFill(item._timeFill, null);
        this._setBarFill(item._usageFill, null);
        this._setBarTint(item._usageFill, "unknown");
        return;
      }
      const liveState = withLiveReset(state);
      const pace = computePace(liveState);
      const used = liveState.usedPercent != null ? `${Math.round(liveState.usedPercent)}%` : "?";
      const reset = `↻${formatDuration(liveState.resetSeconds)}`;
      const paceStr = formatPace(pace);
      const forecast = formatForecast(pace, liveState.resetSeconds);
      const parts = [used, reset];
      if (paceStr) parts.push(`Δ${paceStr}`);
      if (forecast) parts.push(forecast);
      if (staleAgeSeconds != null) parts.push(`(stale ${formatAge(staleAgeSeconds)})`);
      item._summaryLabel.set_text(`${label}: ${parts.join("  ")}`);

      this._setBarFill(item._timeFill, elapsedFraction(liveState));
      this._setBarFill(item._usageFill, liveState.usedPercent == null ? null : liveState.usedPercent / 100);
      const usageTint =
        staleAgeSeconds != null
          ? "stale"
          : tintFor({ pace, usedPercent: liveState.usedPercent, isShort: label === "5h" });
      this._setBarTint(item._usageFill, usageTint);
    }

    _setBarFill(fill, fraction) {
      fill._quotaFraction = clamp01(fraction);
      this._applyBarFill(fill);
    }

    _applyBarFill(fill) {
      const fraction = fill._quotaFraction;
      if (fraction == null) {
        fill.set_width(0);
        return;
      }
      const box = fill._quotaTrack.get_allocation_box();
      const trackWidth = box.x2 - box.x1;
      if (!(trackWidth > 0)) return;
      fill.set_width(Math.round(trackWidth * fraction));
    }

    _setBarTint(fill, tint) {
      for (const cls of TINT_CLASSES) fill.remove_style_class_name(cls);
      fill.add_style_class_name(`quota-${tint}`);
    }

    _startPopupTick() {
      if (this._popupTickId) return;
      this._popupTickId = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, 1, () => {
        if (!this.menu.isOpen) {
          this._popupTickId = null;
          return GLib.SOURCE_REMOVE;
        }
        this._renderPopup();
        return GLib.SOURCE_CONTINUE;
      });
    }

    _stopPopupTick() {
      if (!this._popupTickId) return;
      GLib.source_remove(this._popupTickId);
      this._popupTickId = null;
    }

    _refresh() {
      if (this._refreshInFlight) return;
      this._refreshInFlight = true;

      let proc;
      try {
        proc = Gio.Subprocess.new(
          [this._execPath, "gnome-extension-json"],
          Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_PIPE
        );
      } catch (e) {
        this._refreshInFlight = false;
        console.error(`[aiquota] spawn ${this._binPath} threw: ${errorMessage(e)}`);
        for (const p of this._providers) p.state.error = errorMessage(e);
        this._renderPanel();
        this._renderPopup();
        return;
      }

      this._refreshProc = proc;
      proc.communicate_utf8_async(null, null, (_proc, res) => {
        this._refreshInFlight = false;
        if (this._refreshProc === proc) this._refreshProc = null;
        if (this._destroyed) return;

        try {
          const [, stdout, stderr] = proc.communicate_utf8_finish(res);
          const exitStatus = proc.get_if_exited() ? proc.get_exit_status() : "signal";
          if (!proc.get_successful()) {
            const stderrText = stderr ?? "";
            console.warn(`[aiquota] ${this._binPath} exited ${exitStatus}: ${stderrText}`);
            for (const p of this._providers) p.state.error = `aiquota exited ${exitStatus}`;
          } else {
            const data = JSON.parse(stdout);
            this._loadSubprocessData(data);
          }
        } catch (e) {
          console.error(`[aiquota] refresh ${this._binPath} failed: ${errorMessage(e)}`);
          for (const p of this._providers) p.state.error = errorMessage(e);
        }

        this._renderPanel();
        this._renderPopup();
      });
    }

    destroy() {
      this._destroyed = true;
      if (this._refreshProc) {
        this._refreshProc.force_exit();
        this._refreshProc = null;
      }
      this._refreshInFlight = false;
      this._unexportTestInterface();
      this._stopPopupTick();
      if (this._menuOpenId) {
        this.menu.disconnect(this._menuOpenId);
        this._menuOpenId = null;
      }
      if (this._timerId) {
        GLib.source_remove(this._timerId);
        this._timerId = null;
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
