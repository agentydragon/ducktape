import Adw from "gi://Adw";
import Gio from "gi://Gio";

import { ExtensionPreferences } from "resource:///org/gnome/Shell/Extensions/js/extensions/prefs.js";

export default class QuotaPreferences extends ExtensionPreferences {
  fillPreferencesWindow(window) {
    const settings = this.getSettings();
    const page = new Adw.PreferencesPage({ title: "Panel", icon_name: "view-more-symbolic" });
    const group = new Adw.PreferencesGroup({ title: "Pace" });
    const row = new Adw.SwitchRow({ title: "Show “pp” after pace", subtitle: "On: +21 pp · Off: +21" });
    settings.bind("panel-show-pace-unit", row, "active", Gio.SettingsBindFlags.DEFAULT);
    group.add(row);
    page.add(group);
    window.add(page);
  }
}
