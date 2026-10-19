package main

import (
	"net/http"
	"strings"

	"google.golang.org/grpc"
)

// mountGRPC serves gRPC on the HTTP server's listener: an HTTP/2 request of content type
// application/grpc goes to grpcServer and every other request to the server's own handler. The
// listener takes cleartext HTTP/2 beside HTTP/1.1 for it.
//
// The gRPC server never serves a listener of its own, so only the HTTP server's Shutdown stops it:
// grpc.Server.GracefulStop cannot drain calls served this way.
func mountGRPC(server *http.Server, grpcServer *grpc.Server) {
	rest := server.Handler
	server.Handler = http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.ProtoMajor == 2 && strings.HasPrefix(r.Header.Get("Content-Type"), "application/grpc") {
			grpcServer.ServeHTTP(w, r)
			return
		}
		rest.ServeHTTP(w, r)
	})
	server.Protocols = new(http.Protocols)
	server.Protocols.SetHTTP1(true)
	server.Protocols.SetUnencryptedHTTP2(true)
}
