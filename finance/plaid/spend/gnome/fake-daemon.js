import { FakeViewDaemon } from "./gnome-fake-daemon.js";
import GLib from "gi://GLib";
const name = "works.allegedly.PlaidSpend";
const path = "/works/allegedly/PlaidSpend";
const controlName = "works.allegedly.PlaidSpendFake";
const controlPath = "/works/allegedly/PlaidSpendFake";
const daemon = new FakeViewDaemon({
  name,
  path,
  controlName,
  controlPath,
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
