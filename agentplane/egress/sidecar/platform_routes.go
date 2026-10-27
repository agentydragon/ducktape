package main

import (
	"context"
	"fmt"
	"io"
	"net"
	"os"
	"sort"
	"strings"
	"time"

	"github.com/agentydragon/ducktape/agentplane/observability"
	"github.com/agentydragon/ducktape/util/netlistener"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/keepalive"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
)

type platformRoute struct {
	name   string
	config platformRouteConfig
	conn   *grpc.ClientConn
	server *grpc.Server
	listen net.Listener
}

type routeForwarder struct {
	conn      *grpc.ClientConn
	tokenFile string
}

func bindPlatformRoutes(routes map[string]platformRouteConfig) ([]*platformRoute, error) {
	return bindPlatformRoutesWithKeepalive(routes, keepalive.ClientParameters{
		Time:                time.Minute,
		Timeout:             10 * time.Second,
		PermitWithoutStream: false,
	})
}

func bindPlatformRoutesWithKeepalive(
	routes map[string]platformRouteConfig,
	keepaliveParameters keepalive.ClientParameters,
) ([]*platformRoute, error) {
	names := make([]string, 0, len(routes))
	for name := range routes {
		names = append(names, name)
	}
	sort.Strings(names)
	started := make([]*platformRoute, 0, len(names))
	for _, name := range names {
		config := routes[name]
		listener, err := netlistener.Open(config.ListenAddress)
		if err != nil {
			stopPlatformRoutes(started)
			return nil, fmt.Errorf("listen for platform route %q: %w", name, err)
		}
		conn, err := grpc.NewClient(
			config.Upstream,
			observability.GRPCClientOption(),
			grpc.WithTransportCredentials(insecure.NewCredentials()),
			grpc.WithKeepaliveParams(keepaliveParameters),
		)
		if err != nil {
			_ = listener.Close()
			stopPlatformRoutes(started)
			return nil, fmt.Errorf("create upstream for platform route %q: %w", name, err)
		}
		forwarder := &routeForwarder{conn: conn, tokenFile: config.TokenFile}
		server := grpc.NewServer(
			observability.GRPCServerOption(),
			grpc.UnknownServiceHandler(forwarder.forward),
			grpc.ForceServerCodec(rawCodec{}),
		)
		route := &platformRoute{name: name, config: config, conn: conn, server: server, listen: listener}
		started = append(started, route)
	}
	return started, nil
}

func stopPlatformRoutes(routes []*platformRoute) {
	for i := len(routes) - 1; i >= 0; i-- {
		routes[i].server.Stop()
		_ = routes[i].conn.Close()
		_ = routes[i].listen.Close()
	}
}

func (f *routeForwarder) forward(_ any, workload grpc.ServerStream) error {
	method, okay := grpc.MethodFromServerStream(workload)
	if !okay {
		return status.Error(codes.Internal, "gRPC method is unavailable")
	}
	var token string
	if f.tokenFile != "" {
		var err error
		token, err = readToken(f.tokenFile)
		if err != nil {
			return status.Errorf(codes.Unavailable, "egress sidecar has no token at %s", f.tokenFile)
		}
	}
	incoming, _ := metadata.FromIncomingContext(workload.Context())
	outgoing := metadata.MD{}
	for key, values := range incoming {
		if key == "authorization" || strings.HasPrefix(key, ":") || strings.HasPrefix(key, "grpc-") {
			continue
		}
		outgoing[key] = append([]string(nil), values...)
	}
	if token != "" {
		outgoing.Set("authorization", "Bearer "+token)
	}

	ctx, cancel := context.WithCancel(metadata.NewOutgoingContext(workload.Context(), outgoing))
	defer cancel()
	upstream, err := f.conn.NewStream(
		ctx,
		&grpc.StreamDesc{ClientStreams: true, ServerStreams: true},
		method,
		grpc.ForceCodec(rawCodec{}),
	)
	if err != nil {
		return err
	}
	requestError := make(chan error, 1)
	go func() {
		for {
			var request []byte
			if err := workload.RecvMsg(&request); err != nil {
				if err == io.EOF {
					requestError <- upstream.CloseSend()
					return
				}
				requestError <- err
				cancel()
				return
			}
			if err := upstream.SendMsg(request); err != nil {
				requestError <- err
				cancel()
				return
			}
		}
	}()

	header, err := upstream.Header()
	if err != nil {
		workload.SetTrailer(upstream.Trailer())
		return err
	}
	if err := workload.SendHeader(header); err != nil {
		return err
	}
	for {
		var response []byte
		if err := upstream.RecvMsg(&response); err != nil {
			workload.SetTrailer(upstream.Trailer())
			if err == io.EOF {
				select {
				case requestErr := <-requestError:
					if requestErr != nil {
						return requestErr
					}
				default:
				}
				return nil
			}
			return err
		}
		if err := workload.SendMsg(response); err != nil {
			return err
		}
	}
}

func readToken(path string) (string, error) {
	contents, err := os.ReadFile(path)
	if err != nil {
		return "", err
	}
	token := strings.TrimSpace(string(contents))
	if token == "" {
		return "", fmt.Errorf("token file %q is empty", path)
	}
	return token, nil
}
