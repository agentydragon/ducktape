// Package status checks the integration app's dependent services for the System page.
package status

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"time"

	healthpb "google.golang.org/grpc/health/grpc_health_v1"
	"google.golang.org/grpc/status"
)

type ComponentStatus struct {
	Name      string    `json:"name"`
	State     string    `json:"state"`
	Detail    *string   `json:"detail"`
	CheckedAt time.Time `json:"checked_at"`
}

type Probe struct {
	Name  string
	Check func(context.Context) error
}

func GRPCHealthProbe(name string, connection healthpb.HealthClient) Probe {
	return Probe{Name: name, Check: func(ctx context.Context) error {
		response, err := connection.Check(ctx, &healthpb.HealthCheckRequest{})
		if err != nil {
			return err
		}
		if response.GetStatus() != healthpb.HealthCheckResponse_SERVING {
			return fmt.Errorf("%s", response.GetStatus().String())
		}
		return nil
	}}
}

func HTTPHealthProbe(name string, client *http.Client, path string) Probe {
	return Probe{Name: name, Check: func(ctx context.Context) error {
		request, err := http.NewRequestWithContext(ctx, http.MethodGet, path, nil)
		if err != nil {
			return err
		}
		response, err := client.Do(request)
		if err != nil {
			return err
		}
		_ = response.Body.Close()
		if response.StatusCode < http.StatusOK || response.StatusCode >= http.StatusMultipleChoices {
			return fmt.Errorf("HTTP %d %s", response.StatusCode, http.StatusText(response.StatusCode))
		}
		return nil
	}}
}

type Checker struct {
	probes  []Probe
	timeout time.Duration
	now     func() time.Time
}

func New(probes []Probe, timeout time.Duration) (*Checker, error) {
	if timeout <= 0 {
		return nil, fmt.Errorf("system status timeout must be positive")
	}
	for _, probe := range probes {
		if probe.Name == "" || probe.Check == nil {
			return nil, fmt.Errorf("system status probes require a name and check")
		}
	}
	return &Checker{probes: append([]Probe(nil), probes...), timeout: timeout, now: time.Now}, nil
}

func (c *Checker) CheckAll(ctx context.Context) []ComponentStatus {
	results := make([]ComponentStatus, len(c.probes))
	done := make(chan struct{}, len(c.probes))
	for index, probe := range c.probes {
		go func(index int, probe Probe) {
			checkCtx, cancel := context.WithTimeout(ctx, c.timeout)
			err := probe.Check(checkCtx)
			cancel()
			result := ComponentStatus{Name: probe.Name, State: "ok", CheckedAt: c.now().UTC()}
			if err != nil {
				result.State = "down"
				detail := errorDetail(err, c.timeout)
				result.Detail = &detail
			}
			results[index] = result
			done <- struct{}{}
		}(index, probe)
	}
	for range c.probes {
		<-done
	}
	return results
}

func errorDetail(err error, timeout time.Duration) string {
	if errors.Is(err, context.DeadlineExceeded) {
		return fmt.Sprintf("no answer within %gs", timeout.Seconds())
	}
	if grpcStatus, ok := status.FromError(err); ok {
		return fmt.Sprintf("%s: %s", grpcStatus.Code(), grpcStatus.Message())
	}
	return fmt.Sprintf("%T: %v", err, err)
}
