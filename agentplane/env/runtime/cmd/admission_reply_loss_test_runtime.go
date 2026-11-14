package main

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sync"

	environmentruntimepb "github.com/agentydragon/ducktape/agentplane/env/runtime/control/environmentruntimepb"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/internal/coordinator"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
)

const admissionReplyLossDirectory = "admission-reply-loss-test"

type admissionAttempt struct {
	CommandID    string `json:"command_id"`
	RequestHash  string `json:"request_sha256"`
	Request      []byte `json:"request"`
	Response     []byte `json:"response,omitempty"`
	HandlerCode  string `json:"handler_code"`
	ReturnedCode string `json:"returned_code"`
}

// This replacement factory is linked only into the focused native test executable. Authorization
// still runs in control.Server before this interceptor; the fault is after Runtime's real handler.
func newRuntime(config coordinator.Config) (*coordinator.Runtime, error) {
	directory := filepath.Join(config.StateDir, admissionReplyLossDirectory)
	if err := os.MkdirAll(directory, 0700); err != nil {
		return nil, err
	}
	var mu sync.Mutex
	attempts := 0
	discarded := false
	config.ControlInterceptor = func(ctx context.Context, request any, info *grpc.UnaryServerInfo, handler grpc.UnaryHandler) (any, error) {
		submit, ok := request.(*environmentruntimepb.SubmitInputRequest)
		if !ok {
			return handler(ctx, request)
		}
		armed, err := os.ReadFile(filepath.Join(directory, "command"))
		if errors.Is(err, os.ErrNotExist) {
			return handler(ctx, request)
		}
		if err != nil {
			return nil, status.Error(codes.Internal, "read admission reply-loss command: "+err.Error())
		}
		if string(armed) != submit.GetCommandId() {
			return handler(ctx, request)
		}

		// Serialize only this test command so each recorded handler result has an unambiguous order.
		mu.Lock()
		defer mu.Unlock()
		response, handlerErr := handler(ctx, request)
		attempts++
		requestBytes, marshalErr := proto.Marshal(submit)
		if marshalErr != nil {
			return nil, status.Error(codes.Internal, "marshal admission reply-loss request: "+marshalErr.Error())
		}
		record := admissionAttempt{
			CommandID: submit.GetCommandId(),
			RequestHash: func() string {
				hash := sha256.Sum256(requestBytes)
				return hex.EncodeToString(hash[:])
			}(),
			Request:      requestBytes,
			HandlerCode:  status.Code(handlerErr).String(),
			ReturnedCode: status.Code(handlerErr).String(),
		}
		if handlerErr == nil {
			actual, responseOK := response.(*environmentruntimepb.SubmitInputResponse)
			if !responseOK || actual.GetAdmission() == nil {
				return nil, status.Error(codes.Internal, "real SubmitInput handler returned no admission")
			}
			record.Response, marshalErr = proto.Marshal(actual)
			if marshalErr != nil {
				return nil, status.Error(codes.Internal, "marshal admission reply-loss response: "+marshalErr.Error())
			}
			if !discarded {
				discarded = true
				record.ReturnedCode = codes.Unavailable.String()
			}
		}
		if err := writeAdmissionAttempt(directory, attempts, record); err != nil {
			return nil, status.Error(codes.Internal, "record admission reply-loss attempt: "+err.Error())
		}
		if handlerErr != nil {
			return response, handlerErr
		}
		if record.ReturnedCode == codes.Unavailable.String() {
			return nil, status.Error(codes.Unavailable, "test discarded the committed Runtime admission response")
		}
		return response, nil
	}
	return coordinator.New(config)
}

func writeAdmissionAttempt(directory string, attempt int, record admissionAttempt) error {
	contents, err := json.Marshal(record)
	if err != nil {
		return fmt.Errorf("encode attempt: %w", err)
	}
	temporary, err := os.CreateTemp(directory, ".attempt-*.tmp")
	if err != nil {
		return fmt.Errorf("create temporary record: %w", err)
	}
	name := temporary.Name()
	defer func() { _ = os.Remove(name) }()
	if _, err := temporary.Write(contents); err != nil {
		_ = temporary.Close()
		return fmt.Errorf("write record: %w", err)
	}
	if err := temporary.Sync(); err != nil {
		_ = temporary.Close()
		return fmt.Errorf("sync record: %w", err)
	}
	if err := temporary.Close(); err != nil {
		return fmt.Errorf("close record: %w", err)
	}
	path := filepath.Join(directory, fmt.Sprintf("attempt-%02d.json", attempt))
	if err := os.Rename(name, path); err != nil {
		return fmt.Errorf("publish record: %w", err)
	}
	return nil
}
