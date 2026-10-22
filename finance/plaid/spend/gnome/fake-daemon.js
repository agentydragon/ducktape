import Gio from "gi://Gio";
import GLib from "gi://GLib";
const name = "works.allegedly.PlaidSpend";
const path = "/works/allegedly/PlaidSpend";
const controlName = "works.allegedly.PlaidSpendFake";
const controlPath = "/works/allegedly/PlaidSpendFake";
const mainXml = `<node><interface name="works.allegedly.PlaidSpend1">
  <method name="GetView"><arg name="view" type="s" direction="out"/></method>
  <method name="Login"/>
  <signal name="ViewChanged"><arg name="view" type="s"/></signal>
  <property name="Status" type="s" access="read"/>
  <property name="LastError" type="s" access="read"/>
  <property name="LastAttemptAt" type="d" access="read"/>
  <property name="NextRetryAt" type="d" access="read"/>
</interface></node>`;
const controlXml = `<node><interface name="works.allegedly.PlaidSpendFake">
  <method name="SetView"><arg name="view" type="s" direction="in"/></method>
  <method name="SetViewSilently"><arg name="view" type="s" direction="in"/></method>
  <method name="SetTiming">
    <arg name="last_attempt_at" type="d" direction="in"/>
    <arg name="next_retry_at" type="d" direction="in"/>
  </method>
  <method name="ReleaseGetView"/>
  <method name="ReleaseName"/>
  <method name="AcquireName"/>
  <method name="GetCounters">
    <arg name="calls" type="u" direction="out"/>
    <arg name="replies" type="u" direction="out"/>
    <arg name="pending" type="u" direction="out"/>
  </method>
  <method name="Stop"/>
</interface></node>`;
let currentView = JSON.stringify({
  generated_at: "2026-10-18T12:00:00Z",
  cards: [{ spend_minor_units: 1000, currency: "USD" }],
});
let lastAttemptAt = 0;
let nextRetryAt = 0;
let getCalls = 0;
let replies = 0;
let pendingViews = [];
let nameOwner = 0;
let controlOwner = 0;
const loop = GLib.MainLoop.new(null, false);
const service = Gio.DBusExportedObject.wrapJSObject(mainXml, {
  GetViewAsync(_parameters, invocation) {
    getCalls += 1;
    pendingViews.push({ invocation, view: currentView });
  },
  get Status() {
    return "ready";
  },
  get LastError() {
    return "";
  },
  get LastAttemptAt() {
    return lastAttemptAt;
  },
  get NextRetryAt() {
    return nextRetryAt;
  },
});
service.export(Gio.DBus.session, path);
const control = Gio.DBusExportedObject.wrapJSObject(controlXml, {
  SetView(view) {
    currentView = view;
    service.emit_signal("ViewChanged", new GLib.Variant("(s)", [view]));
  },
  SetTiming(lastAttemptValue, nextRetryValue) {
    lastAttemptAt = lastAttemptValue;
    nextRetryAt = nextRetryValue;
    Gio.DBus.session.emit_signal(
      null,
      path,
      "org.freedesktop.DBus.Properties",
      "PropertiesChanged",
      new GLib.Variant("(sa{sv}as)", [
        "works.allegedly.PlaidSpend1",
        {
          LastAttemptAt: new GLib.Variant("d", lastAttemptAt),
          NextRetryAt: new GLib.Variant("d", nextRetryAt),
        },
        [],
      ])
    );
  },
  SetViewSilently(view) {
    currentView = view;
  },
  ReleaseGetView() {
    const pending = pendingViews.shift();
    if (!pending) throw new Error("no GetView reply is pending");
    replies += 1;
    pending.invocation.return_value(new GLib.Variant("(s)", [pending.view]));
  },
  ReleaseName() {
    if (nameOwner) {
      Gio.bus_unown_name(nameOwner);
      nameOwner = 0;
    }
  },
  AcquireName() {
    if (!nameOwner)
      nameOwner = Gio.bus_own_name(Gio.BusType.SESSION, name, Gio.BusNameOwnerFlags.NONE, null, null, null);
  },
  GetCounters() {
    return [getCalls, replies, pendingViews.length];
  },
  Stop() {
    GLib.idle_add(GLib.PRIORITY_DEFAULT_IDLE, () => {
      if (nameOwner) Gio.bus_unown_name(nameOwner);
      if (controlOwner) Gio.bus_unown_name(controlOwner);
      loop.quit();
      return GLib.SOURCE_REMOVE;
    });
  },
});
control.export(Gio.DBus.session, controlPath);
nameOwner = Gio.bus_own_name(Gio.BusType.SESSION, name, Gio.BusNameOwnerFlags.NONE, null, null, null);
controlOwner = Gio.bus_own_name(Gio.BusType.SESSION, controlName, Gio.BusNameOwnerFlags.NONE, null, null, null);
loop.run();
