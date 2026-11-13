// probe_ingress verifies live packing with one explicit, dedicated synthetic journal. It starts no
// harness or provider request. Credentials are short-lived pod-bound bearer files supplied by the
// operator; server identity uses the same verified SPIFFE transport as service clients.
package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"os"
	"strings"
	"time"

	transport "github.com/agentydragon/ducktape/agentplane/environment_runtime_transport"
	historypb "github.com/agentydragon/ducktape/agentplane/history_service/api/history/history_servicepb"
	ingresspb "github.com/agentydragon/ducktape/agentplane/history_service/api/ingress/session_ingresspb"
	"github.com/agentydragon/ducktape/agentplane/history_service/testing/spans"
	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	"github.com/google/uuid"
	"google.golang.org/grpc"
	"google.golang.org/grpc/metadata"
	"google.golang.org/protobuf/proto"
)

func bearing(ctx context.Context, path string) (context.Context, error) {
	token, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	return metadata.AppendToOutgoingContext(ctx, "authorization", "Bearer "+strings.TrimSpace(string(token))), nil
}

func run() error {
	target := flag.String("target", "", "History RPC host:port")
	caFile := flag.String("ca-file", "", "absolute path to workload trust roots")
	writerToken := flag.String("writer-token-file", "", "pod-bound sampler bearer file, ingress audience")
	readerToken := flag.String("reader-token-file", "", "pod-bound backend reader bearer file, History audience")
	session := flag.String("session-id", "", "dedicated synthetic Session UUID, retained for retry/readback")
	flag.Parse()
	id, err := uuid.Parse(*session)
	if err != nil || id == uuid.Nil || *target == "" || *caFile == "" || *writerToken == "" || *readerToken == "" {
		return fmt.Errorf("target, CA, writer/reader token files and explicit Session UUID are required")
	}
	credentials, err := (&transport.ClientTLS{CAFile: *caFile, ServerURI: transport.HistoryIngressURI}).Credentials()
	if err != nil {
		return err
	}
	connection, err := grpc.NewClient(*target, grpc.WithTransportCredentials(credentials), grpc.WithDisableRetry())
	if err != nil {
		return err
	}
	defer connection.Close()
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	writeCtx, err := bearing(ctx, *writerToken)
	if err != nil {
		return err
	}
	readCtx, err := bearing(ctx, *readerToken)
	if err != nil {
		return err
	}
	chunks := make([]string, 80)
	for index := range chunks {
		chunks[index] = "history packing probe "
	}
	span := spans.CodexReasoning([][]string{chunks})
	entries := span.Entries[:len(span.Entries)-2]
	for _, entry := range entries {
		entry.Origin.SourceId = id.String()
	}
	fmt.Fprintln(os.Stderr, "synthetic History Session:", id)
	push, err := ingresspb.NewSessionJournalIngressClient(connection).Push(writeCtx)
	if err != nil {
		return err
	}
	const namespace, executor = "agentplane-staging", "history-compaction-probe"
	announcement := &ingresspb.SamplerAnnouncement{
		Session: &sessionpb.Attached{SessionId: id.String(), Spec: &sessionpb.SessionSpec{
			Placement: &sessionpb.SessionPlacement{Kind: &sessionpb.SessionPlacement_CentralSamplerV1{CentralSamplerV1: &sessionpb.CentralSamplerV1Placement{
				ExecutorEnvironment: &sessionpb.ExactEnvironment{Name: executor, Uid: id.String()},
			}}}, Model: "maintenance-no-provider", Cwd: "/",
		}},
		Executor: &identitypb.SandboxDestination{
			Target: &identitypb.EnvironmentTarget{Namespace: namespace, Name: executor, Uid: id.String()},
			Owner:  &identitypb.ServiceAccount{Namespace: namespace, Name: executor},
		},
	}
	if err := push.Send(&ingresspb.PushFrame{Frame: &ingresspb.PushFrame_SamplerAnnounce{SamplerAnnounce: announcement}}); err != nil {
		return err
	}
	ack, err := push.Recv()
	if err != nil {
		return err
	}
	if ack.Cursor > uint64(len(entries)) {
		return fmt.Errorf("unexpected existing cursor %d", ack.Cursor)
	}
	if !ack.Sealed {
		for _, entry := range entries[ack.Cursor:] {
			if err := push.Send(&ingresspb.PushFrame{Frame: &ingresspb.PushFrame_Entry{Entry: entry}}); err != nil {
				return err
			}
		}
		if err := push.Send(&ingresspb.PushFrame{Frame: &ingresspb.PushFrame_Sealed{Sealed: &sessionpb.Sealed{Cursor: uint64(len(entries))}}}); err != nil {
			return err
		}
	}
	if err := push.CloseSend(); err != nil {
		return err
	}
	for !ack.Sealed {
		ack, err = push.Recv()
		if err == io.EOF {
			return fmt.Errorf("ingress closed before acknowledging the seal")
		}
		if err != nil {
			return err
		}
	}
	client := historypb.NewHistoryServiceClient(connection)
	for _, page := range []struct {
		after uint64
		limit uint32
	}{{0, 1000}, {17, 11}} {
		response, err := client.ReadSessionEvents(readCtx, &historypb.ReadSessionEventsRequest{SessionId: id.String(), AfterCursor: page.after, Limit: page.limit})
		if err != nil {
			return err
		}
		want := entries[page.after:min(uint64(len(entries)), page.after+uint64(page.limit))]
		if len(response.Entries) != len(want) {
			return fmt.Errorf("readback returned %d events, expected %d", len(response.Entries), len(want))
		}
		for index, entry := range response.Entries {
			if !proto.Equal(entry, want[index]) {
				return fmt.Errorf("readback event %d differs", entry.Cursor)
			}
		}
	}
	return json.NewEncoder(os.Stdout).Encode(map[string]any{"session_id": id, "acknowledged": ack.Cursor, "sealed": ack.Sealed, "verified_events": len(entries), "inside_block_page_verified": true})
}

func main() {
	if err := run(); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
