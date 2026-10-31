package centralsampler

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

	"github.com/agentydragon/ducktape/agentplane/x/central_sampler/protocol"
	"github.com/google/uuid"
)

// CodeModeHost owns one shared code-mode child process and the protocol state for
// its logical sessions. Session IDs live until this connection fails or the
// Runtime shuts down; active CodeMode bindings (and their lease owners and
// Executors) are attached only while a sampler generation is running.
type CodeModeHost struct {
	binary       string
	logs         string
	store        *Store
	hostLifetime context.Context
	cancelHost   context.CancelFunc
	rpcTimeout   time.Duration

	connectMu         sync.Mutex
	mu                sync.Mutex
	closed            bool
	stopping          bool
	starting          chan struct{}
	retiring          chan struct{}
	host              *Host
	instanceID        string
	failed            error
	startProcess      func(context.Context, string, string) (*Host, error)
	closeProcess      func(*Host) error
	stampHostInstance func(context.Context, Owner, string) error

	nextID         int64
	pending        map[int64]*codePending
	active         map[string]*CodeMode
	delegates      map[int64]*CodeMode
	sessionIDs     map[string]string
	openedSessions map[string]struct{}
	writeGate      chan struct{}
}

type codePending struct {
	session  *CodeMode
	response chan codeReply
	initial  chan codeReply
}

func NewCodeModeHost(parent context.Context, settings Settings, store *Store) *CodeModeHost {
	lifetime, cancel := context.WithCancel(parent)
	return &CodeModeHost{
		binary: settings.CodeModeHost, logs: settings.Logs, store: store,
		hostLifetime: lifetime, cancelHost: cancel,
		rpcTimeout: time.Duration(settings.RPCTimeoutSeconds) * time.Second,
		pending:    map[int64]*codePending{}, active: map[string]*CodeMode{}, delegates: map[int64]*CodeMode{},
		sessionIDs: map[string]string{}, openedSessions: map[string]struct{}{},
		writeGate:    make(chan struct{}, 1),
		startProcess: StartHost, closeProcess: func(host *Host) error { return host.Close() },
		instanceID: uuid.NewString(),
	}
}

// InstanceID identifies this host process generation, including a restart within one sampler Pod.
// It is recorded for a conversation only after that conversation opens a host session.
func (m *CodeModeHost) InstanceID() string {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.instanceID
}

func (m *CodeModeHost) OpenSession(ctx context.Context, conversationID string, owner Owner, executor *Executor) (*CodeMode, error) {
	if conversationID == "" || executor == nil {
		return nil, errors.New("code-mode conversation and executor are required")
	}
	epoch := 0
	if m.store != nil {
		var err error
		epoch, err = m.store.CodeSessionEpoch(ctx, owner)
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
	runtime := newCodeModeBinding(ctx, m, host, m.store, owner, executor, sessionID)
	m.active[sessionID] = runtime
	m.mu.Unlock()

	if !opened {
		openCtx, cancel := context.WithTimeout(ctx, m.rpcTimeout)
		value, err := runtime.request(openCtx, codeSessionOperation{Method: "session/open", SessionID: sessionID}, false)
		cancel()
		if err == nil {
			var response codeHostResponse
			err = decodeProtocolObject(value, &response)
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
	if m.store != nil {
		stamp := m.store.StampCodeHostInstance
		if m.stampHostInstance != nil {
			stamp = m.stampHostInstance
		}
		if err := stamp(ctx, owner, instanceID); err != nil {
			_ = runtime.Detach(context.Background())
			return nil, err
		}
	}
	if runtime.Err() != nil {
		_ = runtime.Detach(context.Background())
		return nil, errCodeHostInstanceChanged
	}
	return runtime, nil
}

func (m *CodeModeHost) ensureHost() (*Host, error) {
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
			_ = m.closeProcess(host)
		}
		return nil, err
	}
}

func (m *CodeModeHost) startHost() (*Host, error) {
	host, err := m.startProcess(m.hostLifetime, m.binary, filepath.Join(m.logs, "code-mode-host.log"))
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
	offered := []string{yieldObservationCapability}
	hello, err := json.Marshal(codeHello{Type: "connection/hello", SupportedVersions: []int{1}, RequiredCapabilities: []string{}, OptionalCapabilities: offered})
	if err == nil {
		err = writeHostFrame(host.Stdin(), hello)
	}
	var ready codeFrame
	if err == nil {
		var payload []byte
		payload, err = readHostFrame(host.Stdout())
		if err == nil {
			err = decodeProtocolObject(payload, &ready)
		}
	}
	if err != nil {
		_ = host.Close()
		return nil, err
	}
	if ready.Type != codeReady || ready.SelectedVersion != 1 || ready.Capabilities == nil {
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

func (m *CodeModeHost) readLoop(host *Host) {
	for {
		payload, err := readHostFrame(host.Stdout())
		if err != nil {
			m.fail(host, fmt.Errorf("read code-mode host: %w", err))
			return
		}
		var frame codeFrame
		if err := decodeProtocolObject(payload, &frame); err != nil {
			m.fail(host, fmt.Errorf("decode code-mode host frame: %w", err))
			return
		}
		if err := m.dispatch(host, &frame, payload); err != nil {
			m.fail(host, err)
			return
		}
	}
}

func (m *CodeModeHost) dispatch(host *Host, frame *codeFrame, raw []byte) error {
	if frame.Type == codeResponse || frame.Type == codeInitialResponse {
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
			if frame.Type == codeResponse && pending.response != nil {
				channel = pending.response
				if pending.initial == nil {
					delete(m.pending, *frame.ID)
				} else {
					pending.response = nil
				}
			} else if frame.Type == codeInitialResponse && pending.initial != nil {
				channel = pending.initial
				delete(m.pending, *frame.ID)
				pending.initial = nil
			} else {
				m.mu.Unlock()
				return fmt.Errorf("unexpected duplicate code-mode response %d", *frame.ID)
			}
			var queued bool
			journaled, queued = pending.session.enqueueProtocol(raw, *frame.ID, "receive")
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
	if frame.Type == codeDelegateCancel {
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
	if frame.Type == codeDelegateRequest {
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
			journaled, queued := runtime.enqueueProtocol(raw, *frame.ID, "receive")
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
	if frame.Type == codeCellClosed {
		m.mu.Lock()
		if host != m.host {
			m.mu.Unlock()
			return nil
		}
		runtime := m.active[frame.SessionID]
		if runtime != nil {
			if _, queued := runtime.enqueueProtocol(raw, 0, "receive"); !queued {
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

func (m *CodeModeHost) sendDelegateError(host *Host, id int64, message string) error {
	frame := codeDelegateResponse{Type: "delegate/response", ID: id, Result: codeDelegateResult{Status: "error", Message: message}}
	payload, err := json.Marshal(frame)
	if err != nil {
		return err
	}
	return m.write(host, context.Background(), payload)
}

func (m *CodeModeHost) registerRequest(session *CodeMode, execute bool) (int64, <-chan codeReply, <-chan codeReply, error) {
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

func (m *CodeModeHost) discardUnopenedSession(conversationKey, sessionID string) {
	m.mu.Lock()
	if m.sessionIDs[conversationKey] == sessionID {
		if _, opened := m.openedSessions[sessionID]; !opened {
			delete(m.sessionIDs, conversationKey)
		}
	}
	m.mu.Unlock()
}

func (m *CodeModeHost) failBinding(runtime *CodeMode, err error) {
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

func (m *CodeModeHost) removeRequest(id int64, session *CodeMode) {
	m.mu.Lock()
	if pending := m.pending[id]; pending != nil && pending.session == session {
		delete(m.pending, id)
	}
	m.mu.Unlock()
}

func (m *CodeModeHost) send(host *Host, ctx context.Context, value any) error {
	payload, err := json.Marshal(value)
	if err != nil {
		return err
	}
	return m.write(host, ctx, payload)
}

func (m *CodeModeHost) write(host *Host, ctx context.Context, payload []byte) error {
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

func (m *CodeModeHost) detach(runtime *CodeMode) {
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

func (m *CodeModeHost) endDelegate(runtime *CodeMode, id int64) {
	m.mu.Lock()
	if m.delegates[id] == runtime {
		delete(m.delegates, id)
	}
	m.mu.Unlock()
}

func (m *CodeModeHost) fail(host *Host, err error) {
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
	active := make([]*CodeMode, 0, len(m.active))
	for _, runtime := range m.active {
		active = append(active, runtime)
	}
	m.active = map[string]*CodeMode{}
	m.delegates = map[int64]*CodeMode{}
	pending := m.pending
	m.pending = map[int64]*codePending{}
	m.mu.Unlock()
	_ = os.Remove(filepath.Join(m.logs, "code-mode-host.pid"))
	_ = host.Kill()
	go func() {
		_ = m.closeProcess(host)
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

func (m *CodeModeHost) watchHost(host *Host) {
	<-host.Done()
	if err := host.Wait(); err != nil {
		m.fail(host, fmt.Errorf("code-mode host exited: %w", err))
	} else {
		m.fail(host, errors.New("code-mode host exited"))
	}
}

func (m *CodeModeHost) StopAccepting() {
	m.mu.Lock()
	m.stopping = true
	m.mu.Unlock()
}

func (m *CodeModeHost) Close(ctx context.Context) error {
	m.mu.Lock()
	if m.closed {
		m.mu.Unlock()
		return nil
	}
	m.closed = true
	m.stopping = true
	host := m.host
	retiring := m.retiring
	active := make([]*CodeMode, 0, len(m.active))
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
		if err := m.closeProcess(host); err != nil {
			return err
		}
	}
	_ = os.Remove(filepath.Join(m.logs, "code-mode-host.pid"))
	return nil
}

func (m *CodeModeHost) writePID(pid int) {
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
