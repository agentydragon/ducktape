package runtime

import (
	"context"
	"errors"
	"fmt"

	"github.com/agentydragon/ducktape/agentplane/env/runtime/supervision"
	systemd "github.com/coreos/go-systemd/v22/dbus"
)

type unitInvocation struct {
	ID             string
	ControlGroup   string
	RequireSuccess bool
	reference      *supervision.SystemdUnitReference
}

func startSystemdUnit(ctx context.Context, name string) (unitInvocation, error) {
	reference, err := supervision.OpenSystemdUnitReference(ctx, name)
	if err != nil {
		return unitInvocation{}, fmt.Errorf("connect systemd to start %s: %w", name, err)
	}
	conn := reference.Conn()
	owner := unitInvocation{reference: reference}
	retainIfUnfenced := func(cause error, expected unitInvocation) (unitInvocation, error) {
		expected.reference = reference
		fenceCtx, cancel := context.WithTimeout(context.Background(), executionFenceGrace)
		fenceErr := fenceSystemdUnitOnConnection(fenceCtx, conn, name, expected)
		cancel()
		if fenceErr != nil {
			// Preserve the RefUnit peer when a launch may have been accepted but
			// cleanup remains unknown. Runtime shutdown can retry this exact fence.
			return expected, errors.Join(cause, fenceErr)
		}
		return unitInvocation{}, errors.Join(cause, reference.Close())
	}
	// A unit left by a previous Runtime invocation must be fenced before the new
	// invocation can be accepted. The reference was acquired first and the same
	// manager connection orders this fence, the StartUnit request, and observation.
	if err := fenceSystemdUnitOnConnection(ctx, conn, name, unitInvocation{}); err != nil {
		owner.reference = reference
		return owner, fmt.Errorf("fence previous %s invocation: %w", name, err)
	}
	completed := make(chan string, 1)
	if _, err := conn.StartUnitContext(ctx, name, "replace", completed); err != nil {
		return retainIfUnfenced(fmt.Errorf("start systemd unit %s: %w", name, err), unitInvocation{})
	}
	if err := supervision.WaitSystemdJob(ctx, name, "start", completed); err != nil {
		return retainIfUnfenced(err, unitInvocation{})
	}
	invocation, state, pid, err := readSystemdInvocation(ctx, conn, name)
	if err != nil {
		return retainIfUnfenced(err, invocation)
	}
	if state != "active" || pid == 0 || invocation.ID == "" || invocation.ControlGroup == "" {
		return retainIfUnfenced(fmt.Errorf("systemd unit %s did not enter a verifiable live invocation (state=%s, main_pid=%d, invocation=%q, cgroup=%q)", name, state, pid, invocation.ID, invocation.ControlGroup), invocation)
	}
	invocation.reference = reference
	invocation.RequireSuccess = true
	return invocation, nil
}

func fenceSystemdUnit(ctx context.Context, name string, expected unitInvocation) error {
	if expected.reference == nil {
		return fmt.Errorf("fence systemd unit %s without its retained manager reference", name)
	}
	if err := fenceSystemdUnitOnConnection(ctx, expected.reference.Conn(), name, expected); err != nil {
		return err
	}
	return expected.reference.Close()
}

func fenceSystemdUnitOnConnection(ctx context.Context, conn *systemd.Conn, name string, expected unitInvocation) error {
	return supervision.FenceSystemdUnit(ctx, conn, name, supervision.SystemdFence{InvocationID: expected.ID, ControlGroup: expected.ControlGroup, RequireSuccess: expected.RequireSuccess, Ordered: true})
}

func readSystemdInvocation(ctx context.Context, conn *systemd.Conn, name string) (unitInvocation, string, uint32, error) {
	unit, err := supervision.ReadSystemdUnit(ctx, conn, name)
	return unitInvocation{ID: unit.InvocationID, ControlGroup: unit.ControlGroup}, unit.ActiveState, unit.MainPID, err
}
