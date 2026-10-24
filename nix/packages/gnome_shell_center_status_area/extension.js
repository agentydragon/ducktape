import GLib from "gi://GLib";
import St from "gi://St";
import { Extension } from "resource:///org/gnome/shell/extensions/extension.js";
import * as Main from "resource:///org/gnome/shell/ui/main.js";

// GNOME Shell has no setting for this placement; it relies on its private panel
// boxes and moves late-registered right-box actors after extensions load.
export default class CenterStatusAreaExtension extends Extension {
  enable() {
    this._rightBox = Main.panel._rightBox;
    this._centerBox = Main.panel._centerBox;
    this._centerGroup = new St.BoxLayout({
      style_class: this._rightBox.get_style_class_name(),
    });
    this._centerBox.add_child(this._centerGroup);

    this._moveItems();
    this._timer = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, 1, () => {
      this._moveItems();
      return GLib.SOURCE_CONTINUE;
    });
  }

  _moveItems() {
    for (const child of this._rightBox.get_children()) {
      this._rightBox.remove_child(child);
      this._centerGroup.add_child(child);
    }
  }

  disable() {
    if (this._timer) {
      GLib.Source.remove(this._timer);
      this._timer = 0;
    }

    if (this._centerGroup) {
      for (const child of this._centerGroup.get_children()) {
        this._centerGroup.remove_child(child);
        this._rightBox.add_child(child);
      }
      this._centerBox.remove_child(this._centerGroup);
      this._centerGroup.destroy();
      this._centerGroup = null;
    }

    this._rightBox = null;
    this._centerBox = null;
  }
}
