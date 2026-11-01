package runtime

import (
	"context"
	"errors"
	"fmt"

	"github.com/agentydragon/ducktape/agentplane/env/supervision"
	systemd "github.com/coreos/go-systemd/v22/dbus"
)

type unitInvocation struct {
	ID             string
	ControlGroup   string
	RequireSuccess bool
}

func startSystemdUnit(ctx context.Context, name string) (unitInvocation, error) {
	conn, err := systemd.NewSystemdConnectionContext(context.Background())
	if err != nil {
		return unitInvocation{}, fmt.Errorf("connect systemd to start %s: %w", name, err)
	}
	defer conn.Close()
	// A unit left by a previous Runtime invocation must be fenced before the new
	// invocation can be accepted. Runtime retains the exact new InvocationID below.
	if err := fenceSystemdUnitOnConnection(ctx, conn, name, unitInvocation{}); err != nil {
		return unitInvocation{}, fmt.Errorf("fence previous %s invocation: %w", name, err)
	}
	completed := make(chan string, 1)
	if _, err := conn.StartUnitContext(ctx, name, "replace", completed); err != nil {
		fenceErr := fenceSystemdUnitOnConnection(context.Background(), conn, name, unitInvocation{})
		return unitInvocation{}, errors.Join(fmt.Errorf("start systemd unit %s: %w", name, err), fenceErr)
	}
	if err := supervision.WaitSystemdJob(ctx, name, "start", completed); err != nil {
		fenceErr := fenceSystemdUnitOnConnection(context.Background(), conn, name, unitInvocation{})
		return unitInvocation{}, errors.Join(err, fenceErr)
	}
	invocation, state, pid, err := readSystemdInvocation(ctx, conn, name)
	if err != nil {
		fenceErr := fenceSystemdUnitOnConnection(context.Background(), conn, name, invocation)
		return unitInvocation{}, errors.Join(err, fenceErr)
	}
	if state != "active" || pid == 0 || invocation.ID == "" || invocation.ControlGroup == "" {
		fenceErr := fenceSystemdUnitOnConnection(context.Background(), conn, name, invocation)
		return unitInvocation{}, errors.Join(fmt.Errorf("systemd unit %s did not enter a verifiable live invocation (state=%s, main_pid=%d, invocation=%q, cgroup=%q)", name, state, pid, invocation.ID, invocation.ControlGroup), fenceErr)
	}
	invocation.RequireSuccess = true
	return invocation, nil
}

func fenceSystemdUnit(ctx context.Context, name string, expected unitInvocation) error {
	conn, err := systemd.NewSystemdConnectionContext(ctx)
	if err != nil {
		return fmt.Errorf("connect systemd to fence %s: %w", name, err)
	}
	defer conn.Close()
	return fenceSystemdUnitOnConnection(ctx, conn, name, expected)
}

func fenceSystemdUnitOnConnection(ctx context.Context, conn *systemd.Conn, name string, expected unitInvocation) error {
	return supervision.FenceSystemdUnit(ctx, conn, name, supervision.SystemdFence{InvocationID: expected.ID, ControlGroup: expected.ControlGroup, RequireSuccess: expected.RequireSuccess, Ordered: true})
}

func readSystemdInvocation(ctx context.Context, conn *systemd.Conn, name string) (unitInvocation, string, uint32, error) {
	unit, err := supervision.ReadSystemdUnit(ctx, conn, name)
	return unitInvocation{ID: unit.InvocationID, ControlGroup: unit.ControlGroup}, unit.ActiveState, unit.MainPID, err
}
