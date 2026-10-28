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

// systemdProcess is a retained transient service. Its private name is allocated
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
		return p.cleanupFailedStart(err)
	}
	backend := &systemdProcess{conn: conn, name: policy.scope + uuid.NewString() + ".service", signals: make(chan *dbus.Signal, 32)}
	conn.Signal(backend.signals)
	if err = systemdCall(ctx, conn, "Subscribe").Err; err != nil {
		conn.Close()
		return p.cleanupFailedStart(err)
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
	props = append(props, policy.identityProperties()...)
	props = append(
		props,
		property("Description", "Agentplane native launch "+backend.name), property("Type", "exec"), property("Restart", "no"), property("Slice", policy.Slice),
		property("BindsTo", []string{policy.parentUnit()}), property("After", []string{policy.parentUnit()}), property("AddRef", true), property("RemainAfterExit", true),
		property("ExecStart", []systemdExec{{executable, command, false}}), property("Environment", environment(p.config.Env)), property("WorkingDirectory", p.config.Dir),
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
		return p.cleanupFailedStart(errors.Join(fmt.Errorf("start systemd native unit %s: %w", backend.name, err), fenceErr))
	}
	if err = ctx.Err(); err != nil {
		fenceErr := p.fenceSystemdUntilCertain()
		backend.release()
		closeFiles(stdinWriter, stdoutReader, stderrReader)
		return p.cleanupFailedStart(errors.Join(err, fenceErr))
	}
	// StartTransientUnit acknowledges enqueueing. JobRemoved("done") establishes
	// that Type=exec crossed execve successfully, even for an already-exited helper.
	for err == nil {
		select {
		case <-ctx.Done():
			err = ctx.Err()
		case signal, ok := <-backend.signals:
			if !ok {
				err = errors.New("systemd bus closed during launch")
				break
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
				err = observeErr
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
	_ = systemdCall(ctx, b.conn, "UnrefUnit", b.name).Err
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
	if err := systemdCall(ctx, b.conn, "StopUnit", b.name, "replace").Err; err != nil && !systemdMissing(err) {
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
