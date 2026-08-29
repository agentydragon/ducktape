import Gio from "gi://Gio";
import GLib from "gi://GLib";
import Gtk from "gi://Gtk?version=3.0";
import WebKit2 from "gi://WebKit2?version=4.1";

const APPLICATION_ID = "works.allegedly.HakuApprovals";
const DEFAULT_CONSOLE_URL = "https://haku.allegedly.works";
const APPROVALS_PATH = "/_console/approvals-embed";
const WINDOW_WIDTH = 560;
const WINDOW_HEIGHT = 820;

const scriptPath = GLib.filename_from_uri(import.meta.url)[0];
const scriptDirectory = GLib.path_get_dirname(scriptPath);

let window = null;
let webView = null;
let backgroundMode = false;

function consoleUrl() {
  const configured = GLib.getenv("HAKU_CONSOLE_URL") || DEFAULT_CONSOLE_URL;
  return `${configured.replace(/\/+$/, "")}${APPROVALS_PATH}`;
}

function pendingCount(title) {
  const match = /^(?:Approvals) \((\d+)\) · Haku$/.exec(title || "");
  return match ? Number(match[1]) : null;
}

function updateVisibility() {
  if (!window || !backgroundMode || !webView) return;
  const count = pendingCount(webView.get_title());
  if (count === null) return;
  if (count > 0) window.present();
  else window.hide();
}

function ensureWindow(application) {
  if (window) return;

  window = new Gtk.ApplicationWindow({
    application,
    title: "Haku Approvals",
  });
  window.set_default_size(WINDOW_WIDTH, WINDOW_HEIGHT);
  window.set_icon_from_file(`${scriptDirectory}/logo.svg`);
  window.connect("delete-event", () => {
    // Closing the window keeps the authenticated webview alive in the background so the next
    // approval can raise it again. The panel button's --show command switches to manual mode.
    backgroundMode = true;
    window.hide();
    return true;
  });

  webView = new WebKit2.WebView();
  webView.set_hexpand(true);
  webView.set_vexpand(true);
  webView.connect("notify::title", () => {
    const title = webView.get_title();
    if (title) window.set_title(title);
    updateVisibility();
  });
  window.add(webView);
  webView.load_uri(consoleUrl());
  window.show_all();
  updateVisibility();
}

const application = new Gtk.Application({
  application_id: APPLICATION_ID,
  flags: Gio.ApplicationFlags.HANDLES_COMMAND_LINE,
});

application.connect("activate", () => {
  backgroundMode = false;
  ensureWindow(application);
  window.present();
});

application.connect("command-line", (_application, commandLine) => {
  const args = commandLine.get_arguments().slice(1);
  const show = args.includes("--show");
  backgroundMode = args.includes("--background") && !show;
  ensureWindow(application);
  if (show || !backgroundMode) window.present();
  else window.hide();
  return 0;
});

application.run([GLib.get_prgname(), ...ARGV]);
