# Cold guest-root image import

The profile compares Python stream buffering with CPU in the `ctr` client and containerd. It records measurements, exact source bases, and patch hashes. [Producer and client profile](cold_import_producer.md)

- Producer comparison. [Control patch](cold_import_control.patch), [1 MiB buffer candidate](cold_import_buffer_candidate.patch)
- [Client profiling patch](cold_import_client_profile.patch)
- [Go symbol-table base override used to symbolize the client capture](cold_import_gosymtab_base.patch)
