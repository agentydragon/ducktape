package codemode

import (
	"context"
	"encoding/binary"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"os"
	"path/filepath"
	"slices"
	"sync"
	"time"

	"github.com/agentydragon/ducktape/agentplane/central_sampler/protocol"
	"github.com/google/uuid"
)

// Manager owns one shared code-mode child process and the protocol state for
// its logical sessions. Session IDs live until this connection fails or the
// Runtime shuts down; active Session bindings (and their lease owners and
// Executors) are attached only while a sampler generation is running.
type Manager struct {
	binary       string
	logs         string
	hostLifetime context.Context
	cancelHost   context.CancelFunc
	rpcTimeout   time.Duration

	connectMu  sync.Mutex
	mu         sync.Mutex
	closed     bool
	stopping   bool
	starting   chan struct{}
	retiring   chan struct{}
	host       *Process
	instanceID string
	failed     error

	nextID         int64
	pending        map[int64]*codePending
	active         map[string]*Session
	delegates      map[int64]*Session
	sessionIDs     map[string]string
	openedSessions map[string]struct{}
	writeGate      chan struct{}
}

type codePending struct {
	session  *Session
	response chan codeReply
	initial  chan codeReply
}

func NewManager(parent context.Context, binary, logs string, rpcTimeout time.Duration) *Manager {
	lifetime, cancel := context.WithCancel(parent)
	return &Manager{
		binary: binary, logs: logs,
		hostLifetime: lifetime, cancelHost: cancel,
		rpcTimeout: rpcTimeout,
		pending:    map[int64]*codePending{}, active: map[string]*Session{}, delegates: map[int64]*Session{},
		sessionIDs: map[string]string{}, openedSessions: map[string]struct{}{},
		writeGate:  make(chan struct{}, 1),
		instanceID: uuid.NewString(),
	}
}

// InstanceID identifies this host process generation, including a restart within one sampler Pod.
// It is recorded for a conversation only after that conversation opens a host session.
func (m *Manager) InstanceID() string {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.instanceID
}

func (m *Manager) OpenSession(ctx context.Context, conversationID string, journal Journal, delegate DelegateHandler) (*Session, error) {
	if conversationID == "" || delegate == nil {
		return nil, errors.New("code-mode conversation and delegate handler are required")
	}
	epoch := 0
	if journal != nil {
		var err error
		epoch, err = journal.Epoch(ctx)
		if err != nil {
			return nil, err
		}
	}
	conversationKey := conversationID
	if epoch > 0 {
		conversationKey = fmt.Sprintf("%s:%d", conversationID, epoch)
	}
	m.connectMu.Lock()
	defer m.connectMu.Unlock()
	if _, err := m.ensureHost(); err != nil {
		return nil, err
	}

	m.mu.Lock()
	if m.closed || m.stopping {
		m.mu.Unlock()
		return nil, errors.New("code-mode host is shutting down")
	}
	sessionID := m.sessionIDs[conversationKey]
	if sessionID == "" {
		sessionID = uuid.NewString()
		m.sessionIDs[conversationKey] = sessionID
	}
	if m.active[sessionID] != nil {
		m.mu.Unlock()
		return nil, fmt.Errorf("code-mode session %s already has an active lease binding", sessionID)
	}
	_, opened := m.openedSessions[sessionID]
	host := m.host
	instanceID := m.instanceID
	runtime := newSession(ctx, m, host, journal, delegate, sessionID)
	m.active[sessionID] = runtime
	m.mu.Unlock()

	if !opened {
		openCtx, cancel := context.WithTimeout(ctx, m.rpcTimeout)
		value, err := runtime.Request(openCtx, SessionOperation{Method: "session/open", SessionID: sessionID}, false)
		cancel()
		if err == nil {
			var response HostResponse
			err = protocol.DecodeObject(value, &response)
			if err == nil && (response.Type != "session/ready" || response.SessionID != sessionID) {
				err = fmt.Errorf("unexpected code-mode session/open response: %s", protocol.CanonicalJSON(response))
			}
		}
		if err != nil {
			_ = runtime.Detach(context.Background())
			m.discardUnopenedSession(conversationKey, sessionID)
			return nil, fmt.Errorf("open code-mode session: %w", err)
		}
		runtimeErr := runtime.Err()
		m.mu.Lock()
		if m.active[sessionID] == runtime && m.host != nil && runtimeErr == nil {
			m.openedSessions[sessionID] = struct{}{}
		}
		m.mu.Unlock()
	}
	if journal != nil {
		if err := journal.StampHost(ctx, instanceID); err != nil {
			_ = runtime.Detach(context.Background())
			return nil, err
		}
	}
	if runtime.Err() != nil {
		_ = runtime.Detach(context.Background())
		return nil, ErrHostInstanceChanged
	}
	return runtime, nil
}

func (m *Manager) ensureHost() (*Process, error) {
	for {
		m.mu.Lock()
		if m.closed || m.stopping {
			m.mu.Unlock()
			return nil, errors.New("code-mode host is shutting down")
		}
		if m.host != nil {
			host := m.host
			m.mu.Unlock()
			return host, nil
		}
		if retiring := m.retiring; retiring != nil {
			m.mu.Unlock()
			select {
			case <-retiring:
				continue
			case <-m.hostLifetime.Done():
				return nil, errors.New("code-mode host lifetime ended")
			}
		}
		if started := m.starting; started != nil {
			m.mu.Unlock()
			select {
			case <-started:
				continue
			case <-m.hostLifetime.Done():
				return nil, errors.New("code-mode host lifetime ended")
			}
		}
		started := make(chan struct{})
		m.starting = started
		m.mu.Unlock()

		host, err := m.startHost()
		m.mu.Lock()
		if err == nil && !m.closed && !m.stopping {
			m.host = host
			m.failed = nil
			m.writePID(host.PID())
			m.starting = nil
			close(started)
			m.mu.Unlock()
			slog.Info("central sampler code-mode host started", "pid", host.PID())
			go m.readLoop(host)
			go m.watchHost(host)
			return host, nil
		}
		if err == nil {
			err = errors.New("code-mode host is shutting down")
		}
		m.starting = nil
		close(started)
		m.mu.Unlock()
		if host != nil {
			_ = host.Close()
		}
		return nil, err
	}
}

func (m *Manager) startHost() (*Process, error) {
	host, err := StartProcess(m.hostLifetime, m.binary, filepath.Join(m.logs, "code-mode-host.log"))
	if err != nil {
		return nil, err
	}
	timer := time.AfterFunc(m.rpcTimeout, func() { _ = host.Kill() })
	defer timer.Stop()
	// Codex rust-v0.162.1 code-mode/src/remote_session/connection.rs establish: no required
	// capabilities; yield-observation is optional and used only once the host acknowledges it
	// (supported_yield_signal).
	// TODO: Codex also offers session-cell-execution-resource-limits as optional; the sampler does
	// not negotiate per-session cell execution resource limits yet.
	offered := []string{YieldObservationCapability}
	hello, err := json.Marshal(Hello{Type: "connection/hello", SupportedVersions: []int{1}, RequiredCapabilities: []string{}, OptionalCapabilities: offered})
	if err == nil {
		err = writeHostFrame(host.Stdin(), hello)
	}
	var ready Frame
	if err == nil {
		var payload []byte
		payload, err = readHostFrame(host.Stdout())
		if err == nil {
			err = protocol.DecodeObject(payload, &ready)
		}
	}
	if err != nil {
		_ = host.Close()
		return nil, err
	}
	if ready.Type != Ready || ready.SelectedVersion != 1 || ready.Capabilities == nil {
		_ = host.Close()
		return nil, fmt.Errorf("unexpected code-mode handshake: %s", protocol.CanonicalJSON(ready))
	}
	for _, capability := range ready.Capabilities {
		if !slices.Contains(offered, capability) {
			_ = host.Close()
			return nil, fmt.Errorf("code-mode host acknowledged unoffered capability %q", capability)
		}
	}
	host.capabilities = ready.Capabilities
	return host, nil
}

func writeHostFrame(writer io.Writer, payload []byte) error {
	if len(payload) > maxCodeModeFrame {
		return errors.New("upstream code-mode frame exceeds protocol limit")
	}
	var header [4]byte
	binary.LittleEndian.PutUint32(header[:], uint32(len(payload)))
	for _, part := range [][]byte{header[:], payload} {
		for len(part) > 0 {
			written, err := writer.Write(part)
			if err != nil {
				return err
			}
			if written == 0 {
				return io.ErrShortWrite
			}
			part = part[written:]
		}
	}
	return nil
}

func readHostFrame(reader io.Reader) ([]byte, error) {
	var header [4]byte
	if _, err := io.ReadFull(reader, header[:]); err != nil {
		return nil, err
	}
	length := binary.LittleEndian.Uint32(header[:])
	if length > maxCodeModeFrame {
		return nil, errors.New("upstream code-mode frame exceeds protocol limit")
	}
	payload := make([]byte, int(length))
	if _, err := io.ReadFull(reader, payload); err != nil {
		return nil, err
	}
	return payload, nil
}

func (m *Manager) readLoop(host *Process) {
	for {
		payload, err := readHostFrame(host.Stdout())
		if err != nil {
			m.fail(host, fmt.Errorf("read code-mode host: %w", err))
			return
		}
		var frame Frame
		if err := protocol.DecodeObject(payload, &frame); err != nil {
			m.fail(host, fmt.Errorf("decode code-mode host frame: %w", err))
			return
		}
		if err := m.dispatch(host, &frame, payload); err != nil {
			m.fail(host, err)
			return
		}
	}
}

func (m *Manager) dispatch(host *Process, frame *Frame, raw []byte) error {
	if frame.Type == Response || frame.Type == InitialResponse {
		if frame.ID == nil || frame.Result == nil {
			return errors.New("code-mode response has no ID or result")
		}
		m.mu.Lock()
		if host != m.host {
			m.mu.Unlock()
			return nil
		}
		pending := m.pending[*frame.ID]
		var channel chan codeReply
		var journaled <-chan error
		if pending != nil {
			if frame.Type == Response && pending.response != nil {
				channel = pending.response
				if pending.initial == nil {
					delete(m.pending, *frame.ID)
				} else {
					pending.response = nil
				}
			} else if frame.Type == InitialResponse && pending.initial != nil {
				channel = pending.initial
				delete(m.pending, *frame.ID)
				pending.initial = nil
			} else {
				m.mu.Unlock()
				return fmt.Errorf("unexpected duplicate code-mode response %d", *frame.ID)
			}
			var queued bool
			journaled, queued = pending.session.enqueueProtocol(raw, *frame.ID)
			if !queued {
				m.mu.Unlock()
				pending.session.fail(errors.New("code-mode evidence queue is full"))
				return nil
			}
		}
		knownID := *frame.ID <= m.nextID
		m.mu.Unlock()
		if pending == nil {
			if knownID { // A canceled request may still produce a late response.
				return nil
			}
			return fmt.Errorf("code-mode response references unknown future request %d", *frame.ID)
		}
		if channel == nil {
			return fmt.Errorf("unexpected code-mode response type %s for request %d", frame.Type, *frame.ID)
		}
		select {
		case channel <- codeReply{Result: frame.Result, Journaled: journaled}:
		default:
			return fmt.Errorf("duplicate code-mode response for request %d", *frame.ID)
		}
		return nil
	}
	if frame.Type == DelegateCancel {
		if frame.ID != nil {
			m.mu.Lock()
			if host == m.host {
				if runtime := m.delegates[*frame.ID]; runtime != nil {
					runtime.cancelDelegate(*frame.ID)
				}
			}
			m.mu.Unlock()
		}
		return nil
	}
	if frame.Type == DelegateRequest {
		if frame.ID == nil || frame.SessionID == "" || frame.Request == nil {
			return errors.New("code-mode delegate request is missing ID, sessionId, or request")
		}
		m.mu.Lock()
		if host != m.host {
			m.mu.Unlock()
			return nil
		}
		runtime := m.active[frame.SessionID]
		if runtime != nil {
			if m.delegates[*frame.ID] != nil {
				m.mu.Unlock()
				return fmt.Errorf("duplicate active code-mode delegate ID %d", *frame.ID)
			}
			journaled, queued := runtime.enqueueProtocol(raw, *frame.ID)
			if !queued {
				m.mu.Unlock()
				runtime.fail(errors.New("code-mode evidence queue is full"))
				return nil
			}
			if runtime.beginDelegate(*frame.ID) {
				m.delegates[*frame.ID] = runtime
				m.mu.Unlock()
				go runtime.delegate(*frame.ID, frame, journaled)
				return nil
			}
			m.mu.Unlock()
			return m.sendDelegateError(host, *frame.ID, "code-mode session binding is closing")
		}
		m.mu.Unlock()
		// An idle logical session has no active Executor binding. It cannot have a live cell
		// by construction; reject any late delegate without affecting other sessions.
		return m.sendDelegateError(host, *frame.ID, "code-mode session has no active executor binding")
	}
	if frame.Type == CellClosed {
		m.mu.Lock()
		if host != m.host {
			m.mu.Unlock()
			return nil
		}
		runtime := m.active[frame.SessionID]
		if runtime != nil {
			if _, queued := runtime.enqueueProtocol(raw, 0); !queued {
				m.mu.Unlock()
				runtime.fail(errors.New("code-mode evidence queue is full"))
				return nil
			}
		}
		m.mu.Unlock()
		return nil
	}
	return fmt.Errorf("unexpected code-mode frame: %s", protocol.CanonicalJSON(frame))
}

func (m *Manager) sendDelegateError(host *Process, id int64, message string) error {
	frame := DelegateResponse{Type: "delegate/response", ID: id, Result: DelegateResult{Status: "error", Message: message}}
	payload, err := json.Marshal(frame)
	if err != nil {
		return err
	}
	return m.write(host, context.Background(), payload)
}

func (m *Manager) registerRequest(session *Session, execute bool) (int64, <-chan codeReply, <-chan codeReply, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.closed || m.host == nil || m.host != session.host || m.active[session.sessionID] != session {
		return 0, nil, nil, errors.New("code-mode host connection is not available")
	}
	m.nextID++
	id := m.nextID
	pending := &codePending{session: session, response: make(chan codeReply, 1)}
	if execute {
		pending.initial = make(chan codeReply, 1)
	}
	m.pending[id] = pending
	return id, pending.response, pending.initial, nil
}

func (m *Manager) discardUnopenedSession(conversationKey, sessionID string) {
	m.mu.Lock()
	if m.sessionIDs[conversationKey] == sessionID {
		if _, opened := m.openedSessions[sessionID]; !opened {
			delete(m.sessionIDs, conversationKey)
		}
	}
	m.mu.Unlock()
}

func (m *Manager) failBinding(runtime *Session, err error) {
	m.mu.Lock()
	if m.active[runtime.sessionID] == runtime {
		delete(m.active, runtime.sessionID)
	}
	for id, pending := range m.pending {
		if pending.session != runtime {
			continue
		}
		delete(m.pending, id)
		select {
		case pending.response <- codeReply{Err: err}:
		default:
		}
		if pending.initial != nil {
			select {
			case pending.initial <- codeReply{Err: err}:
			default:
			}
		}
	}
	for id, delegate := range m.delegates {
		if delegate == runtime {
			delete(m.delegates, id)
		}
	}
	m.mu.Unlock()
}

func (m *Manager) removeRequest(id int64, session *Session) {
	m.mu.Lock()
	if pending := m.pending[id]; pending != nil && pending.session == session {
		delete(m.pending, id)
	}
	m.mu.Unlock()
}

func (m *Manager) send(host *Process, ctx context.Context, value any) error {
	payload, err := json.Marshal(value)
	if err != nil {
		return err
	}
	return m.write(host, ctx, payload)
}

func (m *Manager) write(host *Process, ctx context.Context, payload []byte) error {
	if host == nil {
		return errors.New("code-mode host connection is unavailable")
	}
	m.mu.Lock()
	connected := m.host == host && !m.closed
	m.mu.Unlock()
	if !connected {
		return errors.New("code-mode host connection was replaced")
	}
	select {
	case m.writeGate <- struct{}{}:
	case <-ctx.Done():
		return ctx.Err()
	case <-m.hostLifetime.Done():
		return errors.New("code-mode host lifetime ended")
	}
	defer func() { <-m.writeGate }()
	m.mu.Lock()
	connected = m.host == host && !m.closed
	m.mu.Unlock()
	if !connected {
		return errors.New("code-mode host connection was replaced")
	}
	if err := ctx.Err(); err != nil {
		return err
	}
	timer := time.AfterFunc(m.rpcTimeout, func() {
		m.fail(host, errors.New("code-mode host write timed out"))
	})
	defer timer.Stop()
	if err := writeHostFrame(host.Stdin(), payload); err != nil {
		m.fail(host, fmt.Errorf("write code-mode host frame: %w", err))
		return err
	}
	return nil
}

func (m *Manager) detach(runtime *Session) {
	m.mu.Lock()
	if m.active[runtime.sessionID] == runtime {
		delete(m.active, runtime.sessionID)
	}
	for id, pending := range m.pending {
		if pending.session == runtime {
			delete(m.pending, id)
			select {
			case pending.response <- codeReply{Err: errors.New("code-mode session detached")}:
			default:
			}
			if pending.initial != nil {
				select {
				case pending.initial <- codeReply{Err: errors.New("code-mode session detached")}:
				default:
				}
			}
		}
	}
	for id, delegate := range m.delegates {
		if delegate == runtime {
			delete(m.delegates, id)
		}
	}
	m.mu.Unlock()
}

func (m *Manager) endDelegate(runtime *Session, id int64) {
	m.mu.Lock()
	if m.delegates[id] == runtime {
		delete(m.delegates, id)
	}
	m.mu.Unlock()
}

func (m *Manager) fail(host *Process, err error) {
	m.mu.Lock()
	if host != m.host || m.failed != nil || m.closed {
		m.mu.Unlock()
		return
	}
	m.failed = err
	m.host = nil
	m.instanceID = uuid.NewString()
	retiring := make(chan struct{})
	m.retiring = retiring
	m.sessionIDs = map[string]string{}
	m.openedSessions = map[string]struct{}{}
	active := make([]*Session, 0, len(m.active))
	for _, runtime := range m.active {
		active = append(active, runtime)
	}
	m.active = map[string]*Session{}
	m.delegates = map[int64]*Session{}
	pending := m.pending
	m.pending = map[int64]*codePending{}
	m.mu.Unlock()
	_ = os.Remove(filepath.Join(m.logs, "code-mode-host.pid"))
	_ = host.Kill()
	go func() {
		_ = host.Close()
		<-host.Done()
		m.mu.Lock()
		if m.retiring == retiring {
			m.retiring = nil
			close(retiring)
		}
		m.mu.Unlock()
	}()
	for _, runtime := range active {
		runtime.fail(err)
	}
	for _, item := range pending {
		reply := codeReply{Err: err}
		select {
		case item.response <- reply:
		default:
		}
		if item.initial != nil {
			select {
			case item.initial <- reply:
			default:
			}
		}
	}
	slog.Error("central sampler code-mode host failed", "pid", host.PID(), "error", err)
}

func (m *Manager) watchHost(host *Process) {
	<-host.Done()
	if err := host.Wait(); err != nil {
		m.fail(host, fmt.Errorf("code-mode host exited: %w", err))
	} else {
		m.fail(host, errors.New("code-mode host exited"))
	}
}

func (m *Manager) StopAccepting() {
	m.mu.Lock()
	m.stopping = true
	m.mu.Unlock()
}

func (m *Manager) Close(ctx context.Context) error {
	m.mu.Lock()
	if m.closed {
		m.mu.Unlock()
		return nil
	}
	m.closed = true
	m.stopping = true
	host := m.host
	retiring := m.retiring
	active := make([]*Session, 0, len(m.active))
	for _, runtime := range m.active {
		active = append(active, runtime)
	}
	m.mu.Unlock()
	m.cancelHost()
	if retiring != nil {
		select {
		case <-retiring:
		case <-ctx.Done():
			return ctx.Err()
		}
	}
	for _, runtime := range active {
		_ = runtime.Detach(ctx)
	}
	if host != nil {
		if err := host.Close(); err != nil {
			return err
		}
	}
	_ = os.Remove(filepath.Join(m.logs, "code-mode-host.pid"))
	return nil
}

func (m *Manager) writePID(pid int) {
	path := filepath.Join(m.logs, "code-mode-host.pid")
	temp := path + ".tmp"
	if err := os.WriteFile(temp, []byte(fmt.Sprintf("%d\n", pid)), 0o640); err != nil {
		slog.Error("write code-mode host PID diagnostic", "path", path, "error", err)
		return
	}
	if err := os.Rename(temp, path); err != nil {
		_ = os.Remove(temp)
		slog.Error("publish code-mode host PID diagnostic", "path", path, "error", err)
	}
}
