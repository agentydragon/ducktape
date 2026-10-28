package runner

import (
	"context"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/godbus/dbus/v5"
	"github.com/google/uuid"
	"go.uber.org/zap"
)

// systemdProcess retains exit evidence through RemainAfterExit and CollectMode:
// successful exits stay active/exited and failures stay loaded until Stop/fence.
// Its private name is allocated
// before StartTransientUnit; no retry can accidentally allocate another launch.
type systemdProcess struct {
	conn    *dbus.Conn
	name    string
	signals chan *dbus.Signal
}

func (p *Process) startSystemd(ctx context.Context) error {
	policy := p.config.VMProcessPolicy
	if err := ctx.Err(); err != nil {
		return p.cleanupFailedStart(err)
	}
	if policy.scope == "" {
		return p.cleanupFailedStart(errors.New("VM process policy has not fenced its owner scope"))
	}
	conn, err := connectSystemd()
	if err != nil {
		return p.cleanupFailedStart(fmt.Errorf("connect native systemd manager: %w", err))
	}
	backend := &systemdProcess{conn: conn, name: policy.scope + uuid.NewString() + ".service", signals: make(chan *dbus.Signal, 32)}
	conn.Signal(backend.signals)
	if err = systemdCall(ctx, conn, "Subscribe").Err; err != nil {
		conn.Close()
		return p.cleanupFailedStart(fmt.Errorf("subscribe native systemd manager: %w (%s)", err, systemdErrorKind(err)))
	}
	stdinReader, stdinWriter, err := os.Pipe()
	if err != nil {
		conn.Close()
		return p.cleanupFailedStart(err)
	}
	stdoutReader, stdoutWriter, err := os.Pipe()
	if err != nil {
		closeFiles(stdinReader, stdinWriter)
		conn.Close()
		return p.cleanupFailedStart(err)
	}
	stderrReader, stderrWriter, err := os.Pipe()
	if err != nil {
		closeFiles(stdinReader, stdinWriter, stdoutReader, stdoutWriter)
		conn.Close()
		return p.cleanupFailedStart(err)
	}
	defer closeFiles(stdinReader, stdoutWriter, stderrWriter)
	props, err := policy.properties(policy.ProcessLimits)
	if err != nil {
		closeFiles(stdinWriter, stdoutReader, stderrReader)
		conn.Close()
		return p.cleanupFailedStart(err)
	}
	command := p.config.Command
	executable := command[0]
	if !filepath.IsAbs(executable) {
		err = errors.New("VM native executable must be absolute")
		closeFiles(stdinWriter, stdoutReader, stderrReader)
		conn.Close()
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
	var job dbus.ObjectPath
	start := policy.startTransient
	if start == nil {
		start = startTransientSystemdUnit
	}
	job, err = start(launchCtx, conn, backend.name, props)
	cancel()
	if err != nil {
		fenceErr := p.fenceSystemdUntilCertain()
		backend.release()
		closeFiles(stdinWriter, stdoutReader, stderrReader)
		return p.cleanupFailedStart(errors.Join(fmt.Errorf("start systemd native unit %s: %w (%s)", backend.name, err, systemdErrorKind(err)), fenceErr))
	}
	if err = ctx.Err(); err != nil {
		fenceErr := p.fenceSystemdUntilCertain()
		backend.release()
		closeFiles(stdinWriter, stdoutReader, stderrReader)
		return p.cleanupFailedStart(errors.Join(err, fenceErr))
	}
	signalsSeen := 0
	lastSignal := ""
	// StartTransientUnit acknowledges enqueueing. JobRemoved("done") establishes
	// that Type=exec crossed execve successfully, even for an already-exited helper.
	for err == nil {
		select {
		case <-ctx.Done():
			err = fmt.Errorf("wait systemd start job %s for %s: %w (%s; signals=%d last=%s)", job, backend.name, ctx.Err(), backend.diagnostics(), signalsSeen, lastSignal)
		case signal, ok := <-backend.signals:
			if !ok {
				err = errors.New("systemd bus closed during launch")
				break
			}
			signalsSeen++
			lastSignal = signal.Name
			if signal.Name == managerInterface+".JobRemoved" && len(signal.Body) == 4 {
				lastSignal = fmt.Sprintf("%s job=%v unit=%v result=%v", signal.Name, signal.Body[1], signal.Body[2], signal.Body[3])
			}
			if signal.Name != managerInterface+".JobRemoved" || len(signal.Body) != 4 || signal.Body[1] != job {
				continue
			}
			result, ok := signal.Body[3].(string)
			if !ok || result != "done" {
				err = fmt.Errorf("native unit start job failed: %v", signal.Body[3])
				break
			}
			service, observeErr := systemdProperties(ctx, conn, backend.name, serviceInterface)
			if observeErr != nil {
				err = fmt.Errorf("observe executed systemd unit %s: %w", backend.name, observeErr)
				break
			}
			pid, _ := service["ExecMainPID"].Value().(uint32)
			p.mu.Lock()
			p.nativePID = int(pid)
			p.mu.Unlock()
			go p.reapSystemd()
			return nil
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
	_ = systemdCall(ctx, b.conn, "ResetFailedUnit", b.name).Err
	b.conn.RemoveSignal(b.signals)
	_ = b.conn.Close()
}

// A manager failure is a fencing barrier. Never publish waitDone or release
// state ownership merely because observation/StopUnit lost its connection.
func (p *Process) fenceSystemdUntilCertain() error {
	b := p.systemd
	conn := b.conn
	defer func() {
		if conn != b.conn {
			_ = conn.Close()
		}
	}()
	for {
		ctx, cancel := context.WithTimeout(context.Background(), stopGrace+5*time.Second)
		err := fenceSystemdUnit(ctx, conn, b.name)
		cancel()
		if err == nil {
			return nil
		}
		zap.L().Error("native systemd fencing remains blocked", zap.String("unit", b.name), zap.Error(err))
		time.Sleep(100 * time.Millisecond)
		replacement, connectErr := connectSystemd()
		if connectErr == nil {
			if conn != b.conn {
				_ = conn.Close()
			}
			conn = replacement
		}
	}
}

func (p *Process) reapSystemd() {
	b := p.systemd
	code := 125
	for {
		props, err := systemdProperties(context.Background(), b.conn, b.name, serviceInterface)
		if err != nil {
			break
		}
		pid, _ := props["MainPID"].Value().(uint32)
		status, _ := props["ExecMainStatus"].Value().(int32)
		why, _ := props["ExecMainCode"].Value().(int32)
		if pid == 0 && why != 0 {
			code = int(status)
			if why != 1 {
				code += 128
			}
			break
		}
		if !b.waitChange() {
			break
		}
	}
	if unit, err := systemdProperties(context.Background(), b.conn, b.name, serviceInterface); err == nil {
		if group, ok := unit["ControlGroup"].Value().(string); ok && group != "" {
			logCgroupMemoryDiagnostics(filepath.Join("/sys/fs/cgroup", group))
		}
	}
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
	if err := systemdCall(ctx, b.conn, "KillUnit", b.name, "all", int32(15)).Err; err != nil && !systemdMissing(err) {
		return err
	}
	timer := time.NewTimer(max(grace, 0))
	defer timer.Stop()
	select {
	case <-p.waitDone:
		_, err := p.Wait(context.Background())
		return err
	case <-timer.C:
		if err := systemdCall(ctx, b.conn, "KillUnit", b.name, "all", int32(9)).Err; err != nil && !systemdMissing(err) {
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

func startTransientSystemdUnit(ctx context.Context, conn *dbus.Conn, name string, props []systemdProperty) (dbus.ObjectPath, error) {
	var job dbus.ObjectPath
	err := systemdCall(ctx, conn, "StartTransientUnit", name, "fail", props, []systemdAuxUnit{}).Store(&job)
	return job, err
}

// Private peers receive manager signals directly; bus-daemon AddMatch is neither
// necessary nor available. Filter unrelated units before issuing observations.
func (b *systemdProcess) waitChange() bool {
	path := dbus.ObjectPath("/org/freedesktop/systemd1/unit/" + strings.NewReplacer("-", "_2d", ".", "_2e").Replace(b.name))
	for signal := range b.signals {
		if signal.Path == path {
			return true
		}
		if signal.Name == managerInterface+".JobRemoved" && len(signal.Body) == 4 && signal.Body[2] == b.name {
			return true
		}
	}
	return false
}

// diagnostics reports lifecycle evidence only, never argv or environment secrets.
func (b *systemdProcess) diagnostics() string {
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	unit, unitErr := systemdProperties(ctx, b.conn, b.name, unitInterface)
	service, serviceErr := systemdProperties(ctx, b.conn, b.name, serviceInterface)
	return fmt.Sprintf("active=%v sub=%v unit_job=%v main_pid=%v exec_pid=%v exec_code=%v exec_status=%v result=%v unit_error=%v service_error=%v", unit["ActiveState"].Value(), unit["SubState"].Value(), unit["Job"].Value(), service["MainPID"].Value(), service["ExecMainPID"].Value(), service["ExecMainCode"].Value(), service["ExecMainStatus"].Value(), service["Result"].Value(), unitErr, serviceErr)
}

func systemdErrorKind(err error) string {
	var remote dbus.Error
	if errors.As(err, &remote) {
		return "dbus_error=" + remote.Name
	}
	return fmt.Sprintf("error_type=%T", err)
}
