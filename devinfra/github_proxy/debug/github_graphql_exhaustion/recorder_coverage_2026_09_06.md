# GitHub recorder coverage

Investigation: [GitHub issue #5213](https://github.com/agentydragon/ducktape/issues/5213).

The connection recorder cannot exclude activity on an already established TCP
connection. Its TCP probes emit only on `TCP_SYN_SENT`; HTTP/2 can carry further
requests on that socket without another event. Connection counts and TCP byte
counts do not measure GraphQL request cost.

## Established TCP counters

`github-api-socket-snapshot` records iproute2 `ss` output every 30 seconds under
the existing `ducktape.githubApiRecorder.enable` host opt-in. Each snapshot includes
the host, boot ID, network namespace, socket identity, UID, owning processes,
cgroup and cumulative TCP counters. Begin/end markers distinguish a completed
enumeration from a failed or timed-out one. The service has a 10-second timeout.

`/var/log/tcp-connect-recorder/sockets.log` is `root:users`, mode `0640`, in a
`0750` directory. It shares the connection log's daily compression and configured
retention, defaulting to 14 rotations. One live sample on `wyrm2` contained 57
sockets in 60,607 bytes: about 175 MB/day before compression at the configured
interval. This is a sample-based volume estimate, not a storage cap.

The sampler runs in the host network namespace. It includes host-network pods;
it does not enumerate separate pod network namespaces. It can miss a socket that
opens and closes between samples, and the last bytes before a sampled socket
closes. The existing event recorder complements those gaps. Capturing connections
does not recover missing byte counters or request-level attribution.

For a socket present in adjacent complete snapshots, compare `bytes_sent`,
`bytes_acked`, and `bytes_received` with the same host, boot, namespace, endpoints,
`ino` and `sk`. Counters are cumulative, so the initial sample cannot place earlier
traffic inside the measurement window. The `users` field lists current socket
owners; a shared socket does not identify which owner sent a particular byte.

## Bounded live proof

At `2026-09-06T00:18:08Z`, an owned `socat` client on `wyrm2` connected to an owned
loopback listener. The socket was established before running the new collector.
After sending 1,024 bytes, the collector reported `bytes_sent:1024`; after another
8,192 bytes on the same socket, it reported `bytes_sent:9216`. Both snapshots
identified PID `3338427`, inode `37306558`, socket cookie `929136`, and the same
process cgroup. The active connection recorder contained exactly one event for
that client. The experiment terminated both temporary processes and changed no
existing service. Raw local snapshots: `/tmp/github-socket-proof.1nfR4t/`.

This validates established-socket activity capture on the live host. Deployment
and attribution of the account's quota consumption remain separate work.
Nix evaluation confirmed the service configuration for both existing opt-ins,
`wyrm2` and `rugged`, plus the timer, log permissions and rotation on `wyrm2`.

## Other proven or source-visible gaps

- At `2026-09-06T00:12:50Z`, four loopback UDP senders exercised connected and
  unconnected IPv4 and IPv6 sockets. The active recorder logged only connected
  IPv4 PID `3306833`. Unconnected IPv4 PID `3306835` and IPv6 PIDs `3306837` and
  `3306839` produced no records. The source hooks `udp_sendmsg` only and reads the
  socket's connected peer, ignoring `msg_name` supplied by `sendto`/`sendmsg`.
  These are coverage defects, not evidence that GitHub API traffic uses QUIC.
- UDP deduplication keys are PID/address pairs and are never cleared. They cannot
  locate later activity in time and can suppress a reused PID/address pair.
  Bpftrace's deployed default permits 4,096 map keys; the script provides no map
  occupancy or failed-update counter. A failed insertion can defeat deduplication
  and increase output rather than reliably dropping the new peer.
- The service sends tracer errors to the journal, but has no durable heartbeat,
  lost-event metric or alert. A bounded journal search found no matching loss,
  overflow or tracer warning on `2026-09-04` through the proof window; absence of
  such a message is not proof of complete capture. The new snapshot markers show
  collector execution, not eBPF delivery health.

The UDP patch requires live validation of both destination extraction and the
IPv6 hook. Starting a separate tracer, including its parse/debug mode, requires
privileges unavailable through current passwordless access. The active tracer was
left running without modification.
