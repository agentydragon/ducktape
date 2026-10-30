package httperror

import (
	"bytes"
	"errors"
	"log/slog"
	"strings"
	"testing"

	"github.com/danielgtaylor/huma/v2"
)

func TestLogServerErrorRecordsTheCauseOfServerErrorsOnly(t *testing.T) {
	var logs bytes.Buffer
	previous := slog.Default()
	slog.SetDefault(slog.New(slog.NewTextHandler(&logs, nil)))
	t.Cleanup(func() { slog.SetDefault(previous) })

	cause := errors.New("dial tcp 10.0.0.1:8080: connect: connection refused")
	unavailable := huma.Error503ServiceUnavailable("Upstream service unavailable")
	if got := LogServerError(unavailable, cause); got != unavailable {
		t.Fatalf("LogServerError returned %v, want the mapped error unchanged", got)
	}
	if !strings.Contains(logs.String(), "connection refused") {
		t.Fatalf("503 did not log its cause: %q", logs.String())
	}

	logs.Reset()
	LogServerError(huma.Error404NotFound("no thread"), cause)
	if logs.Len() != 0 {
		t.Fatalf("404 logged: %q", logs.String())
	}
}
