import Adw from "gi://Adw";
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
    page.add(group);
    page.add(windowGroup);
    window.add(page);

    let rows = [];
    const rebuild = () => {
      for (const row of rows) group.remove(row);
      rows = [];
      const order = orderedItems(settings);
      const visible = settings.get_strv("panel-visible");
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
    rebuild();
    const handlers = [
      settings.connect("changed::panel-order", rebuild),
      settings.connect("changed::panel-visible", rebuild),
      settings.connect("changed::pace-window", () => {
        windowRow.selected = settings.get_string("pace-window") === "30d" ? 1 : 0;
      }),
    ];
    window.connect("close-request", () => {
      for (const handler of handlers) settings.disconnect(handler);
      return false;
    });
  }
}
