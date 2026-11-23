import { FakeViewDaemon } from "./gnome-fake-daemon.js";
const name = "works.allegedly.AiQuota";
const path = "/works/allegedly/AiQuota";
const controlName = "works.allegedly.AiQuotaFake";
const controlPath = "/works/allegedly/AiQuotaFake";
const daemon = new FakeViewDaemon({
  name,
  path,
  controlName,
  controlPath,
  initialView: JSON.stringify({ fetched_at: "2026-10-18T12:00:00Z", providers: [] }),
});
daemon.run({ Status: "connected", Error: "" });
