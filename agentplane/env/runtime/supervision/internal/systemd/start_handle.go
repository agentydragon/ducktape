package systemd

import (
	"context"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"sync"
	"syscall"
	"time"

	goSystemd "github.com/coreos/go-systemd/v22/dbus"
	"go.uber.org/zap"
)

type nativeStopJob struct {
	done     chan struct{}
	cancel   context.CancelFunc
	result   string
	observed bool
}

// StartHandle owns one systemd request, its manager references, streams, unit
// observations, stop fence, and replayable process exit.
type StartHandle struct {
	backend      *Backend
	conn         *goSystemd.Conn
	cancel       context.CancelFunc
	name         string
	group        string
	invocationID string

	stdin  *os.File
	stdout *os.File
	stderr *os.File

	updates      chan *goSystemd.PropertiesUpdate
	updateErrors chan error

	stateMu     sync.Mutex
	accepted    bool
	pid         int
	startCancel context.CancelFunc

	requestDone chan struct{}
	requestOnce sync.Once
	startDone   chan struct{}
	startOnce   sync.Once
	startErr    error

	exitDone      chan struct{}
	exitMu        sync.Mutex
	exitCode      int
	exitOOMKilled bool
	exitErr       error
	exitOnce      sync.Once

	cleanupRequest   chan struct{}
	cleanupRequested sync.Once
	cleanupDone      chan struct{}
	cleanupMu        sync.Mutex
	cleanupErr       error
	cleanupOnce      sync.Once

	closeStdinOnce sync.Once
	closeStdinErr  error

	observationMu sync.Mutex
	observation   Observation
	observer      func(Observation)

	stopMu         sync.Mutex
	stopRequests   map[*goSystemd.Conn]*nativeStopJob
	referenceMu    sync.Mutex
	unitReferences []UnitReference
}

func newStartHandle(backend *Backend, conn *goSystemd.Conn, cancel context.CancelFunc, name string, stdin, stdout, stderr *os.File) *StartHandle {
	return &StartHandle{
		backend:        backend,
		conn:           conn,
		cancel:         cancel,
		name:           name,
		stdin:          stdin,
		stdout:         stdout,
		stderr:         stderr,
		requestDone:    make(chan struct{}),
		startDone:      make(chan struct{}),
		exitDone:       make(chan struct{}),
		cleanupRequest: make(chan struct{}),
		cleanupDone:    make(chan struct{}),
	}
}

func (h *StartHandle) UnitName() string      { return h.name }
func (h *StartHandle) Done() <-chan struct{} { return h.cleanupDone }

func (h *StartHandle) PID() int {
	h.stateMu.Lock()
	defer h.stateMu.Unlock()
	return h.pid
}

func (h *StartHandle) Stdin() *os.File   { return h.stdin }
func (h *StartHandle) Stdout() io.Reader { return h.stdout }
func (h *StartHandle) Stderr() io.Reader { return h.stderr }

// Observe subscribes to manager evidence and replays the latest observation.
func (h *StartHandle) Observe(sink func(Observation)) {
	h.observationMu.Lock()
	h.observer = sink
	observation := h.observation
	h.observationMu.Unlock()
	if sink != nil && observation.Revision != 0 {
		sink(observation)
	}
}

func (h *StartHandle) updateObservation(update func(*Observation)) {
	h.observationMu.Lock()
	previous := h.observation
	observation := previous
	update(&observation)
	if observation == previous {
		h.observationMu.Unlock()
		return
	}
	observation.Revision++
	h.observation = observation
	sink := h.observer
	h.observationMu.Unlock()
	if sink != nil {
		sink(observation)
	}
}

func (h *StartHandle) observeStart(state StartState, err error) {
	h.updateObservation(func(observation *Observation) {
		observation.Unit = h.name
		observation.StartState = state
		observation.StartError = ""
		if err != nil {
			observation.StartError = err.Error()
		}
	})
}

func (h *StartHandle) observeUnit(snapshot UnitSnapshot, err error) {
	h.stateMu.Lock()
	invocationID, group := h.invocationID, h.group
	h.stateMu.Unlock()
	h.updateObservation(func(observation *Observation) {
		observation.Unit = h.name
		observation.ExpectedInvocationID = invocationID
		observation.CapturedControlGroup = group
		if err != nil {
			observation.Error = err.Error()
		} else {
			observation.Snapshot = snapshot
			observation.Error = ""
		}
	})
}

func (h *StartHandle) observeExit(code int, observed bool, err error) {
	h.updateObservation(func(observation *Observation) {
		observation.ExitState = ExitUnknown
		if observed {
			observation.ExitState = ExitObserved
		}
		observation.ExitCode = code
		observation.ExitError = ""
		if err != nil {
			observation.ExitError = err.Error()
		}
	})
}

// WaitStarted observes the start job and process identity. Cancellation stops
// this observation only; the handle remains owned and must still be stopped.
func (h *StartHandle) WaitStarted(ctx context.Context) error {
	if ctx == nil {
		ctx = context.Background()
	}
	select {
	case <-h.startDone:
		h.stateMu.Lock()
		defer h.stateMu.Unlock()
		return h.startErr
	case <-ctx.Done():
		return ctx.Err()
	}
}

func (h *StartHandle) finishStart(err error) {
	h.stateMu.Lock()
	h.startErr = err
	h.stateMu.Unlock()
	h.startOnce.Do(func() { close(h.startDone) })
}

func (h *StartHandle) finishRequest() { h.requestOnce.Do(func() { close(h.requestDone) }) }

func (h *StartHandle) setStartCancel(cancel context.CancelFunc) {
	h.stateMu.Lock()
	h.startCancel = cancel
	h.stateMu.Unlock()
}

func (h *StartHandle) cancelStart() {
	h.stateMu.Lock()
	cancel := h.startCancel
	h.stateMu.Unlock()
	if cancel != nil {
		cancel()
	}
}

func (h *StartHandle) runStart(ctx context.Context, properties []goSystemd.Property, child [3]*os.File) {
	h.observeStart(StartSubmitted, nil)
	launchCtx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	h.setStartCancel(cancel)
	completion := make(chan string, 1)
	start := h.backend.config.StartTransient
	if start == nil {
		start = func(c context.Context, conn *goSystemd.Conn, name string, props []goSystemd.Property, completed chan<- string) (int, error) {
			return conn.StartTransientUnitContext(c, name, "fail", props, completed)
		}
	}
	job, err := start(launchCtx, h.conn, h.name, properties, completion)
	if err == nil {
		closeFiles(child[:]...)
	} else {
		defer closeFiles(child[:]...)
	}
	cancel()
	h.setStartCancel(nil)
	h.stateMu.Lock()
	h.accepted = err == nil
	h.stateMu.Unlock()
	if err != nil {
		h.observeStart(StartUnconfirmed, err)
		h.finishRequest()
		startErr := fmt.Errorf("start systemd native unit %s: %w", h.name, err)
		h.finishStart(startErr)
		fenceErr := h.fenceUntilCertain()
		h.closeOwnedStreams()
		h.release()
		h.completeWith(125, errors.Join(startErr, fenceErr))
		return
	}
	h.observeStart(StartAccepted, nil)
	h.finishRequest()

	var startErr error
	connectionCheck := time.NewTicker(time.Second)
	defer connectionCheck.Stop()
	for startErr == nil {
		select {
		case <-ctx.Done():
			startErr = fmt.Errorf("wait systemd start job %d for %s: %w (%s)", job, h.name, ctx.Err(), h.diagnostics())
		case <-connectionCheck.C:
			if !h.conn.Connected() {
				startErr = errors.New("systemd manager disconnected while waiting for native start job")
			}
		case result, ok := <-completion:
			if !ok || result != "done" {
				startErr = fmt.Errorf("native unit start job failed: %s (%s)", result, h.diagnostics())
				break
			}
			// ReapSnapshot is an injection point for tests of transient reaper
			// failures. Startup must always read the manager directly so that a
			// reaper-only failure cannot turn a successful start into a failed one.
			service, observeErr := Read(ctx, h.conn, h.name)
			if observeErr != nil {
				h.observeUnit(service, observeErr)
				startErr = observeErr
				break
			}
			if service.ExecMainPID == 0 {
				startErr = errors.New("native start job completed without executing a process; Environment Runtime invocation may have ended")
				break
			}
			if service.InvocationID == "" {
				startErr = errors.New("native start job completed without an invocation ID")
				break
			}
			h.stateMu.Lock()
			h.pid = int(service.ExecMainPID)
			h.group = service.ControlGroup
			h.invocationID = service.InvocationID
			h.stateMu.Unlock()
			h.observeUnit(service, nil)
			h.finishStart(nil)
			h.reap()
			return
		}
	}
	h.finishStart(startErr)
	fenceErr := h.fenceUntilCertain()
	h.closeOwnedStreams()
	h.release()
	h.completeWith(125, errors.Join(startErr, fenceErr))
}

func (h *StartHandle) closeOwnedStreams() {
	_ = h.CloseStdin()
	closeFiles(h.stdout, h.stderr)
}

func (h *StartHandle) CloseStdin() error {
	if h.stdin == nil {
		return nil
	}
	h.closeStdinOnce.Do(func() { h.closeStdinErr = h.stdin.Close() })
	return h.closeStdinErr
}

// OOMKilled reports retained manager evidence for the leader exit.
func (h *StartHandle) OOMKilled() bool {
	h.exitMu.Lock()
	defer h.exitMu.Unlock()
	return h.exitOOMKilled
}

// Wait returns exit evidence as soon as it is observed. The cgroup fence and
// stream/connection teardown continue independently; Stop waits for that cleanup.
func (h *StartHandle) Wait(ctx context.Context) (int, error) {
	if ctx == nil {
		ctx = context.Background()
	}
	select {
	case <-h.exitDone:
		return h.exitResult()
	default:
	}
	select {
	case <-h.exitDone:
		return h.exitResult()
	case <-ctx.Done():
		return 0, ctx.Err()
	}
}

func (h *StartHandle) exitResult() (int, error) {
	h.exitMu.Lock()
	defer h.exitMu.Unlock()
	return h.exitCode, h.exitErr
}

func (h *StartHandle) recordExit(code int, err error, oomKilled bool) {
	h.exitOnce.Do(func() {
		h.exitMu.Lock()
		h.exitCode = code
		h.exitOOMKilled = oomKilled
		h.exitErr = err
		close(h.exitDone)
		h.exitMu.Unlock()
	})
}

func (h *StartHandle) waitFenced(ctx context.Context) error {
	if ctx == nil {
		ctx = context.Background()
	}
	select {
	case <-h.cleanupDone:
		h.cleanupMu.Lock()
		defer h.cleanupMu.Unlock()
		return h.cleanupErr
	default:
	}
	select {
	case <-h.cleanupDone:
		h.cleanupMu.Lock()
		defer h.cleanupMu.Unlock()
		return h.cleanupErr
	case <-ctx.Done():
		return ctx.Err()
	}
}

func (h *StartHandle) completeWith(code int, err error) {
	h.cleanupOnce.Do(func() {
		h.recordExit(code, err, false)
		h.cleanupMu.Lock()
		h.cleanupErr = err
		close(h.cleanupDone)
		h.cleanupMu.Unlock()
	})
}

// Stop closes stdin, requests systemd's configured termination sequence, and
// waits for the reaper's unit and cgroup fence. A failed/canceled wait leaves
// the handle available for a later retry.
func (h *StartHandle) Stop(ctx context.Context) error {
	if ctx == nil {
		ctx = context.Background()
	}
	select {
	case <-h.cleanupDone:
		return h.waitFenced(ctx)
	default:
	}
	_ = h.CloseStdin()
	h.cleanupRequested.Do(func() { close(h.cleanupRequest) })
	h.cancelStart()
	select {
	case <-h.requestDone:
	case <-ctx.Done():
		return ctx.Err()
	}
	if _, err := h.requestStop(ctx, h.conn); err != nil {
		if ctx.Err() != nil {
			return ctx.Err()
		}
		// A failed StopUnit reply is not evidence that no stop was queued. Retry
		// the ordered unit fence while the caller still owns this handle.
		if err := h.fenceUntilCertainContext(ctx); err != nil {
			return err
		}
	}
	return h.waitFenced(ctx)
}

// newNativeStopJob caches the one-shot go-systemd completion for replayable
// fence attempts that share this manager connection.
func newNativeStopJob(completed <-chan string) *nativeStopJob {
	ctx, cancel := context.WithCancel(context.Background())
	job := &nativeStopJob{done: make(chan struct{}), cancel: cancel}
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

func (j *nativeStopJob) completion() <-chan string {
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

func (h *StartHandle) release() {
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	_ = h.conn.ResetFailedUnitContext(ctx, h.name)
	h.conn.Close()
	h.cancel()
	h.stopMu.Lock()
	for _, job := range h.stopRequests {
		job.cancel()
	}
	h.stopMu.Unlock()
	h.referenceMu.Lock()
	for _, reference := range h.unitReferences {
		_ = reference.Close()
	}
	h.unitReferences = nil
	h.referenceMu.Unlock()
}

// A manager failure is a fencing barrier. Never complete or release the
// retained unit reference until cleanup is proven on an ordered connection.
func (h *StartHandle) fenceUntilCertain() error {
	return h.fenceUntilCertainContext(context.Background())
}

func (h *StartHandle) fenceUntilCertainContext(ctx context.Context) error {
	if ctx == nil {
		ctx = context.Background()
	}
	conn := h.conn
	cancelConnection := func() {}
	defer func() {
		if conn != h.conn {
			conn.Close()
		}
		cancelConnection()
	}()
	for {
		if err := ctx.Err(); err != nil {
			return err
		}
		fenceCtx, cancel := context.WithTimeout(ctx, h.backend.config.StopGrace+5*time.Second)
		h.stateMu.Lock()
		accepted := h.accepted
		invocationID, group := h.invocationID, h.group
		h.stateMu.Unlock()
		ordered := accepted || (conn == h.conn && conn.Connected())
		err := FenceUnitWith(fenceCtx, nativeUnitManager{handle: h, conn: conn}, h.name, UnitFence{
			Ordered: ordered, InvocationID: invocationID, ControlGroup: group,
		})
		cancel()
		if err == nil {
			return nil
		}
		if ctx.Err() != nil {
			return ctx.Err()
		}
		zap.L().Error("native systemd fencing remains blocked", zap.String("unit", h.name), zap.Error(err))
		timer := time.NewTimer(100 * time.Millisecond)
		select {
		case <-timer.C:
		case <-ctx.Done():
			timer.Stop()
			return ctx.Err()
		}
		replacement, cancelReplacement, connectErr := h.backend.config.OpenConnection()
		if connectErr == nil {
			h.retainUnitReference(replacement)
			if conn != h.conn {
				conn.Close()
			}
			cancelConnection()
			conn, cancelConnection = replacement, cancelReplacement
		}
	}
}

type nativeUnitManager struct {
	handle *StartHandle
	conn   *goSystemd.Conn
}

func (m nativeUnitManager) Snapshot(ctx context.Context, name string) (UnitSnapshot, error) {
	snapshot, err := Read(ctx, m.conn, name)
	m.handle.observeUnit(snapshot, err)
	return snapshot, err
}

func (m nativeUnitManager) Stop(ctx context.Context, _ string) (<-chan string, error) {
	return m.handle.requestStop(ctx, m.conn)
}

func (h *StartHandle) retainUnitReference(manager *goSystemd.Conn) error {
	if manager != nil && manager == h.conn && manager.Connected() {
		return nil
	}
	h.referenceMu.Lock()
	live := false
	kept := h.unitReferences[:0]
	for _, reference := range h.unitReferences {
		if reference.Connected() {
			live = true
			kept = append(kept, reference)
		} else {
			_ = reference.Close()
		}
	}
	h.unitReferences = kept
	h.referenceMu.Unlock()
	if live {
		return nil
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	openReference := h.backend.config.ReferenceUnit
	if openReference == nil {
		openReference = ReferenceUnit
	}
	reference, err := openReference(ctx, h.name)
	if err != nil {
		zap.L().Warn("could not retain native systemd unit after reconnect", zap.String("unit", h.name), zap.Error(err))
		return err
	}
	h.referenceMu.Lock()
	live = false
	for _, existing := range h.unitReferences {
		if existing.Connected() {
			live = true
			break
		}
	}
	if live {
		_ = reference.Close()
	} else {
		h.unitReferences = append(h.unitReferences, reference)
	}
	h.referenceMu.Unlock()
	return nil
}

func (h *StartHandle) requestStop(ctx context.Context, conn *goSystemd.Conn) (<-chan string, error) {
	h.stopMu.Lock()
	defer h.stopMu.Unlock()
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	if h.stopRequests == nil {
		h.stopRequests = make(map[*goSystemd.Conn]*nativeStopJob)
	}
	if job, ok := h.stopRequests[conn]; ok {
		return job.completion(), nil
	}
	stopUnit := h.backend.config.StopUnit
	if stopUnit == nil {
		stopUnit = Stop
	}
	completed, err := stopUnit(ctx, conn, h.name)
	if err != nil {
		return nil, err
	}
	job := newNativeStopJob(completed)
	h.stopRequests[conn] = job
	return job.completion(), nil
}

func systemdStopTimeout(grace time.Duration) uint64 {
	if grace <= 0 {
		return 1
	}
	usec := uint64(grace / time.Microsecond)
	if grace%time.Microsecond != 0 {
		usec++
	}
	return usec
}

func (h *StartHandle) reap() {
	code := -1
	statusObserved := false
	oomKilled := false
	var observationErr error
	readConn := h.conn
	var readConnCancel context.CancelFunc
	defer func() {
		if readConn != h.conn {
			readConn.Close()
			if readConnCancel != nil {
				readConnCancel()
			}
		}
	}()
	for {
		if readConn != h.conn {
			_ = h.retainUnitReference(readConn)
		}
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		props, err := h.readSnapshotOn(ctx, readConn)
		cancel()
		h.observeUnit(props, err)
		if err != nil {
			if errors.Is(err, context.DeadlineExceeded) && readConn.Connected() {
				zap.L().Warn("native systemd process observation timed out; retrying", zap.String("unit", h.name), zap.Error(err))
				if !h.waitChangeOn(readConn) {
					break
				}
				continue
			}
			if !readConn.Connected() {
				replacement, cancelReplacement, connectErr := h.backend.config.OpenConnection()
				if connectErr != nil {
					observationErr = errors.Join(err, fmt.Errorf("reconnect for native exit evidence: %w", connectErr))
					break
				}
				replacement.SetPropertiesSubscriber(h.updates, h.updateErrors)
				h.retainUnitReference(replacement)
				if readConn != h.conn {
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
		code, statusObserved, observationErr = nativeExitStatus(props, h.invocationID)
		commandOOM := h.backend.config.RetainUntilStop && props.Result == "oom-kill"
		if commandOOM && !statusObserved && observationErr == nil {
			code, statusObserved, observationErr = commandOOMExitStatus(props, h.group, GroupEmpty)
		}
		if statusObserved || observationErr != nil {
			oomKilled = props.Result == "oom-kill"
			break
		}
		if props.ExecMainPID == 0 && !commandOOM {
			observationErr = errors.New("native systemd invocation lost its process exit status")
			break
		}
		if !h.waitChangeOn(readConn) && readConn.Connected() {
			break
		}
	}
	if statusObserved {
		// Publish the manager observation before releasing waiters; cleanup and
		// stream/connection teardown remain tracked by cleanupDone.
		h.observeExit(code, true, observationErr)
		h.recordExit(code, observationErr, oomKilled)
		if h.backend.config.RetainUntilStop {
			h.waitForCommandCleanup()
		}
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	if unit, err := Read(ctx, h.conn, h.name); err == nil && unit.ControlGroup != "" {
		logCgroupMemoryDiagnostics(filepath.Join("/sys/fs/cgroup", unit.ControlGroup))
	}
	cancel()
	if !statusObserved {
		observationErr = errors.Join(observationErr, errors.New("native systemd exit status was not observed"))
		h.observeExit(code, false, observationErr)
	}
	fenceErr := h.fenceUntilCertain()
	_ = h.CloseStdin()
	h.release()
	h.completeWith(code, errors.Join(observationErr, fenceErr))
}

// A command cgroup may outlive its leader. Wait for a stop request or
// natural emptiness so background work stays alive without pinning empty units.
func (h *StartHandle) waitForCommandCleanup() {
	h.stateMu.Lock()
	group := h.group
	h.stateMu.Unlock()
	ticker := time.NewTicker(time.Second)
	defer ticker.Stop()
	for {
		if empty, err := GroupEmpty(group); err == nil && empty {
			return
		}
		select {
		case <-h.cleanupRequest:
			return
		case <-ticker.C:
		}
	}
}

func (h *StartHandle) readSnapshotOn(ctx context.Context, conn *goSystemd.Conn) (UnitSnapshot, error) {
	if read := h.backend.config.ReapSnapshot; read != nil && conn == h.conn {
		return read(ctx, h.conn, h.name)
	}
	return Read(ctx, conn, h.name)
}

func nativeExitStatus(unit UnitSnapshot, invocationID string) (int, bool, error) {
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

// PID 1 can finish an OOM stop before consuming the leader's SIGCHLD.
// The caller must first verify the invocation and check for a raw exit code.
func commandOOMExitStatus(unit UnitSnapshot, group string, groupEmpty func(string) (bool, error)) (int, bool, error) {
	if unit.Result != "oom-kill" || !unit.Stopped() || unit.JobID != 0 {
		return -1, false, nil
	}
	empty, err := groupEmpty(group)
	if err != nil {
		return -1, false, fmt.Errorf("inspect systemd command OOM domain: %w", err)
	}
	if !empty {
		return -1, false, nil
	}
	return 128 + int(syscall.SIGKILL), true, nil
}

func (h *StartHandle) waitChangeOn(conn *goSystemd.Conn) bool {
	timer := time.NewTimer(time.Second)
	defer timer.Stop()
	for {
		select {
		case update := <-h.updates:
			if update.UnitName == h.name {
				return conn.Connected()
			}
		case <-h.updateErrors:
			return conn.Connected()
		case <-timer.C:
			return conn.Connected()
		}
	}
}

func (h *StartHandle) diagnostics() string {
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	unit, err := Read(ctx, h.conn, h.name)
	return fmt.Sprintf("active=%s sub=%s unit_job=%d main_pid=%d exec_pid=%d exec_code=%d exec_status=%d result=%s error=%v", unit.ActiveState, unit.SubState, unit.JobID, unit.MainPID, unit.ExecMainPID, unit.ExecMainCode, unit.ExecMainStatus, unit.Result, err)
}
