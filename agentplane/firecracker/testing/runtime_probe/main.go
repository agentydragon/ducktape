// runtime_probe observes a disposable guest over the real Gateway mTLS protocol.
package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"

	"github.com/agentydragon/ducktape/agentplane/env/runtime/control"
	pb "github.com/agentydragon/ducktape/agentplane/env/runtime/control/environmentruntimepb"
	executorpb "github.com/agentydragon/ducktape/agentplane/env/runtime/execution/executorpb"
	transport "github.com/agentydragon/ducktape/agentplane/environment_runtime_transport"
	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	"google.golang.org/grpc"
	"google.golang.org/grpc/metadata"
)

const probeCallTimeout = 15 * time.Second

type openSessionResult struct {
	response *executorpb.OpenSessionResponse
	err      error
}

type observedChunk struct {
	Seq    uint64 `json:"seq"`
	Stream string `json:"stream"`
	Data   string `json:"data"`
}

func run() error {
	address := flag.String("address", "192.0.2.2:9090", "guest Runtime endpoint")
	tlsDir := flag.String("tls-dir", "", "directory containing Gateway tls.crt, tls.key and ca.crt")
	namespace := flag.String("namespace", "firecracker-environment-proof", "Sandbox namespace")
	sandbox := flag.String("sandbox-uid", "proof-sandbox", "Sandbox UID")
	pod := flag.String("pod-uid", "proof-pod", "unchanged launcher Pod UID")
	credential := flag.String("credential-id", "proof-credential", "Runtime credential ID")
	epoch := flag.Uint64("epoch", 1, "strictly increasing connection epoch")
	expectedBoot := flag.String("expected-boot-id", "", "require this Runtime process identity after resume")
	execution := flag.String("execution", "", "optional Executor probe mode: start or resume")
	executionSessionID := flag.String("execution-session-id", "", "native execution session ID to resume")
	processID := flag.String("process-id", "ram-proof", "Executor process ID")
	hold := flag.Duration("hold", 0, "keep the native execution session open after reading output")
	afterSeq := flag.Uint64("after-seq", 0, "exclusive last-consumed output sequence for resume")
	flag.Parse()
	if *tlsDir == "" || flag.NArg() != 0 {
		return fmt.Errorf("--tls-dir is required; positional arguments are forbidden")
	}
	if *hold < 0 {
		return fmt.Errorf("--hold cannot be negative")
	}
	afterSeqSet := false
	flag.Visit(func(f *flag.Flag) {
		if f.Name == "after-seq" {
			afterSeqSet = true
		}
	})
	switch *execution {
	case "":
		if *executionSessionID != "" || afterSeqSet || *hold != 0 {
			return fmt.Errorf("--execution is required with --execution-session-id, --after-seq or --hold")
		}
	case "start":
		if *executionSessionID != "" || afterSeqSet {
			return fmt.Errorf("--execution-session-id and --after-seq are only valid with --execution=resume")
		}
	case "resume":
		if *executionSessionID == "" || !afterSeqSet {
			return fmt.Errorf("--execution=resume requires --execution-session-id and --after-seq")
		}
	default:
		return fmt.Errorf("--execution must be start or resume, got %q", *execution)
	}
	if *processID == "" {
		return fmt.Errorf("--process-id cannot be empty")
	}
	uri, err := transport.EnvironmentRuntimeURI(*namespace, *sandbox, *credential)
	if err != nil {
		return err
	}
	security := transport.Security{Mode: transport.MutualTLS, TLS: &transport.Config{
		CertFile: filepath.Join(*tlsDir, "tls.crt"), KeyFile: filepath.Join(*tlsDir, "tls.key"), CAFile: filepath.Join(*tlsDir, "ca.crt"),
	}}
	credentials, err := security.ClientCredentials(uri)
	if err != nil {
		return err
	}
	connection, err := grpc.NewClient(*address, grpc.WithTransportCredentials(credentials), grpc.WithDisableRetry())
	if err != nil {
		return err
	}
	defer connection.Close()
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	// WatchState owns the active epoch for the entire execution proof. Bound only
	// connection setup; expiring this context during --hold would fence our own RPCs.
	setupTimer := time.AfterFunc(probeCallTimeout, cancel)
	defer setupTimer.Stop()
	client := pb.NewEnvironmentRuntimeClient(connection)
	watch, err := client.WatchState(ctx, &pb.WatchStateRequest{SandboxUid: *sandbox, IncarnationUid: *pod, ConnectionEpoch: *epoch})
	if err != nil {
		return err
	}
	first, err := watch.Recv()
	if err != nil {
		return err
	}
	boot := first.GetEnvironmentRuntimeBootId()
	if boot == "" || (*expectedBoot != "" && boot != *expectedBoot) {
		return fmt.Errorf("Runtime boot ID %q differs from expected %q", boot, *expectedBoot)
	}
	if first.GetIncarnationUid() != *pod || first.GetConnectionEpoch() != *epoch {
		return fmt.Errorf("Runtime returned a different incarnation or connection epoch")
	}
	call := metadata.AppendToOutgoingContext(ctx, control.SandboxMetadata, *sandbox, control.IncarnationMetadata, *pod, control.BootMetadata, boot, control.EpochMetadata, strconv.FormatUint(*epoch, 10))
	status, err := client.GetStatus(call, &sessionpb.GetStatusRequest{})
	if err != nil {
		return err
	}
	if status.GetEnvironmentRuntimeBootId() != boot {
		return fmt.Errorf("GetStatus changed Runtime boot ID")
	}
	if !setupTimer.Stop() {
		return fmt.Errorf("Runtime setup exceeded %s", probeCallTimeout)
	}
	if *execution == "" {
		return json.NewEncoder(os.Stdout).Encode(map[string]any{"runtime_boot_id": boot, "pod_uid": *pod, "connection_epoch": *epoch, "status": "passed"})
	}
	return runExecution(*execution, *executionSessionID, *processID, *hold, afterSeqSet, *afterSeq, boot, *namespace, *sandbox, *pod, *epoch, connection)
}

func runExecution(mode, nativeSessionID, processID string, hold time.Duration, afterSeqSet bool, afterSeq uint64, bootID, namespace, sandboxUID, podUID string, epoch uint64, connection *grpc.ClientConn) error {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	call := metadata.AppendToOutgoingContext(ctx, control.SandboxMetadata, sandboxUID, control.IncarnationMetadata, podUID, control.BootMetadata, bootID, control.EpochMetadata, strconv.FormatUint(epoch, 10))
	client := executorpb.NewExecutorClient(connection)
	scope := &executorpb.ExecutionScope{
		Destination: &identitypb.SandboxDestination{
			Target: &identitypb.EnvironmentTarget{Namespace: namespace, Name: "proof", Uid: sandboxUID},
			Owner:  &identitypb.ServiceAccount{Namespace: namespace, Name: "proof"},
		},
		ExpectedIncarnationUid: podUID,
		SessionId:              "proof-execution",
	}
	var resumeID *string
	if mode == "resume" {
		resumeID = &nativeSessionID
	}
	openCtx, cancelOpen := context.WithCancel(call)
	stream, err := client.OpenSession(openCtx, &executorpb.OpenSessionRequest{
		Scope: scope, RequestId: fmt.Sprintf("runtime-probe-open-%d", epoch), ResumeSessionId: resumeID,
	})
	if err != nil {
		cancelOpen()
		return fmt.Errorf("Executor OpenSession RPC: %w", err)
	}
	defer cancelOpen()
	firstResponse, err := receiveOpenSession(openCtx, stream)
	if err != nil {
		cancelOpen()
		return fmt.Errorf("Executor OpenSession response: %w", err)
	}
	if err := requireCompleted("OpenSession", firstResponse.GetStatus()); err != nil {
		cancelOpen()
		return err
	}
	openedID := firstResponse.GetExecutionSessionId()
	if openedID == "" {
		cancelOpen()
		return fmt.Errorf("Executor OpenSession returned an empty native execution session ID")
	}
	if mode == "resume" && openedID != nativeSessionID {
		cancelOpen()
		return fmt.Errorf("Executor resumed native session %q as %q", nativeSessionID, openedID)
	}
	if err := emitEvidence(map[string]any{
		"event": "execution_session_opened", "runtime_boot_id": bootID, "connection_epoch": epoch,
		"execution_session_id": openedID, "sampler_session_id": scope.GetSessionId(),
		"process_id": processID, "mode": mode, "native_shell_path": firstResponse.GetShell().GetPath(),
	}); err != nil {
		cancelOpen()
		return err
	}
	if mode == "start" {
		startCtx, cancelStart := context.WithTimeout(call, probeCallTimeout)
		started, startErr := client.StartProcess(startCtx, &executorpb.StartProcessRequest{
			Scope: scope, ExecutionSessionId: openedID,
			RequestId: fmt.Sprintf("runtime-probe-start-%d", epoch), ProcessId: processID,
			Argv: []string{"/run/current-system/sw/bin/bash", "-c", longLivedProcessScript},
			Cwd:  "file:///state/workspaces", PipeStdin: true,
		})
		cancelStart()
		if startErr != nil {
			cancelOpen()
			return fmt.Errorf("Executor StartProcess RPC: %w", startErr)
		}
		if err := requireCompleted("StartProcess", started.GetStatus()); err != nil {
			cancelOpen()
			return err
		}
		if started.GetProcessId() != processID {
			cancelOpen()
			return fmt.Errorf("Executor started process %q, expected %q", started.GetProcessId(), processID)
		}
		if err := emitEvidence(map[string]any{
			"event": "process_started", "runtime_boot_id": bootID, "connection_epoch": epoch,
			"execution_session_id": openedID, "process_id": processID,
		}); err != nil {
			cancelOpen()
			return err
		}
	}
	notificationsDone := drainNotifications(stream)
	defer func() {
		cancelOpen()
		<-notificationsDone
	}()

	var cursor *uint64
	if afterSeqSet {
		cursor = &afterSeq
	}
	readCtx, cancelRead := context.WithTimeout(call, probeCallTimeout)
	read, nextCursor, err := readProcessOutput(readCtx, client, scope, openedID, processID, epoch, cursor)
	cancelRead()
	if err != nil {
		return err
	}
	chunks := make([]observedChunk, 0, len(read.GetChunks()))
	var combined strings.Builder
	for _, chunk := range read.GetChunks() {
		data := string(chunk.GetData())
		chunks = append(chunks, observedChunk{Seq: chunk.GetSeq(), Stream: chunk.GetStream(), Data: data})
		combined.WriteString(data)
	}
	for _, marker := range []string{"marker=", "counter=", "main_pid=", "child_pid="} {
		if !strings.Contains(combined.String(), marker) {
			return fmt.Errorf("Executor output is missing %q from the RAM proof process", marker)
		}
	}
	if nextCursor == nil {
		return fmt.Errorf("Executor ReadProcess returned output without a usable sequence cursor")
	}
	evidence := map[string]any{
		"event": "execution_probe_ready", "runtime_boot_id": bootID, "connection_epoch": epoch,
		"execution_session_id": openedID, "process_id": processID,
		"chunks": chunks, "next_seq": read.GetNextSeq(), "after_seq": *nextCursor,
		"exited": read.GetExited(), "closed": read.GetClosed(), "hold": hold.String(),
	}
	if err := emitEvidence(evidence); err != nil {
		return err
	}
	if hold > 0 {
		closed, err := holdOpenSession(ctx, hold, notificationsDone)
		if err != nil {
			return err
		}
		if closed {
			if err := emitEvidence(map[string]any{
				"event": "execution_session_stream_closed_during_hold", "runtime_boot_id": bootID,
				"connection_epoch": epoch, "execution_session_id": openedID, "process_id": processID, "mode": mode,
			}); err != nil {
				return err
			}
			if mode == "resume" {
				return fmt.Errorf("resumed native execution session stream closed during %s hold", hold)
			}
		}
	}
	return nil
}

const longLivedProcessScript = `IFS= read -r marker < /proc/sys/kernel/random/uuid
/run/current-system/sw/bin/sleep infinity &
child_pid=$!
counter=0
while :; do
  printf 'marker=%s counter=%s main_pid=%s child_pid=%s wall_ns=%s\n' "$marker" "$counter" "$$" "$child_pid" "$(/run/current-system/sw/bin/date +%s%N)"
  counter=$((counter + 1))
  /run/current-system/sw/bin/sleep 1
done`

func receiveOpenSession(ctx context.Context, stream executorpb.Executor_OpenSessionClient) (*executorpb.OpenSessionResponse, error) {
	result := make(chan openSessionResult, 1)
	go func() {
		response, err := stream.Recv()
		result <- openSessionResult{response: response, err: err}
	}()
	timer := time.NewTimer(probeCallTimeout)
	defer timer.Stop()
	select {
	case received := <-result:
		return received.response, received.err
	case <-timer.C:
		return nil, fmt.Errorf("no first response within %s", probeCallTimeout)
	case <-ctx.Done():
		return nil, ctx.Err()
	}
}

func drainNotifications(stream executorpb.Executor_OpenSessionClient) <-chan struct{} {
	done := make(chan struct{})
	go func() {
		defer close(done)
		for {
			if _, err := stream.Recv(); err != nil {
				return
			}
		}
	}()
	return done
}

func readProcessOutput(ctx context.Context, client executorpb.ExecutorClient, scope *executorpb.ExecutionScope, executionSessionID, processID string, epoch uint64, afterSeq *uint64) (*executorpb.ReadProcessResponse, *uint64, error) {
	for attempt := uint64(1); ; attempt++ {
		callCtx, cancel := context.WithTimeout(ctx, probeCallTimeout)
		response, err := client.ReadProcess(callCtx, &executorpb.ReadProcessRequest{
			Scope: scope, ExecutionSessionId: executionSessionID,
			RequestId: fmt.Sprintf("runtime-probe-read-%d-%d", epoch, attempt), ProcessId: processID,
			AfterSeq: afterSeq, MaxBytes: 65536, WaitMs: 1000,
		})
		cancel()
		if err != nil {
			return nil, nil, fmt.Errorf("Executor ReadProcess RPC: %w", err)
		}
		if err := requireCompleted("ReadProcess", response.GetStatus()); err != nil {
			return nil, nil, err
		}
		if response.GetFailure() != "" {
			return nil, nil, fmt.Errorf("Executor ReadProcess reported process failure: %s", response.GetFailure())
		}
		if response.GetExited() || response.GetClosed() {
			return nil, nil, fmt.Errorf("RAM proof process exited or closed before producing fresh output")
		}
		if afterSeq != nil {
			for _, chunk := range response.GetChunks() {
				if chunk.GetSeq() <= *afterSeq {
					return nil, nil, fmt.Errorf("ReadProcess returned sequence %d at or before cursor %d", chunk.GetSeq(), *afterSeq)
				}
			}
		}
		if len(response.GetChunks()) == 0 {
			continue
		}
		if response.GetNextSeq() == 0 {
			return nil, nil, fmt.Errorf("ReadProcess returned chunks without a next sequence")
		}
		cursor := response.GetNextSeq() - 1
		return response, &cursor, nil
	}
}

func requireCompleted(operation string, result *executorpb.ExecutionStatus) error {
	if result == nil {
		return fmt.Errorf("Executor %s returned no status", operation)
	}
	switch result.GetOutcome() {
	case executorpb.ExecutionStatus_OUTCOME_COMPLETED:
		return nil
	case executorpb.ExecutionStatus_OUTCOME_REJECTED:
		return fmt.Errorf("Executor %s was rejected: %s", operation, result.GetMessage())
	case executorpb.ExecutionStatus_OUTCOME_UNKNOWN:
		return fmt.Errorf("Executor %s outcome is unknown: %s", operation, result.GetMessage())
	default:
		return fmt.Errorf("Executor %s returned unexpected outcome %s: %s", operation, result.GetOutcome(), result.GetMessage())
	}
}

func emitEvidence(evidence map[string]any) error {
	return json.NewEncoder(os.Stdout).Encode(evidence)
}

func holdOpenSession(ctx context.Context, hold time.Duration, notificationsDone <-chan struct{}) (bool, error) {
	timer := time.NewTimer(hold)
	defer timer.Stop()
	select {
	case <-timer.C:
		return false, nil
	case <-notificationsDone:
		return true, nil
	case <-ctx.Done():
		return false, fmt.Errorf("native execution session hold ended: %w", ctx.Err())
	}
}

func main() {
	if err := run(); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
