package admission

import (
	"context"
	"crypto"
	"crypto/rand"
	"crypto/rsa"
	"crypto/sha256"
	"crypto/tls"
	"crypto/x509"
	"encoding/base64"
	"encoding/json"
	"errors"
	"io"
	"math/big"
	"net"
	"net/http"
	"net/http/httptest"
	"sync/atomic"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/identity/auth"
	"github.com/agentydragon/ducktape/agentplane/identity/operator"
	pb "github.com/agentydragon/ducktape/agentplane/mcp_edge/api/connection/connection_servicepb"
	"github.com/agentydragon/ducktape/agentplane/testing/grpcmethods"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials"
	"google.golang.org/grpc/credentials/insecure"
	health "google.golang.org/grpc/health"
	healthpb "google.golang.org/grpc/health/grpc_health_v1"
	"google.golang.org/grpc/interop/grpc_testing"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/peer"
	"google.golang.org/grpc/status"
)

const (
	testAudience = "test-mcp-edge"
	testKeyID    = "test-key"
)

type testIssuer struct {
	settings operator.Settings
	key      *rsa.PrivateKey
}

func newTestIssuer(t *testing.T) (*testIssuer, *operator.Verifier) {
	t.Helper()
	key, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatal(err)
	}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/jwks" {
			http.NotFound(w, r)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		public := key.PublicKey
		if err := json.NewEncoder(w).Encode(map[string]any{"keys": []map[string]string{{
			"kty": "RSA", "kid": testKeyID, "use": "sig", "alg": "RS256",
			"n": base64.RawURLEncoding.EncodeToString(public.N.Bytes()),
			"e": base64.RawURLEncoding.EncodeToString(big.NewInt(int64(public.E)).Bytes()),
		}}}); err != nil {
			t.Error(err)
		}
	}))
	t.Cleanup(server.Close)
	settings := operator.DefaultSettings()
	settings.Issuer = server.URL
	settings.Audience = testAudience
	settings.JWKSURI = server.URL + "/jwks"
	verifier, err := operator.NewVerifier(settings)
	if err != nil {
		t.Fatal(err)
	}
	return &testIssuer{settings: settings, key: key}, verifier
}

func (i *testIssuer) token(t *testing.T, changes map[string]any) string {
	t.Helper()
	now := time.Now()
	claims := map[string]any{
		"iss": i.settings.Issuer, "aud": testAudience, "azp": testAudience,
		"sub": "test-operator", "iat": now.Unix(), "exp": now.Add(time.Hour).Unix(),
	}
	for name, value := range changes {
		if value == nil {
			delete(claims, name)
		} else {
			claims[name] = value
		}
	}
	header, err := json.Marshal(map[string]string{"alg": "RS256", "kid": testKeyID, "typ": "JWT"})
	if err != nil {
		t.Fatal(err)
	}
	payload, err := json.Marshal(claims)
	if err != nil {
		t.Fatal(err)
	}
	encoded := base64.RawURLEncoding.EncodeToString(header) + "." + base64.RawURLEncoding.EncodeToString(payload)
	digest := sha256.Sum256([]byte(encoded))
	signature, err := rsa.SignPKCS1v15(rand.Reader, i.key, crypto.SHA256, digest[:])
	if err != nil {
		t.Fatal(err)
	}
	return encoded + "." + base64.RawURLEncoding.EncodeToString(signature)
}

type connectionServer struct {
	pb.UnimplementedConnectionServiceServer
	gate       *Gate
	called     atomic.Bool
	principal  chan operator.Subject
	watchStart chan struct{}
}

func (s *connectionServer) ListConnections(ctx context.Context, _ *pb.ListConnectionsRequest) (*pb.ListConnectionsResponse, error) {
	issuer, subject := s.gate.Operator(ctx)
	s.called.Store(true)
	s.principal <- operator.Subject{Issuer: issuer, Subject: subject}
	return &pb.ListConnectionsResponse{}, nil
}

func (s *connectionServer) WatchConnections(_ *pb.WatchConnectionsRequest, stream grpc.ServerStreamingServer[pb.WatchConnectionsResponse]) error {
	s.watchStart <- struct{}{}
	<-s.gate.DrainingContext().Done()
	return nil
}

type unlistedServer struct {
	grpc_testing.UnimplementedTestServiceServer
	calledUnary  atomic.Bool
	calledStream atomic.Bool
}

func (s *unlistedServer) UnaryCall(context.Context, *grpc_testing.SimpleRequest) (*grpc_testing.SimpleResponse, error) {
	s.calledUnary.Store(true)
	return &grpc_testing.SimpleResponse{}, nil
}

func (s *unlistedServer) StreamingOutputCall(_ *grpc_testing.StreamingOutputCallRequest, stream grpc_testing.TestService_StreamingOutputCallServer) error {
	s.calledStream.Store(true)
	return stream.Send(&grpc_testing.StreamingOutputCallResponse{})
}

type served struct {
	gate        *Gate
	connection  *connectionServer
	unknown     *unlistedServer
	connections pb.ConnectionServiceClient
	health      healthpb.HealthClient
	test        grpc_testing.TestServiceClient
}

type contextStream struct {
	grpc.ServerStream
	ctx context.Context
}

func (s contextStream) Context() context.Context { return s.ctx }

func serve(t *testing.T, gate *Gate) *served {
	t.Helper()
	connectionServer := &connectionServer{gate: gate, principal: make(chan operator.Subject, 1), watchStart: make(chan struct{}, 1)}
	unknownServer := &unlistedServer{}
	server := grpc.NewServer(
		grpc.ChainUnaryInterceptor(gate.UnaryInterceptor),
		grpc.ChainStreamInterceptor(gate.StreamInterceptor),
	)
	pb.RegisterConnectionServiceServer(server, connectionServer)
	grpc_testing.RegisterTestServiceServer(server, unknownServer)
	healthServer := health.NewServer()
	healthServer.SetServingStatus("", healthpb.HealthCheckResponse_SERVING)
	healthpb.RegisterHealthServer(server, healthServer)
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	go func() { _ = server.Serve(listener) }()
	t.Cleanup(server.Stop)
	client, err := grpc.NewClient(listener.Addr().String(), grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = client.Close() })
	return &served{
		gate: gate, connection: connectionServer, unknown: unknownServer,
		connections: pb.NewConnectionServiceClient(client), health: healthpb.NewHealthClient(client),
		test: grpc_testing.NewTestServiceClient(client),
	}
}

func bearer(ctx context.Context, token string) context.Context {
	return metadata.AppendToOutgoingContext(ctx, "authorization", "Bearer "+token)
}

func wantCode(t *testing.T, what string, err error, code codes.Code) {
	t.Helper()
	if got := status.Code(err); got != code {
		t.Errorf("%s: got %v, want %v (%v)", what, got, code, err)
	}
}

func TestGateClassifiesEveryConnectionAndHealthMethod(t *testing.T) {
	gate := NewGate(nil)
	protectedCount := 0
	for _, method := range grpcmethods.Of(pb.ConnectionService_ServiceDesc) {
		listed, protected := gate.authenticateMethod(method.Name)
		if !listed || !protected {
			t.Errorf("%s is listed=%t protected=%t, want protected", method.Name, listed, protected)
		}
		if protected {
			protectedCount++
		}
	}
	if protectedCount != 9 {
		t.Errorf("ConnectionService has %d protected methods, want exactly 9", protectedCount)
	}
	for _, method := range grpcmethods.Of(healthpb.Health_ServiceDesc) {
		listed, protected := gate.authenticateMethod(method.Name)
		if !listed || protected {
			t.Errorf("%s is listed=%t protected=%t, want public", method.Name, listed, protected)
		}
	}
	if listed, _ := gate.authenticateMethod("/test.unlisted.Service/Call"); listed {
		t.Fatal("unlisted method has admission policy")
	}
}

func TestValidOperatorReachesConnectionHandlerAndInvalidCredentialsDoNot(t *testing.T) {
	issuer, verifier := newTestIssuer(t)
	valid := issuer.token(t, nil)
	otherParty := issuer.token(t, map[string]any{"azp": "another-party"})
	wrongAudience := issuer.token(t, map[string]any{"aud": "another-audience"})
	multipleAudiences := issuer.token(t, map[string]any{"aud": []string{testAudience, "another-audience"}})
	workloadToken := issuer.token(t, map[string]any{"iss": "https://workload.example", "aud": "test-workload", "azp": nil})
	wrongIssuer := issuer.token(t, map[string]any{"iss": "https://another-operator.example"})
	emptySubject := issuer.token(t, map[string]any{"sub": " "})
	expired := issuer.token(t, map[string]any{"exp": time.Now().Add(-time.Hour).Unix()})
	missingParty := issuer.token(t, map[string]any{"azp": nil})
	s := serve(t, NewGate(verifier))
	ctx, cancel := context.WithTimeout(t.Context(), 5*time.Second)
	defer cancel()

	if _, err := s.connections.ListConnections(bearer(ctx, valid), &pb.ListConnectionsRequest{}); err != nil {
		t.Fatalf("valid operator call: %v", err)
	}
	select {
	case got := <-s.connection.principal:
		if got.Issuer != issuer.settings.Issuer || got.Subject != "test-operator" {
			t.Fatalf("handler received operator %#v", got)
		}
	case <-ctx.Done():
		t.Fatal("valid operator did not reach the handler")
	}

	for name, call := range map[string]func() error{
		"missing bearer": func() error {
			_, err := s.connections.ListConnections(ctx, &pb.ListConnectionsRequest{})
			return err
		},
		"multiple bearers": func() error {
			request := metadata.AppendToOutgoingContext(ctx, "authorization", "Bearer "+valid, "authorization", "Bearer "+valid)
			_, err := s.connections.ListConnections(request, &pb.ListConnectionsRequest{})
			return err
		},
		"malformed bearer": func() error {
			request := metadata.AppendToOutgoingContext(ctx, "authorization", "Basic "+valid)
			_, err := s.connections.ListConnections(request, &pb.ListConnectionsRequest{})
			return err
		},
		"workload bearer": func() error {
			_, err := s.connections.ListConnections(bearer(ctx, workloadToken), &pb.ListConnectionsRequest{})
			return err
		},
		"delegated scheme": func() error {
			request := metadata.AppendToOutgoingContext(ctx, "authorization", auth.DelegatedScheme+" "+valid)
			_, err := s.connections.ListConnections(request, &pb.ListConnectionsRequest{})
			return err
		},
		"delegation envelope": func() error {
			request := metadata.AppendToOutgoingContext(ctx, "authorization", "Bearer "+valid, auth.DelegationMetadataKey, "not-a-delegation")
			_, err := s.connections.ListConnections(request, &pb.ListConnectionsRequest{})
			return err
		},
		"wrong issuer": func() error {
			_, err := s.connections.ListConnections(bearer(ctx, wrongIssuer), &pb.ListConnectionsRequest{})
			return err
		},
		"wrong audience": func() error {
			_, err := s.connections.ListConnections(bearer(ctx, wrongAudience), &pb.ListConnectionsRequest{})
			return err
		},
		"multiple audiences": func() error {
			_, err := s.connections.ListConnections(bearer(ctx, multipleAudiences), &pb.ListConnectionsRequest{})
			return err
		},
		"wrong authorized party": func() error {
			_, err := s.connections.ListConnections(bearer(ctx, otherParty), &pb.ListConnectionsRequest{})
			return err
		},
		"blank subject": func() error {
			_, err := s.connections.ListConnections(bearer(ctx, emptySubject), &pb.ListConnectionsRequest{})
			return err
		},
		"expired token": func() error {
			_, err := s.connections.ListConnections(bearer(ctx, expired), &pb.ListConnectionsRequest{})
			return err
		},
		"missing authorized party": func() error {
			_, err := s.connections.ListConnections(bearer(ctx, missingParty), &pb.ListConnectionsRequest{})
			return err
		},
	} {
		t.Run(name, func(t *testing.T) {
			err := call()
			wantCode(t, name, err, codes.Unauthenticated)
			if got := status.Convert(err).Message(); got != "invalid bearer" {
				t.Errorf("refusal message %q reveals why the operator token was refused", got)
			}
		})
	}
	if s.connection.called.Load() != true {
		t.Fatal("valid operator handler was not called")
	}
	select {
	case extra := <-s.connection.principal:
		t.Fatalf("rejected calls reached handler as %#v", extra)
	default:
	}
}

func TestHealthIsPublicButUnknownRegisteredUnaryAndStreamMethodsAreDenied(t *testing.T) {
	s := serve(t, NewGate(nil))
	ctx, cancel := context.WithTimeout(t.Context(), 5*time.Second)
	defer cancel()
	response, err := s.health.Check(ctx, &healthpb.HealthCheckRequest{})
	if err != nil || response.GetStatus() != healthpb.HealthCheckResponse_SERVING {
		t.Fatalf("unauthenticated health check returned %v, %v", response, err)
	}
	list, err := s.health.List(ctx, &healthpb.HealthListRequest{})
	if err != nil || list == nil {
		t.Fatalf("unauthenticated health list returned %v, %v", list, err)
	}
	watch, err := s.health.Watch(ctx, &healthpb.HealthCheckRequest{})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := watch.Recv(); err != nil {
		t.Fatalf("unauthenticated health watch: %v", err)
	}

	_, err = s.test.UnaryCall(ctx, &grpc_testing.SimpleRequest{})
	wantCode(t, "unknown registered unary", err, codes.PermissionDenied)
	if got := status.Convert(err).Message(); got != "method not allowed" {
		t.Errorf("unknown method message = %q", got)
	}
	stream, err := s.test.StreamingOutputCall(ctx, &grpc_testing.StreamingOutputCallRequest{})
	if err != nil {
		wantCode(t, "unknown registered stream", err, codes.PermissionDenied)
	} else {
		_, err = stream.Recv()
		wantCode(t, "unknown registered stream", err, codes.PermissionDenied)
	}
	if s.unknown.calledUnary.Load() || s.unknown.calledStream.Load() {
		t.Fatalf("unknown handlers ran: unary=%t stream=%t", s.unknown.calledUnary.Load(), s.unknown.calledStream.Load())
	}
}

func TestSVIDPeersAndMixedCredentialsAreRefusedAtTheBoundary(t *testing.T) {
	issuer, verifier := newTestIssuer(t)
	valid := issuer.token(t, nil)
	gate := NewGate(verifier)
	peerContext := func(ctx context.Context) context.Context {
		return peer.NewContext(ctx, &peer.Peer{AuthInfo: credentials.TLSInfo{State: tls.ConnectionState{
			PeerCertificates: []*x509.Certificate{{}},
		}}})
	}
	for name, ctx := range map[string]context.Context{
		"SVID peer alone on protected method": peerContext(context.Background()),
		"SVID and bearer":                     metadata.NewIncomingContext(peerContext(context.Background()), metadata.Pairs("authorization", "Bearer "+valid)),
		"SVID and delegation":                 metadata.NewIncomingContext(peerContext(context.Background()), metadata.Pairs(auth.DelegationMetadataKey, "delegation")),
	} {
		t.Run(name, func(t *testing.T) {
			_, err := gate.UnaryInterceptor(ctx, &pb.ListConnectionsRequest{}, &grpc.UnaryServerInfo{
				FullMethod: pb.ConnectionService_ListConnections_FullMethodName,
			}, func(context.Context, any) (any, error) {
				t.Fatal("protected handler ran")
				return nil, nil
			})
			wantCode(t, name, err, codes.Unauthenticated)
		})
	}
	for name, ctx := range map[string]context.Context{
		"SVID and bearer on public health":     metadata.NewIncomingContext(peerContext(context.Background()), metadata.Pairs("authorization", "Bearer "+valid)),
		"SVID and delegation on public health": metadata.NewIncomingContext(peerContext(context.Background()), metadata.Pairs(auth.DelegationMetadataKey, "delegation")),
	} {
		t.Run(name, func(t *testing.T) {
			_, err := gate.UnaryInterceptor(ctx, &healthpb.HealthCheckRequest{}, &grpc.UnaryServerInfo{
				FullMethod: healthpb.Health_Check_FullMethodName,
			}, func(context.Context, any) (any, error) {
				t.Fatal("mixed credentials reached public handler")
				return nil, nil
			})
			wantCode(t, name, err, codes.Unauthenticated)
		})
	}
	called := false
	_, err := gate.UnaryInterceptor(peerContext(context.Background()), &healthpb.HealthCheckRequest{}, &grpc.UnaryServerInfo{
		FullMethod: healthpb.Health_Check_FullMethodName,
	}, func(context.Context, any) (any, error) {
		called = true
		return &healthpb.HealthCheckResponse{Status: healthpb.HealthCheckResponse_SERVING}, nil
	})
	if err != nil || !called {
		t.Fatalf("public health with only a peer certificate returned %v, called=%t", err, called)
	}
	for name, tc := range map[string]struct {
		ctx    context.Context
		method string
	}{
		"SVID peer on protected watch": {
			ctx: peerContext(context.Background()), method: pb.ConnectionService_WatchConnections_FullMethodName,
		},
		"SVID and bearer on protected watch": {
			ctx:    metadata.NewIncomingContext(peerContext(context.Background()), metadata.Pairs("authorization", "Bearer "+valid)),
			method: pb.ConnectionService_WatchConnections_FullMethodName,
		},
		"SVID and delegation on protected watch": {
			ctx:    metadata.NewIncomingContext(peerContext(context.Background()), metadata.Pairs(auth.DelegationMetadataKey, "delegation")),
			method: pb.ConnectionService_WatchConnections_FullMethodName,
		},
		"SVID and bearer on public health watch": {
			ctx:    metadata.NewIncomingContext(peerContext(context.Background()), metadata.Pairs("authorization", "Bearer "+valid)),
			method: healthpb.Health_Watch_FullMethodName,
		},
		"SVID and delegation on public health watch": {
			ctx:    metadata.NewIncomingContext(peerContext(context.Background()), metadata.Pairs(auth.DelegationMetadataKey, "delegation")),
			method: healthpb.Health_Watch_FullMethodName,
		},
	} {
		t.Run(name, func(t *testing.T) {
			err := gate.StreamInterceptor(nil, contextStream{ctx: tc.ctx}, &grpc.StreamServerInfo{
				FullMethod: tc.method,
			}, func(any, grpc.ServerStream) error {
				t.Fatal("protected watch handler ran")
				return nil
			})
			wantCode(t, name, err, codes.Unauthenticated)
		})
	}
	calledStream := false
	if err := gate.StreamInterceptor(nil, contextStream{ctx: peerContext(context.Background())}, &grpc.StreamServerInfo{
		FullMethod: healthpb.Health_Watch_FullMethodName,
	}, func(any, grpc.ServerStream) error {
		calledStream = true
		return nil
	}); err != nil || !calledStream {
		t.Errorf("public health watch with only a peer certificate returned %v, called=%t", err, calledStream)
	}
}

func TestRecheckRequiresTheSameVerifiedOperator(t *testing.T) {
	issuer, verifier := newTestIssuer(t)
	otherOperator := issuer.token(t, map[string]any{"sub": "test-another-operator"})
	gate := NewGate(verifier)
	ctx := metadata.NewIncomingContext(context.Background(), metadata.Pairs("authorization", "Bearer "+issuer.token(t, nil)))
	admitted, err := gate.admit(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if err := gate.Recheck(admitted); err != nil {
		t.Fatalf("same operator did not recheck: %v", err)
	}
	changed := metadata.NewIncomingContext(admitted, metadata.Pairs("authorization", "Bearer "+otherOperator))
	wantCode(t, "a different verified operator", gate.Recheck(changed), codes.Unauthenticated)
}

func TestDrainCancelsWatchContextAndRefusesNewCalls(t *testing.T) {
	issuer, verifier := newTestIssuer(t)
	s := serve(t, NewGate(verifier))
	ctx, cancel := context.WithTimeout(bearer(t.Context(), issuer.token(t, nil)), 5*time.Second)
	defer cancel()
	stream, err := s.connections.WatchConnections(ctx, &pb.WatchConnectionsRequest{})
	if err != nil {
		t.Fatal(err)
	}
	select {
	case <-s.connection.watchStart:
	case <-ctx.Done():
		t.Fatal("watch did not reach its handler")
	}
	s.gate.Drain()
	if _, err := stream.Recv(); !errors.Is(err, io.EOF) {
		t.Fatalf("watch ended with %v, want a clean stream end after its drain context", err)
	}
	_, err = s.connections.ListConnections(t.Context(), &pb.ListConnectionsRequest{})
	wantCode(t, "a new protected unary call during drain", err, codes.Unavailable)
	stream, err = s.connections.WatchConnections(t.Context(), &pb.WatchConnectionsRequest{})
	if err != nil {
		wantCode(t, "a new protected stream during drain", err, codes.Unavailable)
	} else {
		_, err = stream.Recv()
		wantCode(t, "a new protected stream during drain", err, codes.Unavailable)
	}
	if got := status.Convert(err).Message(); got != errDraining.Error() {
		t.Errorf("drain refusal message = %q, want %q", got, errDraining.Error())
	}
}

func TestFailurePreservesUncategorizedAndContextStatuses(t *testing.T) {
	if got := failure(context.Background(), errors.New("test fault")); status.Code(got) != codes.Unavailable || status.Convert(got).Message() != "the Action Service is temporarily unavailable" {
		t.Errorf("uncategorized failure = %v", got)
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if got := failure(ctx, ctx.Err()); status.Code(got) != codes.Canceled {
		t.Errorf("canceled failure = %v", got)
	}
	deadline, stop := context.WithDeadline(context.Background(), time.Now().Add(-time.Second))
	defer stop()
	if got := failure(deadline, deadline.Err()); status.Code(got) != codes.DeadlineExceeded || status.Convert(got).Message() != "action request deadline expired" {
		t.Errorf("deadline failure = %v", got)
	}
}
