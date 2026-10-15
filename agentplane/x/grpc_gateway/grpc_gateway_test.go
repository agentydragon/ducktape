package grpcgateway

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/x/grpc_gateway/proto"
	"github.com/grpc-ecosystem/grpc-gateway/v2/runtime"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"google.golang.org/grpc/test/bufconn"
)

const (
	fixtureToken  = "Bearer grpc-gateway-spike"
	largeSequence = uint64(9007199254740993)
)

type proofService struct {
	gatewaypb.UnimplementedGatewayProofServer

	authorizations chan string
	deadlines      chan deadlineObservation
	echoDone       chan error
	watchDone      chan error
}

type deadlineObservation struct {
	deadline   time.Time
	observedAt time.Time
}

func newProofService() *proofService {
	return &proofService{
		authorizations: make(chan string, 8),
		deadlines:      make(chan deadlineObservation, 2),
		echoDone:       make(chan error, 2),
		watchDone:      make(chan error, 2),
	}
}

func (s *proofService) authorize(ctx context.Context) error {
	md, _ := metadata.FromIncomingContext(ctx)
	values := md.Get("authorization")
	token := ""
	if len(values) != 0 {
		token = values[0]
	}
	s.authorizations <- token
	if token != fixtureToken {
		return status.Error(codes.Unauthenticated, "missing or invalid fixture token")
	}
	return nil
}

func (s *proofService) Echo(ctx context.Context, req *gatewaypb.EchoRequest) (*gatewaypb.EchoResponse, error) {
	if err := s.authorize(ctx); err != nil {
		return nil, err
	}
	if req.GetInputText() == "reject" {
		return nil, status.Error(codes.InvalidArgument, "fixture rejected the request")
	}
	if req.GetHoldUntilCancelled() {
		deadline, ok := ctx.Deadline()
		if !ok {
			return nil, status.Error(codes.Internal, "gateway did not set the requested deadline")
		}
		s.deadlines <- deadlineObservation{deadline: deadline, observedAt: time.Now()}
		<-ctx.Done()
		s.echoDone <- ctx.Err()
		return nil, status.FromContextError(ctx.Err()).Err()
	}
	return &gatewaypb.EchoResponse{
		OutputText:       "echo: " + req.GetInputText(),
		CorrelationId:    req.GetCorrelationId(),
		AcceptedSequence: req.GetSequence(),
	}, nil
}

func (s *proofService) Watch(req *gatewaypb.WatchRequest, stream gatewaypb.GatewayProof_WatchServer) error {
	if err := s.authorize(stream.Context()); err != nil {
		return err
	}
	if err := stream.Send(&gatewaypb.WatchEvent{
		Sequence: req.GetAfterSequence() + 1,
		Text:     fmt.Sprintf("after=%d", req.GetAfterSequence()),
	}); err != nil {
		return err
	}
	if req.GetFailAfterFirst() {
		return status.Error(codes.PermissionDenied, "fixture stream failed after its first event")
	}
	if req.GetHoldAfterFirst() {
		<-stream.Context().Done()
		s.watchDone <- stream.Context().Err()
		return status.FromContextError(stream.Context().Err()).Err()
	}
	return nil
}

type gatewayFixture struct {
	server  *httptest.Server
	client  *http.Client
	conn    *grpc.ClientConn
	grpc    *grpc.Server
	lis     *bufconn.Listener
	service *proofService
}

func newGatewayFixture(t *testing.T) *gatewayFixture {
	t.Helper()

	service := newProofService()
	lis := bufconn.Listen(1 << 20)
	grpcServer := grpc.NewServer()
	gatewaypb.RegisterGatewayProofServer(grpcServer, service)
	go func() {
		_ = grpcServer.Serve(lis)
	}()

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	conn, err := grpc.DialContext(
		ctx,
		"bufnet",
		grpc.WithContextDialer(func(context.Context, string) (net.Conn, error) { return lis.Dial() }),
		grpc.WithTransportCredentials(insecure.NewCredentials()),
		grpc.WithBlock(),
	)
	if err != nil {
		grpcServer.Stop()
		_ = lis.Close()
		t.Fatalf("connect to fixture gRPC server: %v", err)
	}

	mux := runtime.NewServeMux()
	if err := gatewaypb.RegisterGatewayProofHandlerClient(ctx, mux, gatewaypb.NewGatewayProofClient(conn)); err != nil {
		conn.Close()
		grpcServer.Stop()
		_ = lis.Close()
		t.Fatalf("register generated gateway handler: %v", err)
	}
	server := httptest.NewServer(mux)
	fixture := &gatewayFixture{
		server:  server,
		client:  &http.Client{Timeout: 5 * time.Second},
		conn:    conn,
		grpc:    grpcServer,
		lis:     lis,
		service: service,
	}
	t.Cleanup(func() {
		server.Close()
		_ = conn.Close()
		grpcServer.Stop()
		_ = lis.Close()
	})
	return fixture
}

func (f *gatewayFixture) postEcho(t *testing.T, body string, token string) *http.Response {
	t.Helper()
	req, err := http.NewRequest(http.MethodPost, f.server.URL+"/v1/echo", strings.NewReader(body))
	if err != nil {
		t.Fatal(err)
	}
	req.Header.Set("Content-Type", "application/json")
	if token != "" {
		req.Header.Set("Authorization", token)
	}
	resp, err := f.client.Do(req)
	if err != nil {
		t.Fatalf("POST /v1/echo: %v", err)
	}
	return resp
}

func TestUnaryProtoJSONAuthorizationAndStatusMapping(t *testing.T) {
	f := newGatewayFixture(t)
	resp := f.postEcho(t, fmt.Sprintf(`{"inputText":"hello","correlationId":"client-42","sequence":"%d"}`, largeSequence), fixtureToken)
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("authorized Echo status = %d, want 200", resp.StatusCode)
	}
	var got map[string]any
	if err := json.NewDecoder(resp.Body).Decode(&got); err != nil {
		t.Fatalf("decode Echo response: %v", err)
	}
	if got["outputText"] != "echo: hello" || got["correlationId"] != "client-42" || got["acceptedSequence"] != fmt.Sprint(largeSequence) {
		t.Fatalf("Echo JSON = %#v, want lowerCamel ProtoJSON fields and quoted uint64", got)
	}
	if token := <-f.service.authorizations; token != fixtureToken {
		t.Fatalf("forwarded authorization = %q, want fixture token", token)
	}

	unauthenticated := f.postEcho(t, `{"inputText":"hello"}`, "")
	defer unauthenticated.Body.Close()
	if unauthenticated.StatusCode != http.StatusUnauthorized {
		body, _ := io.ReadAll(unauthenticated.Body)
		t.Fatalf("unauthenticated Echo status = %d, body %s; want 401", unauthenticated.StatusCode, body)
	}
	if token := <-f.service.authorizations; token != "" {
		t.Fatalf("missing authorization reached fixture as %q, want empty", token)
	}

	badRequest := f.postEcho(t, `{"inputText":"reject"}`, fixtureToken)
	defer badRequest.Body.Close()
	if badRequest.StatusCode != http.StatusBadRequest {
		body, _ := io.ReadAll(badRequest.Body)
		t.Fatalf("invalid Echo status = %d, body %s; want 400", badRequest.StatusCode, body)
	}
	if token := <-f.service.authorizations; token != fixtureToken {
		t.Fatalf("forwarded authorization for invalid request = %q, want fixture token", token)
	}
}

func TestGrpcTimeoutHeaderSetsUnaryDeadline(t *testing.T) {
	f := newGatewayFixture(t)
	req, err := http.NewRequest(http.MethodPost, f.server.URL+"/v1/echo", strings.NewReader(`{"inputText":"wait","holdUntilCancelled":true}`))
	if err != nil {
		t.Fatal(err)
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", fixtureToken)
	req.Header.Set("Grpc-Timeout", "100m")
	resp, err := f.client.Do(req)
	if err != nil {
		t.Fatalf("POST with Grpc-Timeout: %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusGatewayTimeout {
		body, _ := io.ReadAll(resp.Body)
		t.Fatalf("deadline Echo status = %d, body %s; want 504", resp.StatusCode, body)
	}
	select {
	case observation := <-f.service.deadlines:
		if remaining := observation.deadline.Sub(observation.observedAt); remaining > time.Second {
			t.Fatalf("gRPC deadline was %s after service observation, want the 100ms Grpc-Timeout rather than the HTTP client's 5s timeout", remaining)
		}
	case <-time.After(time.Second):
		t.Fatal("fixture did not report the gRPC deadline")
	}
	// The gateway's HTTP deadline and the server-side gRPC deadline can expire in either order.
	if err := <-f.service.echoDone; !errors.Is(err, context.Canceled) && !errors.Is(err, context.DeadlineExceeded) {
		t.Fatalf("fixture Echo context error = %v, want cancellation when the request deadline expires", err)
	}
}

func TestWatchProtoJSONQueryAndTerminalStreamError(t *testing.T) {
	f := newGatewayFixture(t)
	watchURL := fmt.Sprintf("%s/v1/watch?afterSequence=%d&failAfterFirst=true", f.server.URL, largeSequence)
	req, err := http.NewRequest(http.MethodGet, watchURL, nil)
	if err != nil {
		t.Fatal(err)
	}
	req.Header.Set("Authorization", fixtureToken)
	resp, err := f.client.Do(req)
	if err != nil {
		t.Fatalf("GET /v1/watch: %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("stream status = %d, want 200 after stream starts", resp.StatusCode)
	}
	reader := bufio.NewReader(resp.Body)
	first, err := reader.ReadString('\n')
	if err != nil {
		t.Fatalf("read first stream event: %v", err)
	}
	var event struct {
		Result struct {
			Sequence string `json:"sequence"`
			Text     string `json:"text"`
		} `json:"result"`
	}
	if err := json.Unmarshal(bytes.TrimSpace([]byte(first)), &event); err != nil {
		t.Fatalf("decode first event %q: %v", first, err)
	}
	if event.Result.Sequence != fmt.Sprint(largeSequence+1) || event.Result.Text != fmt.Sprintf("after=%d", largeSequence) {
		t.Fatalf("first event = %#v, want exact afterSequence mapping", event.Result)
	}
	last, err := reader.ReadString('\n')
	if err != nil {
		t.Fatalf("read terminal stream error: %v", err)
	}
	var terminal struct {
		Error struct {
			Code    int    `json:"code"`
			Message string `json:"message"`
		} `json:"error"`
	}
	if err := json.Unmarshal(bytes.TrimSpace([]byte(last)), &terminal); err != nil {
		t.Fatalf("decode terminal stream error %q: %v", last, err)
	}
	if terminal.Error.Code != int(codes.PermissionDenied) || terminal.Error.Message != "fixture stream failed after its first event" {
		t.Fatalf("terminal stream error = %#v, want PermissionDenied after the first event", terminal.Error)
	}
	if token := <-f.service.authorizations; token != fixtureToken {
		t.Fatalf("forwarded stream authorization = %q, want fixture token", token)
	}
}

func TestWatchCancelsGrpcStreamWhenHttpClientCancels(t *testing.T) {
	f := newGatewayFixture(t)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, f.server.URL+"/v1/watch?afterSequence=4&holdAfterFirst=true", nil)
	if err != nil {
		t.Fatal(err)
	}
	req.Header.Set("Authorization", fixtureToken)
	resp, err := f.client.Do(req)
	if err != nil {
		t.Fatalf("GET held stream: %v", err)
	}
	reader := bufio.NewReader(resp.Body)
	if _, err := reader.ReadString('\n'); err != nil {
		resp.Body.Close()
		t.Fatalf("read first held stream event: %v", err)
	}
	cancel()
	_ = resp.Body.Close()
	select {
	case err := <-f.service.watchDone:
		if !errors.Is(err, context.Canceled) {
			t.Fatalf("gRPC stream context error = %v, want canceled", err)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("HTTP cancellation did not cancel the gRPC stream")
	}
}
