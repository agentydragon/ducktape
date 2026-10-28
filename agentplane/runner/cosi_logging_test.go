package runner

import (
	"bytes"
	"encoding/json"
	"errors"
	"log/slog"
	"testing"
	"time"

	"go.uber.org/zap"
	"go.uber.org/zap/zapcore"
)

func TestCosiLoggerForwardsThroughDefaultSlogHandler(t *testing.T) {
	var output bytes.Buffer
	previous := slog.Default()
	slog.SetDefault(slog.New(slog.NewJSONHandler(&output, &slog.HandlerOptions{Level: slog.LevelInfo})))
	t.Cleanup(func() { slog.SetDefault(previous) })

	logger := cosiLogger().Named("runtime").With(
		zap.String("runner_id", "runner-1"),
		zap.Namespace("identity"),
		zap.String("session", "session-1"),
	)
	logger.Debug("filtered")

	loggedAt := time.Date(2026, time.October, 27, 18, 15, 30, 123456789, time.UTC)
	if err := logger.Core().Write(zapcore.Entry{
		Time:       loggedAt,
		Level:      zapcore.WarnLevel,
		Message:    "COSI warning",
		LoggerName: "runtime",
	}, []zap.Field{
		zap.Namespace("details"),
		zap.Int("attempt", 2),
		zap.Error(errors.New("temporary failure")),
	}); err != nil {
		t.Fatal(err)
	}
	if err := logger.Core().Write(zapcore.Entry{
		Time:       loggedAt.Add(time.Second),
		Level:      zapcore.ErrorLevel,
		Message:    "COSI error",
		LoggerName: "runtime",
	}, nil); err != nil {
		t.Fatal(err)
	}

	lines := bytes.Split(bytes.TrimSpace(output.Bytes()), []byte("\n"))
	if len(lines) != 2 {
		t.Fatalf("forwarded %d records, want 2: %s", len(lines), output.String())
	}

	var warning map[string]interface{}
	if err := json.Unmarshal(lines[0], &warning); err != nil {
		t.Fatal(err)
	}
	if warning["level"] != "WARN" || warning["msg"] != "COSI warning" || warning["logger"] != "runtime" {
		t.Fatalf("warning record = %v", warning)
	}
	gotTime, err := time.Parse(time.RFC3339Nano, warning["time"].(string))
	if err != nil || !gotTime.Equal(loggedAt) {
		t.Fatalf("warning timestamp = %v, want %v (parse error %v)", warning["time"], loggedAt, err)
	}
	if warning["runner_id"] != "runner-1" {
		t.Fatalf("bound field = %v, want runner-1", warning["runner_id"])
	}
	identity, ok := warning["identity"].(map[string]interface{})
	if !ok || identity["session"] != "session-1" {
		t.Fatalf("bound namespace = %v, want session=session-1", warning["identity"])
	}
	details, ok := warning["details"].(map[string]interface{})
	if !ok || details["attempt"] != float64(2) || details["error"] != "temporary failure" {
		t.Fatalf("entry namespace and error = %v, want attempt=2 and error text", warning["details"])
	}

	var errorRecord map[string]interface{}
	if err := json.Unmarshal(lines[1], &errorRecord); err != nil {
		t.Fatal(err)
	}
	if errorRecord["level"] != "ERROR" || errorRecord["msg"] != "COSI error" {
		t.Fatalf("error record = %v", errorRecord)
	}
}
