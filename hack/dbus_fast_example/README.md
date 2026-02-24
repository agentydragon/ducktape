# DBus-fast example

This folder contains a minimal example showing how to use
[dbus-fast](https://github.com/agoode/dbus-fast) to:

- run a small service that exposes a method and a signal
- listen to that signal from a client
- unsubscribe from the signal
- gracefully handle the service going away and a replacement appearing

Tests spin up a private `dbus-daemon`, launch the service in a separate
process and drive it via a helper `ServiceManager`.
