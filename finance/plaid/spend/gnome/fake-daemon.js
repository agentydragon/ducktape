import { FakeViewDaemon } from "./gnome-fake-daemon.js";
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
const daemon = new FakeViewDaemon({
  name,
  path,
  controlName,
  controlPath,
  mainXml,
  controlXml,
  initialView: JSON.stringify({
    generated_at: "2026-10-18T12:00:00Z",
    cards: [{ spend_minor_units: 1000, currency: "USD" }],
  }),
});
daemon.run(
  { Status: "ready", LastError: "" },
  {
    SetTiming(lastAttemptAt, nextRetryAt) {
      daemon.setTiming("works.allegedly.PlaidSpend1", lastAttemptAt, nextRetryAt);
    },
  }
);
