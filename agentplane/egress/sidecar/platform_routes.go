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

	netlistener "github.com/agentydragon/ducktape/agentplane/net/listener"
	"github.com/agentydragon/ducktape/agentplane/observability"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/keepalive"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
)

type platformRoutes struct {
	server *grpc.Server
	listen net.Listener
	conns  []*grpc.ClientConn
}

type routeForwarder struct {
	conn      *grpc.ClientConn
	tokenFile string
}

type platformRouteDispatcher struct {
	byService map[string]*routeForwarder
}

func bindPlatformRoutes(config platformRoutesConfig) (*platformRoutes, error) {
	return bindPlatformRoutesWithKeepalive(config, keepalive.ClientParameters{
		Time:                time.Minute,
		Timeout:             10 * time.Second,
		PermitWithoutStream: false,
	})
}

func bindPlatformRoutesWithKeepalive(
	config platformRoutesConfig,
	keepaliveParameters keepalive.ClientParameters,
) (*platformRoutes, error) {
	if len(config.Routes) == 0 {
		return nil, nil
	}
	listener, err := netlistener.Open(config.ListenAddress)
	if err != nil {
		return nil, fmt.Errorf("listen for platform routes: %w", err)
	}
	started := &platformRoutes{listen: listener}
	dispatcher := &platformRouteDispatcher{byService: make(map[string]*routeForwarder)}
	names := make([]string, 0, len(config.Routes))
	for name := range config.Routes {
		names = append(names, name)
	}
	sort.Strings(names)
	for _, name := range names {
		route := config.Routes[name]
		conn, err := grpc.NewClient(
			route.Upstream,
			observability.GRPCClientOption(),
			grpc.WithTransportCredentials(insecure.NewCredentials()),
			grpc.WithKeepaliveParams(keepaliveParameters),
		)
		if err != nil {
			closePlatformRoutes(started)
			return nil, fmt.Errorf("create upstream for platform route %q: %w", name, err)
		}
		started.conns = append(started.conns, conn)
		forwarder := &routeForwarder{conn: conn, tokenFile: route.TokenFile}
		for _, prefix := range route.ServicePrefixes {
			dispatcher.byService[prefix] = forwarder
		}
	}
	started.server = grpc.NewServer(
		observability.GRPCServerOption(),
		grpc.UnknownServiceHandler(dispatcher.forward),
		grpc.ForceServerCodec(rawCodec{}),
	)
	return started, nil
}

func validPlatformServicePrefix(prefix string) bool {
	if !strings.HasPrefix(prefix, "/") || !strings.HasSuffix(prefix, "/") {
		return false
	}
	service := strings.TrimSuffix(strings.TrimPrefix(prefix, "/"), "/")
	return service != "" && !strings.ContainsAny(service, "/ \t\r\n")
}

func (d *platformRouteDispatcher) forward(server any, workload grpc.ServerStream) error {
	method, okay := grpc.MethodFromServerStream(workload)
	if !okay {
		return status.Error(codes.Internal, "gRPC method is unavailable")
	}
	separator := strings.LastIndexByte(method, '/')
	if separator <= 0 || separator == len(method)-1 {
		return status.Errorf(codes.Unimplemented, "no platform route for gRPC method %q", method)
	}
	prefix := method[:separator+1]
	forwarder, exists := d.byService[prefix]
	if !exists {
		return status.Errorf(codes.Unimplemented, "no platform route for gRPC service %q", prefix)
	}
	return forwarder.forward(server, workload)
}

func closePlatformRoutes(routes *platformRoutes) {
	if routes == nil {
		return
	}
	if routes.server != nil {
		routes.server.Stop()
	}
	for i := len(routes.conns) - 1; i >= 0; i-- {
		_ = routes.conns[i].Close()
	}
	if routes.listen != nil {
		_ = routes.listen.Close()
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
