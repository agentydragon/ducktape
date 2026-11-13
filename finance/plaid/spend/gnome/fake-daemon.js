import { FakeViewDaemon } from "./gnome-fake-daemon.js";
import GLib from "gi://GLib";
const name = "works.allegedly.PlaidSpend";
const path = "/works/allegedly/PlaidSpend";
const controlName = "works.allegedly.PlaidSpendFake";
const controlPath = "/works/allegedly/PlaidSpendFake";
const mainXml = `<node><interface name="works.allegedly.PlaidSpend1">
  <method name="GetView"><arg name="view" type="s" direction="out"/></method>
  <method name="GetPaceHistory"><arg name="history" type="s" direction="out"/></method>
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
  <method name="SetHistory"><arg name="history" type="s" direction="in"/></method>
  <method name="ReleaseGetHistory"/>
  <method name="GetHistoryPending"><arg name="pending" type="s" direction="out"/></method>
  <method name="ReleaseName"/>
  <method name="AcquireName"/>
  <method name="GetCounters">
    <arg name="calls" type="u" direction="out"/>
    <arg name="replies" type="u" direction="out"/>
    <arg name="pending" type="u" direction="out"/>
  </method>
  <method name="Stop"/>
</interface></node>`;
const daemon = new FakeViewDaemon({
  name,
  path,
  controlName,
  controlPath,
  mainXml,
  controlXml,
  initialView: JSON.stringify({
    generated_at: "2026-10-15T12:00:00Z",
    cards: [{ spend_minor_units: 1000, currency: "USD" }],
  }),
});
let history = "null";
const pendingHistory = [];
daemon.run(
  {
    Status: "ready",
    LastError: "",
    GetPaceHistoryAsync(_parameters, invocation) {
      pendingHistory.push({ invocation, history });
    },
  },
  {
    SetHistory(value) {
      history = value;
    },
    GetHistoryPending() {
      return String(pendingHistory.length);
    },
    ReleaseGetHistory() {
      const pending = pendingHistory.shift();
      if (!pending) throw new Error("no GetPaceHistory reply is pending");
      pending.invocation.return_value(new GLib.Variant("(s)", [pending.history]));
    },
    SetTiming(lastAttemptAt, nextRetryAt) {
      daemon.setTiming("works.allegedly.PlaidSpend1", lastAttemptAt, nextRetryAt);
    },
  }
);
