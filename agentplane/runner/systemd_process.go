package runner

import (
	"context"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"time"

	systemd "github.com/coreos/go-systemd/v22/dbus"
	"github.com/godbus/dbus/v5"
	"github.com/google/uuid"
	"go.uber.org/zap"
)

// systemdProcess retains exit evidence through RemainAfterExit and CollectMode:
// successful exits stay active/exited and failures stay loaded until Stop/fence.
// Its private name is allocated
// before StartTransientUnit; no retry can accidentally allocate another launch.
type systemdProcess struct {
	conn         *systemd.Conn
	cancel       context.CancelFunc
	name         string
	group        string
	accepted     bool
	updates      chan *systemd.PropertiesUpdate
	updateErrors chan error
}

func (p *Process) startSystemd(ctx context.Context) error {
	policy := p.config.VMProcessPolicy
	if err := ctx.Err(); err != nil {
		return p.cleanupFailedStart(err)
	}
	if policy.scope == "" {
		return p.cleanupFailedStart(errors.New("VM process policy has not fenced its owner scope"))
	}
	if policy.lifetimeMarker == "" {
		return p.cleanupFailedStart(errors.New("VM process policy has no Runner invocation guard"))
	}
	conn, cancelConnection, err := openSystemdConnection()
	if err != nil {
		return p.cleanupFailedStart(fmt.Errorf("connect native systemd manager: %w", err))
	}
	backend := &systemdProcess{conn: conn, cancel: cancelConnection, name: policy.scope + uuid.NewString() + ".service", updates: make(chan *systemd.PropertiesUpdate, 32), updateErrors: make(chan error, 1)}
	// Direct peers are already subscribed. Use the upstream typed notification
	// API only as a hint; every observation re-reads current authoritative state.
	conn.SetPropertiesSubscriber(backend.updates, backend.updateErrors)
	stdinReader, stdinWriter, err := os.Pipe()
	if err != nil {
		conn.Close()
		cancelConnection()
		return p.cleanupFailedStart(err)
	}
	stdoutReader, stdoutWriter, err := os.Pipe()
	if err != nil {
		closeFiles(stdinReader, stdinWriter)
		conn.Close()
		cancelConnection()
		return p.cleanupFailedStart(err)
	}
	stderrReader, stderrWriter, err := os.Pipe()
	if err != nil {
		closeFiles(stdinReader, stdinWriter, stdoutReader, stdoutWriter)
		conn.Close()
		cancelConnection()
		return p.cleanupFailedStart(err)
	}
	defer closeFiles(stdinReader, stdoutWriter, stderrWriter)
	props, err := policy.properties(policy.ProcessLimits)
	if err != nil {
		closeFiles(stdinWriter, stdoutReader, stderrReader)
		conn.Close()
		cancelConnection()
		return p.cleanupFailedStart(err)
	}
	command := p.config.Command
	executable := command[0]
	if !filepath.IsAbs(executable) {
		err = errors.New("VM native executable must be absolute")
		closeFiles(stdinWriter, stdoutReader, stderrReader)
		conn.Close()
		cancelConnection()
		return p.cleanupFailedStart(err)
	}
	// ExecStartEx disables manager-side $ expansion. Native argv (including shell
	// programs) must be delivered verbatim, with expansion owned by the native process.
	props = append(props, policy.identityProperties()...)
	// AddRef requires the API bus and cannot be used on the private manager peer.
	// Lifecycle retention preserves status until we consume it before Stop.
	props = append(
		props,
		property("Description", "Agentplane native launch "+backend.name), property("Type", "exec"), property("Restart", "no"), property("Slice", policy.Slice),
		property("BindsTo", []string{policy.parentUnit()}), property("After", []string{policy.parentUnit()}), property("RemainAfterExit", true), property("CollectMode", "inactive"),
		property("Conditions", []systemdCondition{{Type: "ConditionPathExists", Parameter: policy.lifetimeMarker}}),
		property("ExecStartEx", []systemdExec{{executable, command, []string{"no-env-expand"}}}), property("Environment", environment(p.config.Env)), property("WorkingDirectory", p.config.Dir),
		property("StandardInputFileDescriptor", dbus.UnixFD(stdinReader.Fd())), property("StandardOutputFileDescriptor", dbus.UnixFD(stdoutWriter.Fd())), property("StandardErrorFileDescriptor", dbus.UnixFD(stderrWriter.Fd())),
		property("PrivateTmp", false), property("ProtectSystem", "strict"), property("ReadWritePaths", []string{"/state", "/tmp"}), property("UMask", uint32(7)), property("LockPersonality", true), property("ProtectKernelModules", true), property("ProtectKernelLogs", true),
		property("KillMode", "control-group"), property("KillSignal", int32(15)), property("SendSIGKILL", true), property("TimeoutStopUSec", uint64(stopGrace/time.Microsecond)), property("OOMPolicy", "continue"),
	)
	p.mu.Lock()
	p.systemd = backend
	p.stdin = stdinWriter
	p.stdout = stdoutReader
	p.stderr = &tailReader{reader: stderrReader}
	p.mu.Unlock()
	// Caller cancellation cannot let an accepted launch escape cleanup. A subsequent
	// request on this same connection observes the manager after this message.
	launchCtx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	completion := make(chan string, 1)
	start := policy.startTransient
	if start == nil {
		start = func(c context.Context, bus *systemd.Conn, name string, properties []systemd.Property, completed chan<- string) (int, error) {
			return bus.StartTransientUnitContext(c, name, "fail", properties, completed)
		}
	}
	job, err := start(launchCtx, conn, backend.name, props, completion)
	cancel()
	backend.accepted = err == nil
	if err != nil {
		fenceErr := p.fenceSystemdUntilCertain()
		backend.release()
		closeFiles(stdinWriter, stdoutReader, stderrReader)
		return p.cleanupFailedStart(errors.Join(fmt.Errorf("start systemd native unit %s: %w", backend.name, err), fenceErr))
	}
	if err = ctx.Err(); err != nil {
		fenceErr := p.fenceSystemdUntilCertain()
		backend.release()
		closeFiles(stdinWriter, stdoutReader, stderrReader)
		return p.cleanupFailedStart(errors.Join(err, fenceErr))
	}
	// Upstream registers the completion listener before releasing its job lock,
	// so even an immediate exec/exit cannot outrun listener registration.
	connectionCheck := time.NewTicker(time.Second)
	defer connectionCheck.Stop()
	for err == nil {
		select {
		case <-ctx.Done():
			err = fmt.Errorf("wait systemd start job %d for %s: %w (%s)", job, backend.name, ctx.Err(), backend.diagnostics())
		case <-connectionCheck.C:
			if !conn.Connected() {
				err = errors.New("systemd manager disconnected while waiting for native start job")
			}
		case result := <-completion:
			if result != "done" {
				err = fmt.Errorf("native unit start job failed: %s", result)
			} else {
				service, observeErr := conn.GetUnitTypePropertiesContext(ctx, backend.name, "Service")
				if observeErr != nil {
					err = observeErr
				} else {
					pid, _ := service["ExecMainPID"].(uint32)
					if pid == 0 {
						err = errors.New("native start job completed without executing a process; Runner invocation may have ended")
						break
					}
					backend.group, _ = service["ControlGroup"].(string)
					p.mu.Lock()
					p.nativePID = int(pid)
					p.mu.Unlock()
					go p.reapSystemd()
					return nil
				}
			}
		}
	}

	fenceErr := p.fenceSystemdUntilCertain()
	backend.release()
	closeFiles(stdinWriter, stdoutReader, stderrReader)
	return p.cleanupFailedStart(errors.Join(err, fenceErr))
}

func (b *systemdProcess) release() {
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	_ = b.conn.ResetFailedUnitContext(ctx, b.name)
	b.conn.Close()
	b.cancel()
}

// A manager failure is a fencing barrier. Never publish waitDone or release
// state ownership merely because observation/StopUnit lost its connection.
func (p *Process) fenceSystemdUntilCertain() error {
	b := p.systemd
	conn := b.conn
	cancelConnection := func() {}
	defer func() {
		if conn != b.conn {
			conn.Close()
		}
		cancelConnection()
	}()
	for {
		ctx, cancel := context.WithTimeout(context.Background(), stopGrace+5*time.Second)
		// Only the original live connection supplies FIFO ordering behind an
		// uncertain start. Replacement absence alone must never release admission.
		ordered := b.accepted || (conn == b.conn && conn.Connected())
		err := fenceSystemdUnit(ctx, conn, b.name, ordered, b.group)
		cancel()
		if err == nil {
			return nil
		}
		zap.L().Error("native systemd fencing remains blocked", zap.String("unit", b.name), zap.Error(err))
		time.Sleep(100 * time.Millisecond)
		replacement, cancelReplacement, connectErr := openSystemdConnection()
		if connectErr == nil {
			if conn != b.conn {
				conn.Close()
			}
			cancelConnection()
			conn, cancelConnection = replacement, cancelReplacement
		}
	}
}

func (p *Process) reapSystemd() {
	b := p.systemd
	code := 125
	for {
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		props, err := b.conn.GetUnitTypePropertiesContext(ctx, b.name, "Service")
		cancel()
		if err != nil {
			break
		}
		pid, _ := props["MainPID"].(uint32)
		status, _ := props["ExecMainStatus"].(int32)
		why, _ := props["ExecMainCode"].(int32)
		if pid == 0 && why != 0 {
			code = int(status)
			if why != 1 {
				code += 128
			}
			break
		}
		// External Stop (including BindsTo parent death) can collect the unit
		// before this observation. GetAll then synthesizes a not-found service
		// with zeroed exit properties. Start already observed a real ExecMainPID;
		// losing it is terminal process loss, not an indefinitely running process.
		if executed, _ := props["ExecMainPID"].(uint32); executed == 0 {
			break
		}
		if !b.waitChange() {
			break
		}
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	if unit, err := b.conn.GetUnitTypePropertiesContext(ctx, b.name, "Service"); err == nil {
		if group, ok := unit["ControlGroup"].(string); ok && group != "" {
			logCgroupMemoryDiagnostics(filepath.Join("/sys/fs/cgroup", group))
		}
	}
	cancel()
	fenceErr := p.fenceSystemdUntilCertain()
	_ = p.CloseStdin()
	b.release()
	p.mu.Lock()
	p.waitCode = code
	p.waitErr = fenceErr
	close(p.waitDone)
	p.mu.Unlock()
}

func (p *Process) stopSystemd(ctx context.Context, grace time.Duration) error {
	_ = p.CloseStdin()
	b := p.systemd
	// Signal first: StopUnit could collect an inactive unit before the reaper
	// reads ExecMainStatus. RemainAfterExit/CollectMode retain signal results.
	if err := b.conn.KillUnitWithTarget(ctx, b.name, systemd.All, int32(15)); err != nil && !systemdMissing(err) {
		if errors.Is(err, dbus.ErrClosed) {
			// Release may race Stop after the reaper has started fencing. Its
			// completed Wait, rather than the connection, is the cleanup barrier.
			_, waitErr := p.Wait(ctx)
			return waitErr
		}
		return err
	}
	timer := time.NewTimer(max(grace, 0))
	defer timer.Stop()
	select {
	case <-p.waitDone:
		_, err := p.Wait(context.Background())
		return err
	case <-timer.C:
		if err := b.conn.KillUnitWithTarget(ctx, b.name, systemd.All, int32(9)); err != nil && !systemdMissing(err) {
			if errors.Is(err, dbus.ErrClosed) {
				_, waitErr := p.Wait(ctx)
				return waitErr
			}
			return err
		}
	case <-ctx.Done():
		return ctx.Err()
	}
	_, err := p.Wait(ctx)
	return err
}

func (p *VMProcessPolicy) parentUnit() string {
	if p.lifetimeUnit != "" {
		return p.lifetimeUnit
	}
	return runnerUnit
}

// Notifications are hints: upstream may coalesce/drop overflowing updates, and
// disconnection does not close subscriber channels. The fallback bounds both.
func (b *systemdProcess) waitChange() bool {
	timer := time.NewTimer(time.Second)
	defer timer.Stop()
	for {
		select {
		case update := <-b.updates:
			if update.UnitName == b.name {
				return b.conn.Connected()
			}
		case <-b.updateErrors:
			return b.conn.Connected()
		case <-timer.C:
			return b.conn.Connected()
		}
	}
}

// diagnostics reports lifecycle evidence only, never argv or environment secrets.
func (b *systemdProcess) diagnostics() string {
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	unit, unitErr := b.conn.GetUnitPropertiesContext(ctx, b.name)
	service, serviceErr := b.conn.GetUnitTypePropertiesContext(ctx, b.name, "Service")
	return fmt.Sprintf("active=%v sub=%v unit_job=%v main_pid=%v exec_pid=%v exec_code=%v exec_status=%v result=%v unit_error=%v service_error=%v", unit["ActiveState"], unit["SubState"], unit["Job"], service["MainPID"], service["ExecMainPID"], service["ExecMainCode"], service["ExecMainStatus"], service["Result"], unitErr, serviceErr)
}

func systemdErrorKind(err error) string {
	var remote dbus.Error
	if errors.As(err, &remote) {
		return "dbus_error=" + remote.Name
	}
	return fmt.Sprintf("error_type=%T", err)
}
