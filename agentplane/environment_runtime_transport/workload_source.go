package environment_runtime_transport

import (
	"context"
	"fmt"
	"log/slog"

	"github.com/spiffe/go-spiffe/v2/workloadapi"
)

func newWorkloadAPISource(ctx context.Context, address string) (credentialSource, func() error, error) {
	startup, cancel := context.WithTimeout(ctx, workloadAPISourceStartupTimeout)
	defer cancel()
	source, err := workloadapi.NewX509Source(startup, workloadapi.WithClientOptions(
		workloadapi.WithAddr(address), workloadapi.WithLogger(workloadAPILogger{}),
	))
	if err != nil {
		return nil, nil, fmt.Errorf("open Workload API X.509 source: %w", err)
	}
	return source, source.Close, nil
}

type workloadAPILogger struct{}

func (workloadAPILogger) Debugf(format string, args ...any) { slog.Debug(fmt.Sprintf(format, args...)) }

func (workloadAPILogger) Infof(format string, args ...any) { slog.Info(fmt.Sprintf(format, args...)) }

func (workloadAPILogger) Warnf(format string, args ...any) { slog.Warn(fmt.Sprintf(format, args...)) }

func (workloadAPILogger) Errorf(format string, args ...any) { slog.Error(fmt.Sprintf(format, args...)) }
