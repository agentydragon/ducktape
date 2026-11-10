package connections

import (
	"bufio"
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"testing"
	"time"

	pb "github.com/agentydragon/ducktape/agentplane/mcp_edge/connection_servicepb"
	"github.com/google/uuid"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/protobuf/encoding/protojson"
)

type connectionStream = grpc.ServerStreamingClient[pb.WatchConnectionsResponse]

func (s *served) watch(t *testing.T, subject string) connectionStream {
	t.Helper()
	stream, err := s.client.WatchConnections(as(t, subject), &pb.WatchConnectionsRequest{})
	if err != nil {
		t.Fatal(err)
	}
	return stream
}

func recvFrame(t *testing.T, stream connectionStream) *pb.WatchConnectionsResponse {
	t.Helper()
	frame, err := stream.Recv()
	if err != nil {
		t.Fatal(err)
	}
	return frame
}

// snapshot reads the frames up to snapshot_complete and returns the chunks they carried.
func snapshot(t *testing.T, stream connectionStream) [][]*pb.Connection {
	t.Helper()
	var chunks [][]*pb.Connection
	for {
		switch frame := recvFrame(t, stream).GetFrame().(type) {
		case *pb.WatchConnectionsResponse_Snapshot:
			chunks = append(chunks, frame.Snapshot.GetConnections())
		case *pb.WatchConnectionsResponse_SnapshotComplete:
			return chunks
		default:
			t.Fatalf("a %T frame before snapshot_complete", frame)
		}
	}
}

// upsert reads the next frame, which must be an upsert.
func upsert(t *testing.T, stream connectionStream) *pb.Connection {
	t.Helper()
	frame := recvFrame(t, stream)
	if frame.GetUpsert() == nil {
		t.Fatalf("frame %v, want an upsert", frame)
	}
	return frame.GetUpsert()
}

// wantEnd reads the stream to its end, which must carry the code; OK is a clean end.
func wantEnd(t *testing.T, what string, stream connectionStream, code codes.Code) {
	t.Helper()
	frame, err := stream.Recv()
	switch {
	case err == nil:
		t.Errorf("%s: frame %v, want the stream to end %v", what, frame, code)
	case code == codes.OK && !errors.Is(err, io.EOF):
		t.Errorf("%s: %v, want the stream to end", what, err)
	case code != codes.OK:
		wantCode(t, what, err, code)
	}
}

func (s *served) rename(t *testing.T, c *pb.Connection, name string) {
	t.Helper()
	if _, err := s.client.UpdateConnection(as(t, testOperator), &pb.UpdateConnectionRequest{
		ConnectionId: c.GetId(), ExpectedVersion: c.GetVersion(), DisplayName: name,
	}); err != nil {
		t.Fatal(err)
	}
}

func TestAWatchSendsEveryConnectionInBoundedChunksThenEachChange(t *testing.T) {
	s := serve(t)
	start := now().Add(-time.Hour)
	var want []string
	for i := range snapshotChunk + 1 {
		want = append(want, s.connect(t, fmt.Sprintf("test-connection-%d", i), start.Add(time.Duration(i)*time.Second),
			&callerAccount, storedGrant{caller: callerAccount, status: grantActive}).String())
	}
	stream := s.watch(t, testOperator)
	var got []string
	for _, chunk := range snapshot(t, stream) {
		if len(chunk) > snapshotChunk {
			t.Errorf("a chunk of %d Connections", len(chunk))
		}
		for _, c := range chunk {
			got = append(got, c.GetId())
		}
	}
	if fmt.Sprint(got) != fmt.Sprint(want) {
		t.Errorf("snapshot %v, want every Connection oldest first: %v", got, want)
	}

	renamed := s.get(t, uuid.MustParse(want[3]))
	s.rename(t, renamed, "test-renamed")
	if got := upsert(t, stream); got.GetId() != renamed.GetId() || got.GetDisplayName() != "test-renamed" {
		t.Errorf("upsert %v, want the rename of %s", got, renamed.GetId())
	}
	rebound := s.get(t, uuid.MustParse(want[5]))
	if _, err := s.client.RebindConnection(as(t, testOperator), &pb.RebindConnectionRequest{
		ConnectionId: rebound.GetId(), ExpectedVersion: 1, ServiceAccount: message(otherAccount),
	}); err != nil {
		t.Fatal(err)
	}
	if got := upsert(t, stream); got.GetId() != rebound.GetId() || *accountOf(got.GetBoundCaller()) != otherAccount {
		t.Errorf("upsert %v, want the rebind of %s", got, rebound.GetId())
	}
	// A Connection the authorization server's code exchange creates.
	created := s.allowed(t, callerAccount)
	if err := s.activate(t, created, approver); err != nil {
		t.Fatal(err)
	}
	if got := upsert(t, stream); len(got.GetGrants()) != 1 || got.GetGrants()[0].GetId() != created.String() {
		t.Errorf("upsert %v, want the Connection of grant %s", got, created)
	}
}

func TestAWatchEndsUnauthenticatedOnceItsBearerIsRefused(t *testing.T) {
	s := serve(t)
	c := s.active(t, "test-watched", callerAccount)
	refused := s.watch(t, testOperator)
	idle := s.watch(t, testOtherOperator)
	snapshot(t, refused)
	snapshot(t, idle)

	// A change is not sent once the bearer is refused.
	s.gate.refuse(testOperator)
	s.rename(t, c, "test-after-refusal")
	wantEnd(t, "the watch with a change to send", refused, codes.Unauthenticated)
	if got := upsert(t, idle); got.GetDisplayName() != "test-after-refusal" {
		t.Errorf("upsert %v", got)
	}
	// With nothing to send, the bearer is checked again all the same.
	s.gate.refuse(testOtherOperator)
	wantEnd(t, "the idle watch", idle, codes.Unauthenticated)
}

func TestDrainingRenewsAWatch(t *testing.T) {
	s := serve(t)
	stream := s.watch(t, testOperator)
	if chunks := snapshot(t, stream); len(chunks) != 0 {
		t.Errorf("snapshot of no Connections: %v", chunks)
	}
	s.gate.drain()
	if frame := recvFrame(t, stream); frame.GetReconnectRequired() == nil {
		t.Errorf("frame %v, want reconnect_required", frame)
	}
	wantEnd(t, "the watch after reconnect_required", stream, codes.OK)
}

func TestAWatchOutlivesTheNotificationConnectionAndReadsWhatChangedUnannounced(t *testing.T) {
	s := serve(t)
	c := s.active(t, "test-watched", callerAccount)
	stream := s.watch(t, testOperator)
	snapshot(t, stream)
	// A change no notification announces, then a dropped LISTEN connection: only the wakeup after it reconnects
	// can show the change.
	if _, err := s.db.ExecContext(t.Context(), `UPDATE external_connection SET version = version + 1 WHERE id = $1`,
		c.GetId()); err != nil {
		t.Fatal(err)
	}
	if _, err := s.db.ExecContext(t.Context(), `SELECT pg_terminate_backend(pid) FROM pg_stat_activity
		WHERE application_name = $1 AND datname = current_database()`, updatesConnection); err != nil {
		t.Fatal(err)
	}
	if got := upsert(t, stream); got.GetId() != c.GetId() || got.GetVersion() != 2 {
		t.Errorf("upsert %v, want version 2 of %s", got, c.GetId())
	}
}

func TestTheHTTPFacadeServesTheConnectionsAsJSON(t *testing.T) {
	s := serve(t)
	c := s.active(t, "test-over-http", callerAccount)
	call := func(method, path string, body []byte) *http.Response {
		t.Helper()
		request, err := http.NewRequestWithContext(as(t, testOperator), method, s.url+path, bytes.NewReader(body))
		if err != nil {
			t.Fatal(err)
		}
		request.Header.Set("Authorization", "Bearer "+testOperator)
		response, err := http.DefaultClient.Do(request)
		if err != nil {
			t.Fatal(err)
		}
		t.Cleanup(func() { response.Body.Close() })
		return response
	}

	renamed := call(http.MethodPatch, "/v1/operator/connections/"+c.GetId(),
		[]byte(`{"expectedVersion": "1", "displayName": "test-renamed-over-http"}`))
	body, err := io.ReadAll(renamed.Body)
	if err != nil {
		t.Fatal(err)
	}
	var answer pb.UpdateConnectionResponse
	if err := protojson.Unmarshal(body, &answer); err != nil {
		t.Fatalf("%d %s: %v", renamed.StatusCode, body, err)
	}
	if renamed.StatusCode != http.StatusOK || answer.GetConnection().GetDisplayName() != "test-renamed-over-http" {
		t.Errorf("%d %s", renamed.StatusCode, body)
	}

	watched := call(http.MethodGet, "/v1/operator/connections:watch", nil)
	if watched.StatusCode != http.StatusOK {
		t.Fatalf("watch: %d", watched.StatusCode)
	}
	// The watch never ends by itself: read the snapshot, a line of JSON per frame.
	lines := bufio.NewScanner(watched.Body)
	var frames []*pb.WatchConnectionsResponse
	for len(frames) < 2 && lines.Scan() {
		var line struct {
			Result json.RawMessage `json:"result"`
		}
		if err := json.Unmarshal(lines.Bytes(), &line); err != nil {
			t.Fatalf("line %s: %v", lines.Bytes(), err)
		}
		frame := &pb.WatchConnectionsResponse{}
		if err := protojson.Unmarshal(line.Result, frame); err != nil {
			t.Fatal(err)
		}
		frames = append(frames, frame)
	}
	if len(frames) != 2 || len(frames[0].GetSnapshot().GetConnections()) != 1 ||
		frames[0].GetSnapshot().GetConnections()[0].GetDisplayName() != "test-renamed-over-http" ||
		frames[1].GetSnapshotComplete() == nil {
		t.Errorf("frames %v, %v", frames, lines.Err())
	}
}
