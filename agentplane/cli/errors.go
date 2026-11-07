package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"net/http"
	"regexp"
	"strings"

	"google.golang.org/genproto/googleapis/rpc/code"
	"google.golang.org/genproto/googleapis/rpc/errdetails"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

// Exit statuses. Those below 11 are shared by every command group; docs/agent_cli.md owns the meanings.
const (
	exitRefused          = 1
	exitUsage            = 2
	exitTransient        = 3
	exitConflict         = 4
	exitNotFound         = 5
	exitDeadline         = 10
	exitDenied           = 11
	exitFailed           = 12
	exitCancelled        = 13
	exitExecutionUnknown = 14
	exitSubmitLost       = 15
	exitTooLate          = 16
	exitAlreadyFinished  = 17
)

// Who refused a call. The proxy and the Pod sidecar mark their own refusals with these headers.
const (
	sourceService = "service"
	sourceEgress  = "egress"
	sourceSidecar = "sidecar"
	sourceClient  = "client"

	egressHeader  = "x-agentplane-egress"
	sidecarHeader = "x-agentplane-egress-sidecar"
)

var (
	// errInterrupted means SIGINT or SIGTERM ended the command; the exit status is 128 plus the signal.
	errInterrupted = errors.New("interrupted")

	reasonPattern = regexp.MustCompile(`reason=([^;\s\\]+)`)
)

// outcome is a nonzero exit whose answer the command has already printed on stdout.
type outcome int

func (o outcome) Error() string { return fmt.Sprintf("exit status %d", int(o)) }

// exitWith ends a command with status code; 0 is success.
func exitWith(code int) error {
	if code == 0 {
		return nil
	}
	return outcome(code)
}

// apiError is the error envelope's body. Common absent fields are null; lifecycle details appear
// only when the resume backend supplies them.
type apiError struct {
	Code              string          `json:"code"`
	Message           string          `json:"message"`
	Source            string          `json:"source"`
	HTTPStatus        *int            `json:"http_status"`
	Reason            *string         `json:"reason"`
	RetryAfterSeconds *int            `json:"retry_after_seconds"`
	ResumeEligibility json.RawMessage `json:"resume_eligibility,omitempty"`
	WakeAssessment    json.RawMessage `json:"wake_assessment,omitempty"`
}

type envelope struct {
	Error apiError `json:"error"`
}

// failure is a command that did not get its answer: the envelope for stderr and the exit status.
type failure struct {
	apiError
	exit int
	// sent is whether the request was completely written before the failure, so a write may have
	// been committed.
	sent bool
}

func (f *failure) Error() string {
	return fmt.Sprintf("%s: %s", f.Code, f.Message)
}

// ambiguous is whether a write that failed this way may still have been committed.
func (f *failure) ambiguous() bool {
	return f.sent && (f.HTTPStatus == nil || *f.HTTPStatus >= http.StatusInternalServerError)
}

func grpcFailure(ctx context.Context, err error) error {
	if errors.Is(ctx.Err(), context.Canceled) {
		return errInterrupted
	}
	st, ok := status.FromError(err)
	if !ok {
		return clientFailure(exitTransient, "UNAVAILABLE", "%v", err)
	}
	grpcCode := st.Code().String()
	if name, ok := code.Code_name[int32(st.Code())]; ok {
		grpcCode = name
	}
	f := &failure{apiError: apiError{Code: grpcCode, Message: st.Message(), Source: sourceService}}
	if f.Message == "" {
		f.Message = grpcCode
	}
	// grpc-go represents failed CONNECT handshakes and HTTP responses that are not gRPC as
	// status errors too. Keep transport failures distinct from an Action Service status. A CONNECT
	// refusal carries the proxy's reason header in its error text, so preserve that diagnostic.
	message := strings.ToLower(f.Message)
	switch {
	case strings.Contains(message, "x-agentplane-egress-sidecar:"):
		f.Source = sourceSidecar
		if match := reasonPattern.FindStringSubmatch(f.Message); match != nil {
			f.Reason = &match[1]
		}
	case strings.Contains(message, "x-agentplane-egress:"):
		f.Source = sourceEgress
		if match := reasonPattern.FindStringSubmatch(f.Message); match != nil {
			f.Reason = &match[1]
		}
	case transportStatus(st.Code(), f.Message):
		f.Source = sourceClient
	}
	switch st.Code() {
	case codes.NotFound:
		f.exit = exitNotFound
	case codes.AlreadyExists, codes.Aborted:
		f.exit = exitConflict
	case codes.ResourceExhausted, codes.Unavailable, codes.DeadlineExceeded, codes.Canceled:
		f.exit = exitTransient
	case codes.InvalidArgument, codes.Unauthenticated, codes.PermissionDenied, codes.FailedPrecondition, codes.OutOfRange:
		f.exit = exitRefused
	default:
		f.exit = exitRefused
	}
	switch st.Code() {
	case codes.Unknown, codes.Internal, codes.Unavailable, codes.DeadlineExceeded, codes.Canceled:
		f.sent = true
	}
	if strings.Contains(message, "failed to do connect handshake") ||
		strings.Contains(message, "transport: error while dialing:") ||
		strings.Contains(message, "unexpected http status code received from server:") {
		f.sent = false
	}
	for _, detail := range st.Details() {
		if retry, ok := detail.(*errdetails.RetryInfo); ok && retry.GetRetryDelay() != nil {
			seconds := max(0, int(math.Ceil(retry.GetRetryDelay().AsDuration().Seconds())))
			f.RetryAfterSeconds = &seconds
		}
	}
	return f
}

// transportStatus recognizes grpc-go's transport-generated messages. It keeps a service's own
// UNAVAILABLE/DEADLINE_EXCEEDED status classified as a service response.
func transportStatus(code codes.Code, message string) bool {
	lower := strings.ToLower(message)
	return strings.Contains(lower, "connection error:") ||
		strings.Contains(lower, "failed to do connect handshake") ||
		strings.Contains(lower, "unexpected http status code received from server:") ||
		strings.Contains(lower, "transport: error while dialing:") ||
		strings.Contains(lower, "context deadline exceeded") ||
		(code == codes.Unavailable && strings.Contains(lower, "transport is closing"))
}

func clientFailure(exit int, code, format string, args ...any) *failure {
	return &failure{exit: exit, apiError: apiError{Code: code, Message: fmt.Sprintf(format, args...), Source: sourceClient}}
}

func usageFailure(format string, args ...any) *failure {
	return clientFailure(exitUsage, "INVALID_ARGUMENT", format, args...)
}

func invalidResponse(format string, args ...any) *failure {
	return clientFailure(exitRefused, "INTERNAL", "unusable response: "+format, args...)
}
