package servicecmd

import (
	"bytes"
	"context"
	"errors"
	"log/slog"
	"strings"
	"testing"
)

func TestCommandRun(t *testing.T) {
	failure := errors.New("startup test error")
	for _, test := range []struct {
		name        string
		args        []string
		loadErr     error
		traceErr    bool
		serveErr    error
		wantLoad    bool
		wantServe   bool
		wantCode    int
		wantMessage string
	}{
		{name: "migrate only skips serving settings", args: []string{"--migrate-only"}, traceErr: true, wantCode: 1, wantMessage: "Example migration failed"},
		{name: "settings checked before tracing", loadErr: failure, traceErr: true, wantLoad: true, wantCode: 2, wantMessage: "invalid Example configuration"},
		{name: "tracing failure skips serve", traceErr: true, wantLoad: true, wantCode: 1, wantMessage: "Example tracing setup failed"},
		{name: "serving success", wantLoad: true, wantServe: true},
		{name: "cancellation is clean", serveErr: context.Canceled, wantLoad: true, wantServe: true},
		{name: "serving failure", serveErr: failure, wantLoad: true, wantServe: true, wantCode: 1, wantMessage: "Example stopped"},
	} {
		t.Run(test.name, func(t *testing.T) {
			t.Setenv("TEST_SERVICECMD_DATABASE_URL", "")
			if test.traceErr {
				t.Setenv("OTEL_SDK_DISABLED", "false")
				t.Setenv("OTEL_TRACES_EXPORTER", "invalid")
			} else {
				t.Setenv("OTEL_SDK_DISABLED", "true")
			}

			var logs bytes.Buffer
			previousLogger := slog.Default()
			slog.SetDefault(slog.New(slog.NewJSONHandler(&logs, nil)))
			t.Cleanup(func() { slog.SetDefault(previousLogger) })
			loaded, served := false, false
			command := Command[string]{
				Name: "Example", TraceName: "example", DatabaseURLEnv: "TEST_SERVICECMD_DATABASE_URL",
				LoadConfig: func() (string, error) {
					loaded = true
					return "settings", test.loadErr
				},
				Serve: func(_ context.Context, config string) error {
					served = true
					if config != "settings" {
						t.Fatalf("Serve got config %q", config)
					}
					return test.serveErr
				},
			}
			if code := command.Run(t.Context(), test.args); code != test.wantCode || loaded != test.wantLoad || served != test.wantServe {
				t.Fatalf("Run returned %d, loaded config = %t, served = %t; want %d, %t, %t", code, loaded, served, test.wantCode, test.wantLoad, test.wantServe)
			}
			if (test.wantMessage == "" && logs.Len() != 0) ||
				(test.wantMessage != "" && !strings.Contains(logs.String(), `"msg":"`+test.wantMessage+`"`)) {
				t.Fatalf("Run logged %q, want message %q", logs.String(), test.wantMessage)
			}
		})
	}
}
