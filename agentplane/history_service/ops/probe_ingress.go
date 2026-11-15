// probe_ingress verifies live packing with one explicit, dedicated synthetic journal. It starts no
// harness or provider request. Operator-supplied service certificates authenticate the sampler
// writer and a configured backend reader; both peers verify the History server SVID.
package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"os"
	"time"

	historypb "github.com/agentydragon/ducktape/agentplane/history_service/api/history/history_servicepb"
	ingresspb "github.com/agentydragon/ducktape/agentplane/history_service/api/ingress/session_ingresspb"
	"github.com/agentydragon/ducktape/agentplane/history_service/testing/spans"
	transport "github.com/agentydragon/ducktape/agentplane/net/peertls"
	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	"github.com/google/uuid"
	"google.golang.org/grpc"
	"google.golang.org/protobuf/proto"
)

func dial(target, caFile, certFile, keyFile, clientURI string) (*grpc.ClientConn, error) {
	credentials, err := (&transport.ClientTLS{
		CAFile: caFile, ServerURI: transport.HistoryIngressURI,
		CertFile: certFile, KeyFile: keyFile, ClientURI: clientURI,
	}).Credentials()
	if err != nil {
		return nil, err
	}
	return grpc.NewClient(target, grpc.WithTransportCredentials(credentials), grpc.WithDisableRetry())
}

func run() error {
	target := flag.String("target", "", "History RPC host:port")
	caFile := flag.String("ca-file", "", "absolute path to workload trust roots")
	writerCert := flag.String("writer-cert-file", "", "absolute path to the sampler service certificate")
	writerKey := flag.String("writer-key-file", "", "absolute path to the sampler private key")
	readerCert := flag.String("reader-cert-file", "", "absolute path to an admitted reader service certificate")
	readerKey := flag.String("reader-key-file", "", "absolute path to the reader private key")
	readerURI := flag.String("reader-uri", transport.AppURI, "exact configured reader service SPIFFE URI")
	session := flag.String("session-id", "", "dedicated synthetic Session UUID, retained for retry/readback")
	flag.Parse()
	id, err := uuid.Parse(*session)
	if err != nil || id == uuid.Nil || *target == "" || *caFile == "" {
		return fmt.Errorf("target, CA and explicit Session UUID are required")
	}
	writer, err := dial(*target, *caFile, *writerCert, *writerKey, transport.CentralSamplerURI)
	if err != nil {
		return fmt.Errorf("writer TLS: %w", err)
	}
	defer writer.Close()
	reader, err := dial(*target, *caFile, *readerCert, *readerKey, *readerURI)
	if err != nil {
		return fmt.Errorf("reader TLS: %w", err)
	}
	defer reader.Close()
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
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
	push, err := ingresspb.NewSessionJournalIngressClient(writer).Push(ctx)
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
	client := historypb.NewHistoryServiceClient(reader)
	for _, page := range []struct {
		after uint64
		limit uint32
	}{{0, 1000}, {17, 11}} {
		response, err := client.ReadSessionEvents(ctx, &historypb.ReadSessionEventsRequest{SessionId: id.String(), AfterCursor: page.after, Limit: page.limit})
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
