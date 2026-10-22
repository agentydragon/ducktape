import Gio from "gi://Gio";
import GLib from "gi://GLib";
const name = "works.allegedly.AiQuota";
const path = "/works/allegedly/AiQuota";
const controlName = "works.allegedly.AiQuotaFake";
const controlPath = "/works/allegedly/AiQuotaFake";
const mainXml = `<node><interface name="works.allegedly.AiQuota1">
  <method name="GetView"><arg name="view" type="s" direction="out"/></method>
  <signal name="ViewChanged"><arg name="view" type="s"/></signal>
  <property name="Status" type="s" access="read"/>
  <property name="Error" type="s" access="read"/>
  <property name="LastAttemptAt" type="d" access="read"/>
  <property name="NextRetryAt" type="d" access="read"/>
</interface></node>`;
const controlXml = `<node><interface name="works.allegedly.AiQuotaFake">
  <method name="SetView"><arg name="view" type="s" direction="in"/></method>
  <method name="SetViewSilently"><arg name="view" type="s" direction="in"/></method>
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
let currentView = JSON.stringify({ fetched_at: "2026-10-18T12:00:00Z", providers: [] });
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
    return "connected";
  },
  get Error() {
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
