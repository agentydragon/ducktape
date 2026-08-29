import GLib from "gi://GLib";
import GObject from "gi://GObject";
import Gio from "gi://Gio";
import St from "gi://St";

import { Extension } from "resource:///org/gnome/shell/extensions/extension.js";
import * as Main from "resource:///org/gnome/shell/ui/main.js";
import * as PanelMenu from "resource:///org/gnome/shell/ui/panelMenu.js";

const DESK_COMMAND = "haku-approvals";

function launcherPath(extensionPath) {
  const sibling = `${extensionPath}/${DESK_COMMAND}`;
  if (GLib.file_test(sibling, GLib.FileTest.IS_EXECUTABLE)) return sibling;
  return GLib.find_program_in_path(DESK_COMMAND) ?? DESK_COMMAND;
}

const ApprovalIndicator = GObject.registerClass(
  class ApprovalIndicator extends PanelMenu.Button {
    _init(extension) {
      super._init(0.0, "Haku Approvals", false);
      this._launcher = launcherPath(extension.path);
      this._destroyed = false;

      this.add_child(
        new St.Icon({
          gicon: Gio.icon_new_for_string(`${extension.path}/logo.svg`),
          style_class: "system-status-icon haku-approvals-indicator",
        })
      );

      // PanelMenu.Button normally opens a popup menu. Redirect that one click to the dedicated
      // GTK window so the approvals surface never lands in the default browser.
      this.menu.connect("open-state-changed", (_menu, open) => {
        if (!open || this._destroyed) return;
        this.menu.close();
        this._launch("--show");
      });
    }

    _launch(mode) {
      try {
        Gio.Subprocess.new([this._launcher, mode], Gio.SubprocessFlags.NONE);
      } catch (error) {
        console.error(`[haku-approvals] unable to launch ${this._launcher}: ${error?.message ?? error}`);
      }
    }

    startBackground() {
      this._launch("--background");
    }

    destroy() {
      this._destroyed = true;
      super.destroy();
    }
  }
);

export default class HakuApprovalsExtension extends Extension {
  enable() {
    this._indicator = new ApprovalIndicator(this);
    Main.panel.addToStatusArea(this.uuid, this._indicator);
    this._indicator.startBackground();
  }

  disable() {
    this._indicator?.destroy();
    this._indicator = null;
  }
}
