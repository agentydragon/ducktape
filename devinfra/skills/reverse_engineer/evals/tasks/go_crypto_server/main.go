package main

import (
	"flag"
	"fmt"
	"net/http"
	"os"
)

func main() {
	addr := flag.String("addr", "127.0.0.1:8080", "listen address")
	flag.Parse()
	srv := newServer()
	fmt.Fprintf(os.Stderr, "ncs %s listening on %s\n", protocolVersion, *addr)
	if err := http.ListenAndServe(*addr, srv.routes()); err != nil {
		fmt.Fprintf(os.Stderr, "server error: %v\n", err)
		os.Exit(1)
	}
}
