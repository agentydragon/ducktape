import Adw from "gi://Adw";
import Gio from "gi://Gio";
import Gdk from "gi://Gdk";
import Gtk from "gi://Gtk";

import { ExtensionPreferences } from "resource:///org/gnome/Shell/Extensions/js/extensions/prefs.js";
import { panelOrder } from "./panelOrder.js";

const ITEM_TITLES = {
  status: "Status icon",
  balance: "Available balance",
  "pace-window": "Pace window",
  pace: "Pace as percentage of leash",
  sparkline: "Rolling pace sparkline",
};

export default class PlaidSpendPreferences extends ExtensionPreferences {
  fillPreferencesWindow(window) {
    window.set_default_size(640, 760);
    const settings = this.getSettings();
    const handlers = [];
    function dayChoice(key, properties) {
      const [, choices] = settings.settings_schema.get_key(key).get_range().recursiveUnpack();
      const row = new Adw.ComboRow({
        ...properties,
        model: Gtk.StringList.new(choices.map((value) => `${parseInt(value, 10)} days`)),
      });
      const read = () => {
        row.selected = choices.indexOf(settings.get_string(key));
      };
      handlers.push(settings.connect(`changed::${key}`, read));
      read();
      row.connect("notify::selected", () => {
        if (choices[row.selected] !== undefined) settings.set_string(key, choices[row.selected]);
      });
      return row;
    }
    const page = new Adw.PreferencesPage({ title: "Panel", icon_name: "view-more-symbolic" });
    const group = new Adw.PreferencesGroup({
      title: "Panel items",
      description: "Choose and arrange the compact items shown in the top bar.",
    });
    const windowGroup = new Adw.PreferencesGroup({ title: "Pace window" });
    windowGroup.add(dayChoice("pace-window", { title: "Rolling average" }));
    const sparklineGroup = new Adw.PreferencesGroup({ title: "Sparkline" });
    sparklineGroup.add(
      dayChoice("sparkline-history-days", {
        title: "Time shown",
        subtitle: "Separate from the rolling average period",
      })
    );
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
      const order = panelOrder(settings);
      const visible = settings.get_strv("panel-visible");
      sparklineGroup.sensitive = visible.includes("sparkline");
      for (const [index, id] of order.entries()) {
        const row = new Adw.ActionRow({ title: ITEM_TITLES[id] ?? id });
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
    handlers.push(
      settings.connect("changed::panel-order", rebuild),
      settings.connect("changed::panel-visible", rebuild),
      settings.connect("changed::sparkline-leash-color", updateColor)
    );
    // GSettings reports changes only for keys read after connecting a handler.
    rebuild();
    updateColor();
    window.connect("close-request", () => {
      for (const handler of handlers) settings.disconnect(handler);
      return false;
    });
  }
}
