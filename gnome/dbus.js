import Gio from "gi://Gio";

const interfaces = Gio.File.new_for_uri(import.meta.url)
  .get_parent()
  .get_child("dbus");

export function loadInterfaceXml(filename) {
  const [, bytes] = interfaces.get_child(filename).load_contents(null);
  return new TextDecoder().decode(bytes);
}
