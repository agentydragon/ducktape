# TCP listeners at process boundaries

`netlistener.Open(spec)` accepts a TCP `host:port` address or `fd:N` for an inherited,
already-listening TCP socket. `N` is a descriptor in the child process, at least 3.
The function consumes an inherited descriptor and returns an owned `net.Listener`.
Invalid descriptors fail startup; there is no fallback bind.

Binary startup acquires listeners, defers their cleanup, and passes `net.Listener`
values to serving code. Follow Go's `ListenAndServe`/`Serve` separation. One address
setting selects either binding or inheritance; do not add parallel FD override fields.

A parent binds first, passes the socket when spawning, then closes its copy. Go's
`exec.Cmd.ExtraFiles` assigns child descriptors `3+i`; Python's `pass_fds` preserves
the supplied descriptor numbers. The launcher builds `fd:N` using the child mapping.
The parent's typed endpoint already supplies client addresses, so no startup-file
or log parsing is needed. Listening does not establish application readiness:
launchers still check the service's protocol before yielding it.
