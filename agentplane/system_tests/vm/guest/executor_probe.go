// The OOM scenario opens a native session before pressure and resumes its exact
// ID afterward, through the VM's loopback execution backend.
package main

import (
	"context"
	"errors"
	"fmt"
	"log"
	"net/url"
	"os"
	"time"

	executor "github.com/agentydragon/ducktape/agentplane/env/runtime/execution/executorpb"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
)

func main() {
	if len(os.Args) > 2 {
		log.Fatal("expected at most one native session ID to resume")
	}
	var resume *string
	if len(os.Args) == 2 {
		resume = &os.Args[1]
	}
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	id, err := probe(ctx, resume)
	if err != nil {
		log.Fatal(err)
	}
	fmt.Println("VM_EXECUTION_SESSION", id)
}

func probe(ctx context.Context, resume *string) (string, error) {
	conn, err := grpc.NewClient("passthrough:///127.0.0.1:4501", grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		return "", err
	}
	defer conn.Close()
	client := executor.NewExecutorClient(conn)
	stream, err := client.OpenSession(ctx, &executor.OpenSessionRequest{RequestId: "vm-oom-open", ResumeSessionId: resume})
	if err != nil {
		return "", err
	}
	opened, err := stream.Recv()
	if err != nil {
		return "", err
	}
	session := opened.GetExecutionSessionId()
	if opened.GetStatus().GetOutcome() != executor.ExecutionStatus_OUTCOME_COMPLETED || session == "" {
		return "", fmt.Errorf("open native execution session: %v", opened.GetStatus())
	}
	if resume != nil {
		if session != *resume {
			return "", fmt.Errorf("resumed session %q, expected %q", session, *resume)
		}
		if err := runFollowUp(ctx, client, session); err != nil {
			return "", err
		}
	}
	return session, nil
}

func runFollowUp(ctx context.Context, client executor.ExecutorClient, session string) error {
	dir, err := os.Getwd()
	if err != nil {
		return err
	}
	cwd := (&url.URL{Scheme: "file", Path: dir}).String()
	const processID = "vm-oom-followup"
	const marker = "same-native-session"
	started, err := client.StartProcess(ctx, &executor.StartProcessRequest{
		RequestId: "vm-oom-start", ExecutionSessionId: session, ProcessId: processID,
		Argv: []string{"/bin/sh", "-c", "printf '%s' " + marker}, Cwd: cwd,
	})
	if err != nil {
		return err
	}
	if started.GetStatus().GetOutcome() != executor.ExecutionStatus_OUTCOME_COMPLETED || started.GetProcessId() != processID {
		return fmt.Errorf("start follow-up command: %v", started)
	}
	request := &executor.ReadProcessRequest{
		RequestId: "vm-oom-read", ExecutionSessionId: session, ProcessId: processID, MaxBytes: 4096, WaitMs: 500,
	}
	var output []byte
	for {
		result, err := client.ReadProcess(ctx, request)
		if err != nil {
			return err
		}
		if result.GetStatus().GetOutcome() != executor.ExecutionStatus_OUTCOME_COMPLETED || result.Failure != nil {
			return fmt.Errorf("read follow-up command: %v", result)
		}
		for _, chunk := range result.GetChunks() {
			output = append(output, chunk.GetData()...)
		}
		if result.GetClosed() {
			if result.ExitCode == nil || result.GetExitCode() != 0 || string(output) != marker {
				return fmt.Errorf("follow-up command failed: %v, output %q", result, output)
			}
			return nil
		}
		if next := result.GetNextSeq(); next != 0 {
			cursor := next - 1
			request.AfterSeq = &cursor
		}
		if len(output) > len(marker) {
			return errors.New("follow-up command returned unexpected output")
		}
	}
}
