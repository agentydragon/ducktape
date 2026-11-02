package supervision

import (
	"context"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sync"
	"time"

	systemd "github.com/coreos/go-systemd/v22/dbus"
	"github.com/godbus/dbus/v5"
	"github.com/google/uuid"
	"go.uber.org/zap"
)

// systemdBackend retains exit evidence through RemainAfterExit and an API-bus
// AddRef until the reaper reads the result and fences the unit's cgroup. Its
// unit name is allocated before StartTransientUnit so uncertain launch retries
// always reconcile the same UUID.
type systemdBackend struct {
	config   ProcessConfig
	launcher *SystemdLauncher

	conn         *systemd.Conn
	cancel       context.CancelFunc
	name         string
	group        string
	invocationID string
	accepted     bool
	updates      chan *systemd.PropertiesUpdate
	updateErrors chan error

	stopMu         sync.Mutex
	stopRequests   map[*systemd.Conn]*nativeSystemdStopJob
	stopUnit       func(context.Context, *systemd.Conn, string) (<-chan string, error)
	referenceMu    sync.Mutex
	unitReferences []systemdUnitReference
	referenceUnit  func(context.Context, string) (systemdUnitReference, error)
}

type nativeSystemdStopJob struct {
	done     chan struct{}
	cancel   context.CancelFunc
	result   string
	observed bool
}

// go-systemd delivers one result to one channel. Cache that result so retries
// of the systemd fence each receive an independent, replayable observation.
func newNativeSystemdStopJob(completed <-chan string) *nativeSystemdStopJob {
	ctx, cancel := context.WithCancel(context.Background())
	job := &nativeSystemdStopJob{done: make(chan struct{}), cancel: cancel}
	go func() {
		defer close(job.done)
		if completed == nil {
			return
		}
		select {
		case result, ok := <-completed:
			if ok {
				job.result = result
				job.observed = true
			}
		case <-ctx.Done():
		}
	}()
	return job
}

func (j *nativeSystemdStopJob) completion() <-chan string {
	completed := make(chan string, 1)
	go func() {
		<-j.done
		if j.observed {
			completed <- j.result
		} else {
			completed <- "completion not observed"
		}
		close(completed)
	}()
	return completed
}

func (b *systemdBackend) validate() error {
	if !filepath.IsAbs(b.config.Command[0]) {
		return errors.New("VM native executable must be absolute")
	}
	return nil
}

func (b *systemdBackend) start(ctx context.Context, p *Process) error {
	if err := ctx.Err(); err != nil {
		return err
	}
	if b.launcher.scope == "" {
		return errors.New("VM process launcher has not fenced its owner scope")
	}
	if b.launcher.lifetimeMarker == "" {
		return errors.New("VM process launcher has no Runner invocation guard")
	}
	conn, cancelConnection, err := openSystemdConnection()
	if err != nil {
		return fmt.Errorf("connect native systemd manager: %w", err)
	}
	b.conn = conn
	b.cancel = cancelConnection
	b.name = b.launcher.scope + uuid.NewString() + ".service"
	p.observeSystemdStart(b.name, SystemdPreparing, nil)
	b.updates = make(chan *systemd.PropertiesUpdate, 32)
	b.updateErrors = make(chan error, 1)
	// Use the upstream typed notification API only as a hint; every observation
	// re-reads current authoritative state.
	conn.SetPropertiesSubscriber(b.updates, b.updateErrors)
	stdinReader, stdinWriter, err := os.Pipe()
	if err != nil {
		conn.Close()
		cancelConnection()
		return err
	}
	stdoutReader, stdoutWriter, err := os.Pipe()
	if err != nil {
		closeFiles(stdinReader, stdinWriter)
		conn.Close()
		cancelConnection()
		return err
	}
	stderrReader, stderrWriter, err := os.Pipe()
	if err != nil {
		closeFiles(stdinReader, stdinWriter, stdoutReader, stdoutWriter)
		conn.Close()
		cancelConnection()
		return err
	}
	defer closeFiles(stdinReader, stdoutWriter, stderrWriter)
	props, err := b.launcher.properties(b.launcher.ProcessLimits)
	if err != nil {
		closeFiles(stdinWriter, stdoutReader, stderrReader)
		conn.Close()
		cancelConnection()
		return err
	}
	command := b.config.Command
	executable := command[0]
	if !filepath.IsAbs(executable) {
		err = errors.New("VM native executable must be absolute")
		closeFiles(stdinWriter, stdoutReader, stderrReader)
		conn.Close()
		cancelConnection()
		return err
	}
	// ExecStartEx disables manager-side $ expansion. Native argv (including shell
	// programs) must be delivered verbatim, with expansion owned by the native process.
	props = append(props, b.launcher.identityProperties()...)
	// StartTransientUnit's AddRef pins status to this API-bus sender. Systemd
	// drops the reference when this connection closes after the reaper has read
	// the invocation's exit evidence and fenced its complete cgroup.
	props = append(
		props,
		// Runtime-owned processes must remain available until their parent has
		// completed its lifecycle fence during guest shutdown.
		property("DefaultDependencies", false),
		property("Description", "Agentplane native launch "+b.name), property("Type", "exec"), property("Restart", "no"), property("Slice", b.launcher.Slice),
		property("BindsTo", []string{b.launcher.parentUnit()}), property("After", []string{b.launcher.parentUnit()}), property("RemainAfterExit", true), property("CollectMode", "inactive"),
		property("Conditions", []systemdCondition{{Type: "ConditionPathExists", Parameter: b.launcher.lifetimeMarker}}), property("AddRef", true),
		property("ExecStartEx", []systemdExec{{executable, command, []string{"no-env-expand"}}}), property("Environment", environment(b.config.Env)), property("WorkingDirectory", b.config.Dir),
		property("StandardInputFileDescriptor", dbus.UnixFD(stdinReader.Fd())), property("StandardOutputFileDescriptor", dbus.UnixFD(stdoutWriter.Fd())), property("StandardErrorFileDescriptor", dbus.UnixFD(stderrWriter.Fd())),
		property("PrivateTmp", false), property("ProtectSystem", "strict"), property("ReadWritePaths", []string{"/state", "/tmp"}), property("UMask", uint32(7)), property("LockPersonality", true), property("ProtectKernelModules", true), property("ProtectKernelLogs", true),
		property("KillMode", "control-group"), property("KillSignal", int32(15)), property("SendSIGKILL", true), property("TimeoutStopUSec", systemdStopTimeout(b.config.StopGrace)), property("OOMPolicy", "continue"),
	)
	p.attachStreams(stdinWriter, stdoutReader, stderrReader)
	// Caller cancellation cannot let an accepted launch escape cleanup. A subsequent
	// request on this same connection observes the manager after this message.
	launchCtx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	completion := make(chan string, 1)
	start := b.launcher.startTransient
	if start == nil {
		start = func(c context.Context, bus *systemd.Conn, name string, properties []systemd.Property, completed chan<- string) (int, error) {
			return bus.StartTransientUnitContext(c, name, "fail", properties, completed)
		}
	}
	p.observeSystemdStart(b.name, SystemdStartSubmitted, nil)
	job, err := start(launchCtx, conn, b.name, props, completion)
	cancel()
	b.accepted = err == nil
	if err != nil {
		p.observeSystemdStart(b.name, SystemdStartUnconfirmed, err)
		fenceErr := b.fenceUntilCertain(p)
		b.release()
		closeFiles(stdinWriter, stdoutReader, stderrReader)
		return errors.Join(fmt.Errorf("start systemd native unit %s: %w", b.name, err), fenceErr)
	}
	p.observeSystemdStart(b.name, SystemdStartAccepted, nil)
	if err = ctx.Err(); err != nil {
		fenceErr := b.fenceUntilCertain(p)
		b.release()
		closeFiles(stdinWriter, stdoutReader, stderrReader)
		return errors.Join(err, fenceErr)
	}
	// Upstream registers the completion listener before releasing its job lock,
	// so even an immediate exec/exit cannot outrun listener registration.
	connectionCheck := time.NewTicker(time.Second)
	defer connectionCheck.Stop()
	for err == nil {
		select {
		case <-ctx.Done():
			err = fmt.Errorf("wait systemd start job %d for %s: %w (%s)", job, b.name, ctx.Err(), b.diagnostics())
		case <-connectionCheck.C:
			if !conn.Connected() {
				err = errors.New("systemd manager disconnected while waiting for native start job")
			}
		case result := <-completion:
			if result != "done" {
				err = fmt.Errorf("native unit start job failed: %s", result)
			} else {
				service, observeErr := ReadSystemdUnit(ctx, conn, b.name)
				if observeErr != nil {
					p.observeSystemd(b.name, "", "", service, observeErr)
					err = observeErr
				} else {
					pid := service.ExecMainPID
					if pid == 0 {
						err = errors.New("native start job completed without executing a process; Runner invocation may have ended")
						break
					}
					if service.InvocationID == "" {
						err = errors.New("native start job completed without an invocation ID")
						break
					}
					b.group = service.ControlGroup
					b.invocationID = service.InvocationID
					p.observeSystemd(b.name, b.invocationID, b.group, service, nil)
					p.setNativePID(int(pid))
					go b.reap(p)
					return nil
				}
			}
		}
	}

	fenceErr := b.fenceUntilCertain(p)
	b.release()
	closeFiles(stdinWriter, stdoutReader, stderrReader)
	return errors.Join(err, fenceErr)
}

func (b *systemdBackend) release() {
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	_ = b.conn.ResetFailedUnitContext(ctx, b.name)
	b.conn.Close()
	b.cancel()
	b.stopMu.Lock()
	for _, job := range b.stopRequests {
		job.cancel()
	}
	b.stopMu.Unlock()
	b.referenceMu.Lock()
	for _, reference := range b.unitReferences {
		_ = reference.Close()
	}
	b.unitReferences = nil
	b.referenceMu.Unlock()
}

// A manager failure is a fencing barrier. Never publish waitDone or release
// state ownership merely because observation/StopUnit lost its connection.
func (b *systemdBackend) fenceUntilCertain(p *Process) error {
	conn := b.conn
	cancelConnection := func() {}
	defer func() {
		if conn != b.conn {
			conn.Close()
		}
		cancelConnection()
	}()
	for {
		ctx, cancel := context.WithTimeout(context.Background(), b.config.StopGrace+5*time.Second)
		// Only the original live connection supplies FIFO ordering behind an
		// uncertain start. Replacement absence alone must never release admission.
		ordered := b.accepted || (conn == b.conn && conn.Connected())
		err := fenceSystemdUnit(ctx, nativeSystemdUnitManager{backend: b, conn: conn, process: p}, b.name, SystemdFence{
			Ordered: ordered, InvocationID: b.invocationID, ControlGroup: b.group,
		}, systemdGroupEmpty)
		cancel()
		if err == nil {
			return nil
		}
		zap.L().Error("native systemd fencing remains blocked", zap.String("unit", b.name), zap.Error(err))
		time.Sleep(100 * time.Millisecond)
		replacement, cancelReplacement, connectErr := openSystemdConnection()
		if connectErr == nil {
			b.retainUnitReference(replacement)
			if conn != b.conn {
				conn.Close()
			}
			cancelConnection()
			conn, cancelConnection = replacement, cancelReplacement
		}
	}
}

// nativeSystemdUnitManager lets the shared fence reuse the stop job requested
// by Process.Stop instead of enqueueing a competing replacement job.
type nativeSystemdUnitManager struct {
	backend *systemdBackend
	conn    *systemd.Conn
	process *Process
}

func (m nativeSystemdUnitManager) snapshot(ctx context.Context, name string) (SystemdUnitSnapshot, error) {
	snapshot, err := ReadSystemdUnit(ctx, m.conn, name)
	m.process.observeSystemd(name, m.backend.invocationID, m.backend.group, snapshot, err)
	return snapshot, err
}

func (m nativeSystemdUnitManager) stop(ctx context.Context, _ string) (<-chan string, error) {
	return m.backend.requestStop(ctx, m.conn)
}

// retainUnitReference acquires a reference on a replacement API-bus peer before
// asking systemd to stop the unit. If the reference cannot be acquired, cleanup
// still proceeds; the reaper reports missing exit evidence instead of inventing
// a status if systemd collected the result.
func (b *systemdBackend) retainUnitReference(manager *systemd.Conn) error {
	// StartTransientUnit's AddRef belongs to the original API-bus sender. It is
	// sufficient only while that manager connection remains alive.
	if manager != nil && manager == b.conn && manager.Connected() {
		return nil
	}
	b.referenceMu.Lock()
	live := false
	kept := b.unitReferences[:0]
	for _, reference := range b.unitReferences {
		if reference.Connected() {
			live = true
			kept = append(kept, reference)
		} else {
			_ = reference.Close()
		}
	}
	b.unitReferences = kept
	b.referenceMu.Unlock()
	if live {
		return nil
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	openReference := b.referenceUnit
	if openReference == nil {
		openReference = referenceSystemdUnit
	}
	reference, err := openReference(ctx, b.name)
	if err != nil {
		zap.L().Warn("could not retain native systemd unit after reconnect", zap.String("unit", b.name), zap.Error(err))
		return err
	}
	b.referenceMu.Lock()
	live = false
	for _, existing := range b.unitReferences {
		if existing.Connected() {
			live = true
			break
		}
	}
	if live {
		_ = reference.Close()
	} else {
		b.unitReferences = append(b.unitReferences, reference)
	}
	b.referenceMu.Unlock()
	return nil
}

func (b *systemdBackend) requestStop(ctx context.Context, conn *systemd.Conn) (<-chan string, error) {
	b.stopMu.Lock()
	defer b.stopMu.Unlock()
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	if b.stopRequests == nil {
		b.stopRequests = make(map[*systemd.Conn]*nativeSystemdStopJob)
	}
	if job, ok := b.stopRequests[conn]; ok {
		return job.completion(), nil
	}
	stopUnit := b.stopUnit
	if stopUnit == nil {
		stopUnit = StopSystemdUnit
	}
	completed, err := stopUnit(ctx, conn, b.name)
	if err != nil {
		return nil, err
	}
	job := newNativeSystemdStopJob(completed)
	b.stopRequests[conn] = job
	return job.completion(), nil
}

func systemdStopTimeout(grace time.Duration) uint64 {
	if grace <= 0 {
		// systemd interprets a zero stop timeout as disabled; the smallest
		// representable positive timeout preserves the immediate-escalation case.
		return 1
	}
	usec := uint64(grace / time.Microsecond)
	if grace%time.Microsecond != 0 {
		usec++
	}
	return usec
}

func (b *systemdBackend) reap(p *Process) {
	code := -1
	statusObserved := false
	var observationErr error
	readConn := b.conn
	var readConnCancel context.CancelFunc
	defer func() {
		if readConn != b.conn {
			readConn.Close()
			if readConnCancel != nil {
				readConnCancel()
			}
		}
	}()
	for {
		// A reconnect loses the original sender's AddRef. Keep checking the
		// replacement pin's liveness before trusting status from that manager.
		if readConn != b.conn {
			_ = b.retainUnitReference(readConn)
		}
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		props, err := b.readSnapshotOn(ctx, readConn)
		cancel()
		p.observeSystemd(b.name, b.invocationID, b.group, props, err)
		if err != nil {
			if errors.Is(err, context.DeadlineExceeded) && readConn.Connected() {
				zap.L().Warn("native systemd process observation timed out; retrying", zap.String("unit", b.name), zap.Error(err))
				if !b.waitChangeOn(readConn) {
					break
				}
				continue
			}
			if !readConn.Connected() {
				replacement, cancelReplacement, connectErr := openSystemdConnection()
				if connectErr != nil {
					observationErr = errors.Join(err, fmt.Errorf("reconnect for native exit evidence: %w", connectErr))
					break
				}
				replacement.SetPropertiesSubscriber(b.updates, b.updateErrors)
				b.retainUnitReference(replacement)
				if readConn != b.conn {
					readConn.Close()
					if readConnCancel != nil {
						readConnCancel()
					}
				}
				readConn, readConnCancel = replacement, cancelReplacement
				continue
			}
			observationErr = fmt.Errorf("read native systemd exit evidence: %w", err)
			break
		}
		code, statusObserved, observationErr = nativeExitStatus(props, b.invocationID)
		if statusObserved || observationErr != nil {
			break
		}
		// A retained invocation keeps ExecMainPID and ExecMainCode available.
		// If either disappears without a status, containment may still be proven
		// below, but its exit code remains explicitly unknown.
		if props.ExecMainPID == 0 {
			observationErr = errors.New("native systemd invocation lost its process exit status")
			break
		}
		if !b.waitChangeOn(readConn) && readConn.Connected() {
			break
		}
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	if unit, err := ReadSystemdUnit(ctx, b.conn, b.name); err == nil && unit.ControlGroup != "" {
		logCgroupMemoryDiagnostics(filepath.Join("/sys/fs/cgroup", unit.ControlGroup))
	}

	cancel()
	if !statusObserved {
		observationErr = errors.Join(observationErr, errors.New("native systemd exit status was not observed"))
	}
	// Exit evidence is useful while the domain fence is still pending. It must
	// never be interpreted as proof that descendants have been removed.
	p.observeSystemdExit(code, statusObserved, observationErr)
	fenceErr := b.fenceUntilCertain(p)
	_ = p.CloseStdin()
	b.release()
	p.completeWith(code, errors.Join(observationErr, fenceErr))
}

func (b *systemdBackend) readSnapshotOn(ctx context.Context, conn *systemd.Conn) (SystemdUnitSnapshot, error) {
	if read := b.launcher.reapSnapshot; read != nil && conn == b.conn {
		return read(ctx, b.conn, b.name)
	}
	return ReadSystemdUnit(ctx, conn, b.name)
}

func nativeExitStatus(unit SystemdUnitSnapshot, invocationID string) (int, bool, error) {
	if unit.LoadState == "not-found" {
		return -1, false, errors.New("native systemd invocation was collected before its exit status was observed")
	}
	if invocationID == "" || unit.InvocationID != invocationID {
		return -1, false, fmt.Errorf("native systemd invocation identity mismatch: expected %q, observed %q", invocationID, unit.InvocationID)
	}
	if unit.ExecMainCode == 0 {
		return -1, false, nil
	}
	code := int(unit.ExecMainStatus)
	if unit.ExecMainCode != 1 {
		code += 128
	}
	return code, true, nil
}

func (b *systemdBackend) stop(ctx context.Context, p *Process) error {
	_ = p.CloseStdin()
	_, err := b.requestStop(ctx, b.conn)
	if err != nil {
		if errors.Is(err, dbus.ErrClosed) {
			// Reaper reconnection and its process-domain fence are the completion
			// barrier when this caller loses its manager connection.
			_, waitErr := p.Wait(ctx)
			return waitErr
		}
		return err
	}
	_, err = p.Wait(ctx)
	return err
}

// Notifications are hints: upstream may coalesce/drop overflowing updates, and
// disconnection does not close subscriber channels. The fallback bounds both.
func (b *systemdBackend) waitChangeOn(conn *systemd.Conn) bool {
	timer := time.NewTimer(time.Second)
	defer timer.Stop()
	for {
		select {
		case update := <-b.updates:
			if update.UnitName == b.name {
				return conn.Connected()
			}
		case <-b.updateErrors:
			return conn.Connected()
		case <-timer.C:
			return conn.Connected()
		}
	}
}

// diagnostics reports lifecycle evidence only, never argv or environment secrets.
func (b *systemdBackend) diagnostics() string {
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	unit, err := ReadSystemdUnit(ctx, b.conn, b.name)
	return fmt.Sprintf("active=%s sub=%s unit_job=%d main_pid=%d exec_pid=%d exec_code=%d exec_status=%d result=%s error=%v", unit.ActiveState, unit.SubState, unit.JobID, unit.MainPID, unit.ExecMainPID, unit.ExecMainCode, unit.ExecMainStatus, unit.Result, err)
}
