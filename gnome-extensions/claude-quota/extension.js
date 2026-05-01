import GLib from "gi://GLib";
import GObject from "gi://GObject";
import Gio from "gi://Gio";
import St from "gi://St";
import Clutter from "gi://Clutter";
import Soup from "gi://Soup";
import Secret from "gi://Secret";

import { Extension } from "resource:///org/gnome/shell/extensions/extension.js";
import * as Main from "resource:///org/gnome/shell/ui/main.js";
import * as PanelMenu from "resource:///org/gnome/shell/ui/panelMenu.js";
import * as PopupMenu from "resource:///org/gnome/shell/ui/popupMenu.js";

const CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage";
const CODEX_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage";
const POLL_INTERVAL_SECONDS = 120;
const STALE_AFTER_SECONDS = 5 * 60;

// Pace deviation thresholds, in signed percentage points (used% − expected%).
// TODO: expose via gschema settings (along with poll interval and the
// short-window override threshold below).
const PACE_COOL_BELOW = -10;
const PACE_WARN_ABOVE = 5;
const PACE_HOT_ABOVE = 15;
const SHORT_WIN_HOT_PERCENT = 85;
// Pace deviation is too noisy in the first/last sliver of a window; fall back
// to absolute-usage tinting and suppress pace numerals when within these edges.
const STABLE_FRACTION = 0.05;

// Window total lengths. Codex returns `limit_window_seconds`; Claude does not,
// so the short/long lengths are constants matching the published windows.
const CLAUDE_SHORT_W = 5 * 3600;
const CLAUDE_LONG_W = 7 * 86400;
const CODEX_SHORT_W_FALLBACK = 3600;
const CODEX_LONG_W_FALLBACK = 86400;

// keyring crate (used by Codex CLI) stores entries under the generic schema
// with attributes {service, username}. We search by service only.
const CODEX_KEYRING_SCHEMA = new Secret.Schema("org.freedesktop.Secret.Generic", Secret.SchemaFlags.DONT_MATCH_NAME, {
  service: Secret.SchemaAttributeType.STRING,
  username: Secret.SchemaAttributeType.STRING,
});

const TINT_CLASSES = ["quota-cool", "quota-ok", "quota-warn", "quota-hot", "quota-unknown", "quota-stale"];
const TINT_RANK = { unknown: 0, stale: 0, ok: 1, cool: 1, warn: 2, hot: 3 };

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

function secondsUntil(isoTimestamp) {
  if (!isoTimestamp) return null;
  return (new Date(isoTimestamp) - Date.now()) / 1000;
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

const QuotaIndicator = GObject.registerClass(
  class QuotaIndicator extends PanelMenu.Button {
    _init(extension) {
      super._init(0.0, "AI Quota Tracker", false);

      this._iconsDir = `${extension.path}/icons`;
      this._claude = { short: null, long: null, lastFetch: null };
      this._codex = { short: null, long: null, lastFetch: null };
      this._httpSession = new Soup.Session();

      this._buildPanel();
      this._buildPopup();

      this._refresh();
      this._timerId = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, POLL_INTERVAL_SECONDS, () => {
        this._refresh();
        return GLib.SOURCE_CONTINUE;
      });
    }

    _buildPanel() {
      const box = new St.BoxLayout({
        style_class: "quota-indicator",
        y_align: Clutter.ActorAlign.CENTER,
      });
      this._claudeIcon = this._makeIcon("claude-symbolic.svg");
      this._claudePace = new St.Label({
        style_class: "quota-pace",
        y_align: Clutter.ActorAlign.CENTER,
      });
      this._codexIcon = this._makeIcon("openai-symbolic.svg");
      this._codexPace = new St.Label({
        style_class: "quota-pace",
        y_align: Clutter.ActorAlign.CENTER,
      });
      const claudeBox = new St.BoxLayout({
        style_class: "quota-provider",
        y_align: Clutter.ActorAlign.CENTER,
      });
      claudeBox.add_child(this._claudeIcon);
      claudeBox.add_child(this._claudePace);
      const codexBox = new St.BoxLayout({
        style_class: "quota-provider",
        y_align: Clutter.ActorAlign.CENTER,
      });
      codexBox.add_child(this._codexIcon);
      codexBox.add_child(this._codexPace);
      box.add_child(claudeBox);
      box.add_child(codexBox);
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
      this._claudeHeader = new PopupMenu.PopupSeparatorMenuItem("Claude");
      this._claudeShort = new PopupMenu.PopupMenuItem("burst …", { reactive: false });
      this._claudeLong = new PopupMenu.PopupMenuItem("weekly …", { reactive: false });
      this._codexHeader = new PopupMenu.PopupSeparatorMenuItem("Codex");
      this._codexShort = new PopupMenu.PopupMenuItem("primary …", { reactive: false });
      this._codexLong = new PopupMenu.PopupMenuItem("secondary …", { reactive: false });
      const items = [
        this._claudeHeader,
        this._claudeShort,
        this._claudeLong,
        this._codexHeader,
        this._codexShort,
        this._codexLong,
      ];
      for (const item of items) this.menu.addMenuItem(item);
      for (const item of [this._claudeShort, this._claudeLong, this._codexShort, this._codexLong]) {
        item.label.add_style_class_name("quota-popup-row");
      }
    }

    _readClaudeToken() {
      try {
        const path = `${GLib.get_home_dir()}/.claude/.credentials.json`;
        const [ok, bytes] = GLib.file_get_contents(path);
        if (!ok) return null;
        const creds = JSON.parse(new TextDecoder().decode(bytes));
        return creds?.claudeAiOauth?.accessToken ?? null;
      } catch {
        return null;
      }
    }

    _readCodexAuth() {
      // File-based auth (~/.codex/auth.json) — Codex CLI writes this when
      // Secret Service is unavailable (common on headless/NixOS setups).
      try {
        const path = `${GLib.get_home_dir()}/.codex/auth.json`;
        const [ok, bytes] = GLib.file_get_contents(path);
        if (ok) {
          const auth = JSON.parse(new TextDecoder().decode(bytes));
          const token = auth?.tokens?.access_token ?? null;
          const accountId = auth?.tokens?.account_id ?? null;
          if (token) return { token, accountId };
        }
      } catch {
        // fall through to keyring
      }
      try {
        const results = Secret.password_search_sync(
          CODEX_KEYRING_SCHEMA,
          { service: "Codex Auth" },
          Secret.SearchFlags.UNLOCK | Secret.SearchFlags.LOAD_SECRETS,
          null
        );
        if (!results?.length) return null;
        const token = results[0].get_secret()?.get_text() ?? null;
        return token ? { token, accountId: null } : null;
      } catch {
        return null;
      }
    }

    _fetchAsync(url, headers, onSuccess) {
      const msg = Soup.Message.new("GET", url);
      for (const [k, v] of Object.entries(headers)) msg.request_headers.append(k, v);
      this._httpSession.send_and_read_async(msg, GLib.PRIORITY_DEFAULT, null, (session, result) => {
        try {
          const bytes = session.send_and_read_finish(result);
          const json = JSON.parse(new TextDecoder().decode(bytes.get_data()));
          onSuccess(json);
        } catch {
          // leave state unchanged, show stale data until next poll
        }
        this._renderPanel();
        this._renderPopup();
      });
    }

    _fetchClaude(token) {
      this._fetchAsync(
        CLAUDE_USAGE_URL,
        {
          Authorization: `Bearer ${token}`,
          "anthropic-beta": "oauth-2025-04-20",
        },
        (data) => {
          this._claude.short = windowFromClaude(data.five_hour, CLAUDE_SHORT_W);
          this._claude.long = windowFromClaude(data.seven_day, CLAUDE_LONG_W);
          this._claude.lastFetch = Date.now();
        }
      );
    }

    _fetchCodex({ token, accountId }) {
      const headers = {
        Authorization: `Bearer ${token}`,
        "User-Agent": "codex_cli_rs/0.125.0 (Linux; x86_64) gnome-shell-extension",
      };
      if (accountId) headers["ChatGPT-Account-Id"] = accountId;
      this._fetchAsync(CODEX_USAGE_URL, headers, (data) => {
        this._codex.short = windowFromCodex(data.rate_limit?.primary_window, CODEX_SHORT_W_FALLBACK);
        this._codex.long = windowFromCodex(data.rate_limit?.secondary_window, CODEX_LONG_W_FALLBACK);
        this._codex.lastFetch = Date.now();
      });
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
      this._renderProvider(this._claude, this._claudeIcon, this._claudePace);
      this._renderProvider(this._codex, this._codexIcon, this._codexPace);
    }

    _renderProvider(state, icon, paceLabel) {
      if (state.short == null && state.long == null) {
        this._setTint(icon, paceLabel, "unknown");
        paceLabel.set_text("");
        return;
      }
      const stale = state.lastFetch != null && (Date.now() - state.lastFetch) / 1000 > STALE_AFTER_SECONDS;
      const shortPace = state.short ? computePace(state.short) : null;
      const longPace = state.long ? computePace(state.long) : null;
      const shortTint = state.short
        ? tintFor({ pace: shortPace, usedPercent: state.short.usedPercent, isShort: true })
        : "unknown";
      const longTint = state.long
        ? tintFor({ pace: longPace, usedPercent: state.long.usedPercent, isShort: false })
        : "unknown";
      const tint = stale ? "stale" : bindingTint(shortTint, longTint);
      this._setTint(icon, paceLabel, tint);
      paceLabel.set_text(formatPace(longPace) ?? "");
    }

    _renderPopup() {
      this._renderPopupRow(this._claudeShort, "burst (5h)", this._claude.short);
      this._renderPopupRow(this._claudeLong, "weekly (7d)", this._claude.long);
      this._renderPopupRow(this._codexShort, "primary", this._codex.short);
      this._renderPopupRow(this._codexLong, "secondary", this._codex.long);
    }

    _renderPopupRow(item, label, state) {
      if (state == null) {
        item.label.set_text(`${label}: no data`);
        return;
      }
      const pace = computePace(state);
      const used = state.usedPercent != null ? `${Math.round(state.usedPercent)}%` : "?";
      const reset = `↻${formatDuration(state.resetSeconds)}`;
      const paceStr = formatPace(pace);
      const forecast = formatForecast(pace, state.resetSeconds);
      const parts = [used, reset];
      if (paceStr) parts.push(`Δ${paceStr}`);
      if (forecast) parts.push(forecast);
      item.label.set_text(`${label}: ${parts.join("  ")}`);
    }

    _refresh() {
      const claudeToken = this._readClaudeToken();
      if (claudeToken) this._fetchClaude(claudeToken);

      const codexAuth = this._readCodexAuth();
      if (codexAuth) this._fetchCodex(codexAuth);

      this._renderPanel();
      this._renderPopup();
    }

    destroy() {
      if (this._timerId) {
        GLib.source_remove(this._timerId);
        this._timerId = null;
      }
      this._httpSession.abort();
      super.destroy();
    }
  }
);

function windowFromClaude(node, windowSeconds) {
  if (!node) return null;
  return {
    usedPercent: node.utilization ?? null,
    resetSeconds: node.resets_at ? Math.max(0, secondsUntil(node.resets_at)) : null,
    windowSeconds,
  };
}

function windowFromCodex(node, fallbackWindowSeconds) {
  if (!node) return null;
  return {
    usedPercent: node.used_percent ?? null,
    resetSeconds: node.reset_after_seconds ?? null,
    windowSeconds: node.limit_window_seconds ?? fallbackWindowSeconds,
  };
}

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
