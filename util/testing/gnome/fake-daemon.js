import Gio from "gi://Gio";
import GLib from "gi://GLib";

export class FakeViewDaemon {
  constructor({ name, path, controlName, controlPath, mainXml, controlXml, initialView }) {
    this.name = name;
    this.path = path;
    this.controlName = controlName;
    this.controlPath = controlPath;
    this.mainXml = mainXml;
    this.controlXml = controlXml;
    this.currentView = initialView;
    this.lastAttemptAt = 0;
    this.nextRetryAt = 0;
    this.getCalls = 0;
    this.replies = 0;
    this.pendingViews = [];
    this.nameOwner = 0;
    this.controlOwner = 0;
    this.loop = GLib.MainLoop.new(null, false);
  }

  acquireName() {
    if (!this.nameOwner)
      this.nameOwner = Gio.bus_own_name(Gio.BusType.SESSION, this.name, Gio.BusNameOwnerFlags.NONE, null, null, null);
  }

  releaseName() {
    if (this.nameOwner) {
      Gio.bus_unown_name(this.nameOwner);
      this.nameOwner = 0;
    }
  }

  setTiming(interfaceName, lastAttemptAt, nextRetryAt) {
    this.lastAttemptAt = lastAttemptAt;
    this.nextRetryAt = nextRetryAt;
    Gio.DBus.session.emit_signal(
      null,
      this.path,
      "org.freedesktop.DBus.Properties",
      "PropertiesChanged",
      new GLib.Variant("(sa{sv}as)", [
        interfaceName,
        {
          LastAttemptAt: new GLib.Variant("d", lastAttemptAt),
          NextRetryAt: new GLib.Variant("d", nextRetryAt),
        },
        [],
      ])
    );
  }

  run(properties, controlMethods = {}) {
    const daemon = this;
    const service = Gio.DBusExportedObject.wrapJSObject(this.mainXml, {
      ...properties,
      GetViewAsync(_parameters, invocation) {
        daemon.getCalls += 1;
        daemon.pendingViews.push({ invocation, view: daemon.currentView });
      },
      get LastAttemptAt() {
        return daemon.lastAttemptAt;
      },
      get NextRetryAt() {
        return daemon.nextRetryAt;
      },
    });
    service.export(Gio.DBus.session, this.path);
    const control = Gio.DBusExportedObject.wrapJSObject(this.controlXml, {
      ...controlMethods,
      SetView(view) {
        daemon.currentView = view;
        service.emit_signal("ViewChanged", new GLib.Variant("(s)", [view]));
      },
      SetViewSilently(view) {
        daemon.currentView = view;
      },
      ReleaseGetView() {
        const pending = daemon.pendingViews.shift();
        if (!pending) throw new Error("no GetView reply is pending");
        daemon.replies += 1;
        pending.invocation.return_value(new GLib.Variant("(s)", [pending.view]));
      },
      ReleaseName() {
        daemon.releaseName();
      },
      AcquireName() {
        daemon.acquireName();
      },
      GetCounters() {
        return [daemon.getCalls, daemon.replies, daemon.pendingViews.length];
      },
      Stop() {
        GLib.idle_add(GLib.PRIORITY_DEFAULT_IDLE, () => {
          daemon.releaseName();
          Gio.bus_unown_name(daemon.controlOwner);
          daemon.loop.quit();
          return GLib.SOURCE_REMOVE;
        });
      },
    });
    control.export(Gio.DBus.session, this.controlPath);
    this.acquireName();
    this.controlOwner = Gio.bus_own_name(
      Gio.BusType.SESSION,
      this.controlName,
      Gio.BusNameOwnerFlags.NONE,
      null,
      null,
      null
    );
    this.loop.run();
  }
}
