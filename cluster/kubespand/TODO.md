# kubespand TODOs

## Future API Capabilities

kubespand currently lacks Talos machined's diagnostic RPCs. The probe gRPC server
in qemu_tests is a test-only workaround. Eventually kubespand should expose its
own API:

- [ ] **Ping RPC** — Talos machined has no Ping; kubespand could add one
- [ ] **Exec/Shell RPC** — remote command execution (Talos lacks this too)
- [ ] **Diagnostics RPC** — dump WG/nftables/routing state on demand
- [ ] **PacketCapture** — Talos has this via machined; useful for debugging
- [ ] **Netstat** — Talos has this via machined; useful for debugging
- [ ] **apid-compatible gRPC interface** — so existing Talos tooling works

These would replace the test-only probe server with a proper RPC interface
built into kubespand itself.
