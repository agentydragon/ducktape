import { FakeViewDaemon } from "./gnome-fake-daemon.js";
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
const daemon = new FakeViewDaemon({
  name,
  path,
  controlName,
  controlPath,
  mainXml,
  controlXml,
  initialView: JSON.stringify({ fetched_at: "2026-10-18T12:00:00Z", providers: [] }),
});
daemon.run({ Status: "connected", Error: "" });
