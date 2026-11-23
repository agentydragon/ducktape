package invocation

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
	ExitRefused          = 1
	ExitUsage            = 2
	ExitTransient        = 3
	ExitConflict         = 4
	ExitNotFound         = 5
	ExitDeadline         = 10
	ExitDenied           = 11
	ExitFailed           = 12
	ExitCancelled        = 13
	ExitExecutionUnknown = 14
	ExitSubmitLost       = 15
	ExitTooLate          = 16
	ExitAlreadyFinished  = 17
)

// Who refused a call. The proxy and the Pod sidecar mark their own refusals with the
// `x-agentplane-egress` and `x-agentplane-egress-sidecar` headers.
const (
	sourceService = "service"
	sourceEgress  = "egress"
	sourceSidecar = "sidecar"
	sourceClient  = "client"
)

var (
	// ErrInterrupted means SIGINT or SIGTERM ended the command; the exit status is 128 plus the signal.
	ErrInterrupted = errors.New("interrupted")

	reasonPattern = regexp.MustCompile(`reason=([^;\s\\]+)`)
)

// Outcome is a nonzero exit whose answer the command has already printed on stdout.
type Outcome int

func (o Outcome) Error() string { return fmt.Sprintf("exit status %d", int(o)) }

// ExitWith ends a command with status code; 0 is success.
func ExitWith(code int) error {
	if code == 0 {
		return nil
	}
	return Outcome(code)
}

// APIError is the error envelope's body. Common absent fields are null; lifecycle details appear
// only when the resume backend supplies them.
type APIError struct {
	Code              string          `json:"code"`
	Message           string          `json:"message"`
	Source            string          `json:"source"`
	HTTPStatus        *int            `json:"http_status"`
	Reason            *string         `json:"reason"`
	RetryAfterSeconds *int            `json:"retry_after_seconds"`
	ResumeEligibility json.RawMessage `json:"resume_eligibility,omitempty"`
	WakeAssessment    json.RawMessage `json:"wake_assessment,omitempty"`
}

type Envelope struct {
	Error APIError `json:"error"`
}

// Failure is a command that did not get its answer: the envelope for stderr and the exit status.
type Failure struct {
	APIError
	Exit int
	// Sent is whether the request was completely written before the failure, so a write may have
	// been committed.
	Sent bool
}

func (f *Failure) Error() string {
	return fmt.Sprintf("%s: %s", f.Code, f.Message)
}

// Ambiguous is whether a write that failed this way may still have been committed.
func (f *Failure) Ambiguous() bool {
	return f.Sent && (f.HTTPStatus == nil || *f.HTTPStatus >= http.StatusInternalServerError)
}

func GRPCFailure(ctx context.Context, err error) error {
	if errors.Is(ctx.Err(), context.Canceled) {
		return ErrInterrupted
	}
	st, ok := status.FromError(err)
	if !ok {
		return ClientFailure(ExitTransient, "UNAVAILABLE", "%v", err)
	}
	grpcCode := st.Code().String()
	if name, ok := code.Code_name[int32(st.Code())]; ok {
		grpcCode = name
	}
	f := &Failure{APIError: APIError{Code: grpcCode, Message: st.Message(), Source: sourceService}}
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
		f.Exit = ExitNotFound
	case codes.AlreadyExists, codes.Aborted:
		f.Exit = ExitConflict
	case codes.ResourceExhausted, codes.Unavailable, codes.DeadlineExceeded, codes.Canceled:
		f.Exit = ExitTransient
	case codes.InvalidArgument, codes.Unauthenticated, codes.PermissionDenied, codes.FailedPrecondition, codes.OutOfRange:
		f.Exit = ExitRefused
	default:
		f.Exit = ExitRefused
	}
	switch st.Code() {
	case codes.Unknown, codes.Internal, codes.Unavailable, codes.DeadlineExceeded, codes.Canceled:
		f.Sent = true
	}
	if strings.Contains(message, "failed to do connect handshake") ||
		strings.Contains(message, "transport: error while dialing:") ||
		strings.Contains(message, "unexpected http status code received from server:") {
		f.Sent = false
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

func ClientFailure(Exit int, code, format string, args ...any) *Failure {
	return &Failure{Exit: Exit, APIError: APIError{Code: code, Message: fmt.Sprintf(format, args...), Source: sourceClient}}
}

func UsageFailure(format string, args ...any) *Failure {
	return ClientFailure(ExitUsage, "INVALID_ARGUMENT", format, args...)
}

func InvalidResponse(format string, args ...any) *Failure {
	return ClientFailure(ExitRefused, "INTERNAL", "unusable response: "+format, args...)
}
