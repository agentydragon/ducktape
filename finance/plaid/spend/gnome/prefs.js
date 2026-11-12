import Adw from "gi://Adw";
import Gio from "gi://Gio";
import Gdk from "gi://Gdk";
import Gtk from "gi://Gtk";

import { ExtensionPreferences } from "resource:///org/gnome/Shell/Extensions/js/extensions/prefs.js";

const ITEMS = [
  ["status", "Status icon"],
  ["balance", "Available balance"],
  ["pace-window", "Pace window"],
  ["pace", "Pace as percentage of leash"],
  ["sparkline", "Rolling pace sparkline"],
];

function orderedItems(settings) {
  const ids = ITEMS.map(([id]) => id);
  const saved = settings.get_strv("panel-order");
  return [...new Set([...saved.filter((id) => ids.includes(id)), ...ids])];
}

export default class PlaidSpendPreferences extends ExtensionPreferences {
  fillPreferencesWindow(window) {
    window.set_default_size(640, 760);
    const settings = this.getSettings();
    const page = new Adw.PreferencesPage({ title: "Panel", icon_name: "view-more-symbolic" });
    const group = new Adw.PreferencesGroup({
      title: "Panel items",
      description: "Choose and arrange the compact items shown in the top bar.",
    });
    const windowGroup = new Adw.PreferencesGroup({ title: "Pace window" });
    const windowRow = new Adw.ComboRow({
      title: "Rolling average",
      model: Gtk.StringList.new(["7 days", "30 days"]),
      selected: settings.get_string("pace-window") === "30d" ? 1 : 0,
    });
    windowRow.connect("notify::selected", () =>
      settings.set_string("pace-window", windowRow.selected === 1 ? "30d" : "7d")
    );
    windowGroup.add(windowRow);
    const sparklineGroup = new Adw.PreferencesGroup({ title: "Sparkline" });
    const days = [7, 14, 30];
    const historyRow = new Adw.ComboRow({
      title: "Time shown",
      subtitle: "Separate from the rolling average period",
      model: Gtk.StringList.new(days.map((value) => `${value} days`)),
      selected: days.indexOf(Number(settings.get_string("sparkline-history-days"))),
    });
    historyRow.connect("notify::selected", () => {
      if (days[historyRow.selected] !== undefined)
        settings.set_string("sparkline-history-days", String(days[historyRow.selected]));
    });
    sparklineGroup.add(historyRow);
    const anchorRow = new Adw.SwitchRow({
      title: "Start vertical scale at zero",
      subtitle: "Turn off to fit the displayed history",
    });
    settings.bind("sparkline-anchor-zero", anchorRow, "active", Gio.SettingsBindFlags.DEFAULT);
    sparklineGroup.add(anchorRow);
    const leashRow = new Adw.SwitchRow({ title: "Show the daily leash line" });
    settings.bind("sparkline-show-leash", leashRow, "active", Gio.SettingsBindFlags.DEFAULT);
    sparklineGroup.add(leashRow);
    const colorRow = new Adw.ActionRow({ title: "Leash line color", subtitle: "Defaults to the panel text color" });
    const colorButton = new Gtk.ColorDialogButton({
      dialog: new Gtk.ColorDialog({ title: "Leash line color", with_alpha: false }),
      valign: Gtk.Align.CENTER,
    });
    const resetColor = new Gtk.Button({ label: "Reset", valign: Gtk.Align.CENTER });
    let updatingColor = false;
    const updateColor = () => {
      const color = new Gdk.RGBA();
      if (!color.parse(settings.get_string("sparkline-leash-color"))) color.parse("#ffffff");
      updatingColor = true;
      colorButton.rgba = color;
      updatingColor = false;
    };
    colorButton.connect("notify::rgba", () => {
      if (updatingColor) return;
      const color = colorButton.rgba;
      const hex = [color.red, color.green, color.blue]
        .map((component) =>
          Math.round(component * 255)
            .toString(16)
            .padStart(2, "0")
        )
        .join("");
      settings.set_string("sparkline-leash-color", `#${hex}`);
    });
    resetColor.connect("clicked", () => settings.reset("sparkline-leash-color"));
    settings.bind("sparkline-show-leash", colorRow, "sensitive", Gio.SettingsBindFlags.GET);
    colorRow.add_suffix(colorButton);
    colorRow.add_suffix(resetColor);
    sparklineGroup.add(colorRow);
    page.add(group);
    page.add(windowGroup);
    page.add(sparklineGroup);
    window.add(page);

    let rows = [];
    const rebuild = () => {
      for (const row of rows) group.remove(row);
      rows = [];
      const order = orderedItems(settings);
      const visible = settings.get_strv("panel-visible");
      sparklineGroup.sensitive = visible.includes("sparkline");
      for (const [index, id] of order.entries()) {
        const title = ITEMS.find(([item]) => item === id)?.[1] ?? id;
        const row = new Adw.ActionRow({ title });
        const enabled = new Gtk.Switch({ active: visible.includes(id), valign: Gtk.Align.CENTER });
        enabled.connect("notify::active", () => {
          const selected = new Set(settings.get_strv("panel-visible"));
          if (enabled.active) selected.add(id);
          else selected.delete(id);
          settings.set_strv("panel-visible", [...selected]);
        });
        for (const [delta, icon, tooltip] of [
          [-1, "go-up-symbolic", "Move up"],
          [1, "go-down-symbolic", "Move down"],
        ]) {
          const button = new Gtk.Button({ icon_name: icon, tooltip_text: tooltip, valign: Gtk.Align.CENTER });
          button.sensitive = index + delta >= 0 && index + delta < order.length;
          button.connect("clicked", () => {
            const updated = [...order];
            [updated[index], updated[index + delta]] = [updated[index + delta], updated[index]];
            settings.set_strv("panel-order", updated);
          });
          row.add_suffix(button);
        }
        row.add_suffix(enabled);
        group.add(row);
        rows.push(row);
      }
    };
    const handlers = [
      settings.connect("changed::panel-order", rebuild),
      settings.connect("changed::panel-visible", rebuild),
      settings.connect("changed::pace-window", () => {
        windowRow.selected = settings.get_string("pace-window") === "30d" ? 1 : 0;
      }),
      settings.connect("changed::sparkline-history-days", () => {
        historyRow.selected = days.indexOf(Number(settings.get_string("sparkline-history-days")));
      }),
      settings.connect("changed::sparkline-leash-color", updateColor),
    ];
    // GSettings reports changes only for keys read after connecting a handler.
    rebuild();
    windowRow.selected = settings.get_string("pace-window") === "30d" ? 1 : 0;
    historyRow.selected = days.indexOf(Number(settings.get_string("sparkline-history-days")));
    updateColor();
    window.connect("close-request", () => {
      for (const handler of handlers) settings.disconnect(handler);
      return false;
    });
  }
}
