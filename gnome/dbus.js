import Gio from "gi://Gio";

const interfaces = Gio.File.new_for_uri(import.meta.url)
  .get_parent()
  .get_child("dbus");

export function loadInterfaceXml(filename) {
  const [, bytes] = interfaces.get_child(filename).load_contents(null);
  return new TextDecoder().decode(bytes);
}

// A getter has one outstanding call. A newer read, a ViewChanged signal, or an
// owner change can supersede it before Gio delivers either its value or its error.
export class DBusGetter {
  constructor(method, timeoutMs = -1) {
    this.method = method;
    this.timeoutMs = timeoutMs;
    this._pending = null;
  }

  cancel() {
    const pending = this._pending;
    this._pending = null;
    pending?.cancel();
  }

  read(proxy, onValue, onError) {
    this.cancel();
    const owner = proxy?.g_name_owner;
    if (!owner) return;
    const pending = new Gio.Cancellable();
    this._pending = pending;
    proxy.call(this.method, null, Gio.DBusCallFlags.NONE, this.timeoutMs, pending, (source, result) => {
      const current = this._pending === pending && owner === source.g_name_owner;
      if (this._pending === pending) this._pending = null;
      try {
        const reply = source.call_finish(result);
        if (current) onValue(reply.deep_unpack()[0]);
      } catch (error) {
        if (current) onError(error);
      }
    });
  }
}
