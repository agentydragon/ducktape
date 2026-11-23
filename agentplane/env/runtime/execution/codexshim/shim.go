// Package codexshim exposes a Codex-compatible WebSocket over native execution.
package codexshim

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"time"

	"github.com/agentydragon/ducktape/agentplane/env/runtime/execution/native"
	"github.com/coder/websocket"
)

type rpcError struct {
	code    int
	message string
}

func (e rpcError) Error() string { return e.message }

type Handler struct {
	Server  *native.Server
	Limits  Limits
	observe func(string)
}

// Limits bounds resources consumed by each Codex connection.
type Limits struct {
	MaxMessageBytes         int64 `json:"max_message_bytes"`
	MaxConcurrentRequests   int   `json:"max_concurrent_requests"`
	MaxOpenFiles            int   `json:"max_open_files"`
	MaxBufferedHTTPBytes    int64 `json:"max_buffered_http_bytes"`
	MaxWalkDepth            int   `json:"max_walk_depth"`
	MaxWalkDirectories      int   `json:"max_walk_directories"`
	MaxWalkEntries          int   `json:"max_walk_entries"`
	MaxWalkResponseBytes    int   `json:"max_walk_response_bytes"`
	MaxDiscoveryRoots       int   `json:"max_discovery_roots"`
	MaxDiscoveryDepth       int   `json:"max_discovery_depth"`
	MaxDiscoveryDirectories int   `json:"max_discovery_directories"`
	MaxDiscoveryEntries     int   `json:"max_discovery_entries"`
	MaxDiscoveryFileBytes   int   `json:"max_discovery_file_bytes"`
	MaxDiscoveryBundleBytes int   `json:"max_discovery_bundle_bytes"`
	MaxReadFileBytes        int64 `json:"max_read_file_bytes"`
	MaxReadDirectoryEntries int   `json:"max_read_directory_entries"`
}

func DefaultLimits() Limits {
	return Limits{MaxMessageBytes: 16 << 20, MaxConcurrentRequests: 64, MaxOpenFiles: 128, MaxBufferedHTTPBytes: 8 << 20, MaxWalkDepth: 64, MaxWalkDirectories: 10000, MaxWalkEntries: 50000, MaxWalkResponseBytes: 4 << 20, MaxDiscoveryRoots: 128, MaxDiscoveryDepth: 6, MaxDiscoveryDirectories: 2000, MaxDiscoveryEntries: 20000, MaxDiscoveryFileBytes: 1 << 20, MaxDiscoveryBundleBytes: 16 << 20, MaxReadFileBytes: 512 << 20, MaxReadDirectoryEntries: 50000}
}

func (l Limits) Validate() error {
	if l.MaxMessageBytes <= 0 || l.MaxMessageBytes == int64(^uint64(0)>>1) || l.MaxConcurrentRequests <= 0 || l.MaxOpenFiles <= 0 || l.MaxBufferedHTTPBytes <= 0 || l.MaxBufferedHTTPBytes == int64(^uint64(0)>>1) || l.MaxWalkDepth <= 0 || l.MaxWalkDirectories <= 0 || l.MaxWalkEntries <= 0 || l.MaxWalkResponseBytes < minWalkResponseBytes || l.MaxDiscoveryRoots <= 0 || l.MaxDiscoveryDepth <= 0 || l.MaxDiscoveryDirectories <= 0 || l.MaxDiscoveryEntries <= 0 || l.MaxDiscoveryFileBytes <= 0 || l.MaxDiscoveryBundleBytes <= 0 || l.MaxReadFileBytes <= 0 || l.MaxReadFileBytes == int64(^uint64(0)>>1) || l.MaxReadDirectoryEntries <= 0 {
		return errors.New("all Codex protocol limits must be positive")
	}
	return nil
}

type request struct {
	ID     json.RawMessage `json:"id"`
	Method string          `json:"method"`
	Params json.RawMessage `json:"params"`
}
type params struct {
	maxBufferedHTTPBytes                                                                                                               int64
	maxWalkDepth, maxWalkDirectories, maxWalkEntries, maxWalkResponseBytes                                                             int
	maxDiscoveryRoots, maxDiscoveryDepth, maxDiscoveryDirectories, maxDiscoveryEntries, maxDiscoveryFileBytes, maxDiscoveryBundleBytes int
	maxReadFileBytes                                                                                                                   int64
	maxReadDirectoryEntries                                                                                                            int
	ResumeSessionID                                                                                                                    string `json:"resumeSessionId"`
	Roots                                                                                                                              []struct {
		ID      string          `json:"id"`
		Path    string          `json:"path"`
		Sandbox json.RawMessage `json:"sandbox"`
	} `json:"roots"`
	ProcessID string            `json:"processId"`
	Argv      []string          `json:"argv"`
	Cwd       string            `json:"cwd"`
	Env       map[string]string `json:"env"`
	EnvPolicy *struct {
		Inherit               string            `json:"inherit"`
		IgnoreDefaultExcludes bool              `json:"ignoreDefaultExcludes"`
		Exclude               []string          `json:"exclude"`
		Set                   map[string]string `json:"set"`
		IncludeOnly           []string          `json:"includeOnly"`
	} `json:"envPolicy"`
	TTY                   bool            `json:"tty"`
	PipeStdin             bool            `json:"pipeStdin"`
	Arg0                  *string         `json:"arg0"`
	Sandbox               json.RawMessage `json:"sandbox"`
	ManagedNetwork        json.RawMessage `json:"managedNetwork"`
	NetworkProxy          json.RawMessage `json:"networkProxy"`
	ShellSnapshot         json.RawMessage `json:"shellSnapshot"`
	EnforceManagedNetwork bool            `json:"enforceManagedNetwork"`
	AfterSeq              *uint64         `json:"afterSeq"`
	MaxBytes              int             `json:"maxBytes"`
	WaitMS                int             `json:"waitMs"`
	Chunk                 []byte          `json:"chunk"`
	WriteID               string          `json:"writeId"`
	Signal                string          `json:"signal"`
	Path                  string          `json:"path"`
	HTTPRequestID         string          `json:"requestId"`
	HTTPMethod            string          `json:"method"`
	HTTPURL               string          `json:"url"`
	Headers               []struct {
		Name        string  `json:"name"`
		Value       string  `json:"value"`
		ValueEnvVar *string `json:"valueEnvVar"`
	} `json:"headers"`
	BodyBase64      []byte `json:"bodyBase64"`
	TimeoutMS       *int   `json:"timeoutMs"`
	RedirectPolicy  string `json:"redirectPolicy"`
	StreamResponse  bool   `json:"streamResponse"`
	SourcePath      string `json:"sourcePath"`
	DestinationPath string `json:"destinationPath"`
	DataBase64      string `json:"dataBase64"`
	Options         struct {
		MaxDepth                int  `json:"maxDepth"`
		MaxDirectories          int  `json:"maxDirectories"`
		MaxEntries              int  `json:"maxEntries"`
		FollowDirectorySymlinks bool `json:"followDirectorySymlinks"`
		PruneHiddenDirectories  bool `json:"pruneHiddenDirectories"`
	} `json:"options"`
	Recursive      *bool  `json:"recursive"`
	Force          *bool  `json:"force"`
	FollowSymlinks *bool  `json:"followSymlinks"`
	HandleID       string `json:"handleId"`
	Mode           string `json:"mode"`
	Offset         int64  `json:"offset"`
	Len            int    `json:"len"`
}

func nonnull(v json.RawMessage) bool { return len(v) > 0 && string(v) != "null" }
func (h Handler) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	limits := h.Limits
	if limits == (Limits{}) {
		limits = DefaultLimits()
	}
	if limits.Validate() != nil {
		http.Error(w, "invalid executor limits", http.StatusInternalServerError)
		return
	}
	ws, err := websocket.Accept(w, r, nil)
	if err != nil {
		return
	}
	defer ws.CloseNow()
	ws.SetReadLimit(limits.MaxMessageBytes)
	requests := make(chan struct{}, limits.MaxConcurrentRequests)
	control := make(chan struct{}, limits.MaxConcurrentRequests)
	ctx, cancel := context.WithCancel(r.Context())
	var wg sync.WaitGroup
	var handleMu sync.Mutex
	var httpMu sync.Mutex
	httpStreams := map[string]bool{}
	handles := map[string]bool{}
	writeHandles := map[string]*os.File{}
	var token [16]byte
	if _, err := rand.Read(token[:]); err != nil {
		return
	}
	prefix := hex.EncodeToString(token[:]) + ":"
	var session *native.Session
	resumed := false
	started := false
	defer func() {
		cancel()
		wg.Wait()
		if session != nil {
			for handle := range handles {
				if file := writeHandles[handle]; file != nil {
					_ = file.Close()
				} else {
					_ = session.CloseFile(handle)
				}
			}
			h.Server.Detach(session.ID())
		}
	}()
	for {
		_, data, err := ws.Read(ctx)
		if err != nil {
			return
		}
		var q request
		if json.Unmarshal(data, &q) != nil {
			return
		}
		if h.observe != nil {
			h.observe(q.Method)
		}
		if q.Method == "initialized" {
			if session != nil && resumed && !started {
				started = true
				for _, id := range session.ProcessIDs() {
					go streamProcess(ctx, ws, session, id)
				}
			}
			continue
		}
		var p params
		if err = json.Unmarshal(q.Params, &p); err != nil {
			reply(ctx, ws, q.ID, nil, err)
			continue
		}
		if q.Method == "fs/writeFile" {
			var fields map[string]json.RawMessage
			if json.Unmarshal(q.Params, &fields) != nil || !nonnull(fields["dataBase64"]) {
				reply(ctx, ws, q.ID, nil, rpcError{-32602, "dataBase64 is required"})
				continue
			}
		}
		if q.Method == "initialize" {
			if session != nil {
				reply(ctx, ws, q.ID, nil, errors.New("already initialized"))
				continue
			}
			session, err = h.Server.OpenSession(p.ResumeSessionID)
			resumed = p.ResumeSessionID != ""
			if err != nil {
				if p.ResumeSessionID != "" && errors.Is(err, native.ErrUnknownSession) {
					err = rpcError{-32600, "unknown session id " + p.ResumeSessionID}
				} else if errors.Is(err, native.ErrSessionAttached) {
					err = rpcError{-32010, "session " + p.ResumeSessionID + " is already attached to another connection"}
				}
				reply(ctx, ws, q.ID, nil, err)
				continue
			}
			reply(ctx, ws, q.ID, map[string]any{"sessionId": session.ID(), "environmentInfo": environmentInfo()}, nil)
			continue
		}
		if session == nil {
			reply(ctx, ws, q.ID, nil, errors.New("not initialized"))
			continue
		}
		p.maxBufferedHTTPBytes = limits.MaxBufferedHTTPBytes
		p.maxWalkDepth, p.maxWalkDirectories, p.maxWalkEntries, p.maxWalkResponseBytes = limits.MaxWalkDepth, limits.MaxWalkDirectories, limits.MaxWalkEntries, limits.MaxWalkResponseBytes
		p.maxDiscoveryRoots, p.maxDiscoveryDepth, p.maxDiscoveryDirectories, p.maxDiscoveryEntries, p.maxDiscoveryFileBytes, p.maxDiscoveryBundleBytes = limits.MaxDiscoveryRoots, limits.MaxDiscoveryDepth, limits.MaxDiscoveryDirectories, limits.MaxDiscoveryEntries, limits.MaxDiscoveryFileBytes, limits.MaxDiscoveryBundleBytes
		p.maxReadFileBytes, p.maxReadDirectoryEntries = limits.MaxReadFileBytes, limits.MaxReadDirectoryEntries
		lane := requests
		switch q.Method {
		case "environment/info", "environment/status", "process/signal", "process/terminate", "fs/close":
			lane = control
		}
		select {
		case lane <- struct{}{}:
		default:
			reply(ctx, ws, q.ID, nil, rpcError{-32603, "too many concurrent requests"})
			continue
		}
		wg.Add(1)
		go func(q request, p params, s *native.Session) {
			defer wg.Done()
			defer func() { <-lane }()
			if q.Method == "http/request" && p.StreamResponse {
				if p.HTTPRequestID == "" {
					reply(ctx, ws, q.ID, nil, errors.New("stream request ID required"))
					return
				}
				httpMu.Lock()
				if httpStreams[p.HTTPRequestID] {
					httpMu.Unlock()
					reply(ctx, ws, q.ID, nil, errors.New("HTTP stream request ID already active"))
					return
				}
				httpStreams[p.HTTPRequestID] = true
				httpMu.Unlock()
				defer func() { httpMu.Lock(); delete(httpStreams, p.HTTPRequestID); httpMu.Unlock() }()
				streamHTTP(ctx, ws, q, p)
				return
			}
			originalHandle := p.HandleID
			fileHandleMethod := q.Method == "fs/open" || q.Method == "fs/readBlock" || q.Method == "fs/writeBlock" || q.Method == "fs/close"
			if fileHandleMethod {
				handleMu.Lock()
				defer handleMu.Unlock()
				p.HandleID = prefix + p.HandleID
				if originalHandle == "" || len(originalHandle) > 32 {
					reply(ctx, ws, q.ID, nil, rpcError{-32600, "file handle ID must be nonempty and at most 32 bytes"})
					return
				}
				if q.Method == "fs/open" && handles[p.HandleID] {
					reply(ctx, ws, q.ID, nil, rpcError{-32600, "handle already exists"})
					return
				}
				if q.Method == "fs/open" && len(handles) >= limits.MaxOpenFiles {
					reply(ctx, ws, q.ID, nil, rpcError{-32600, "file handle limit reached"})
					return
				}
				if q.Method != "fs/open" && !handles[p.HandleID] {
					if q.Method == "fs/close" {
						reply(ctx, ws, q.ID, map[string]any{}, nil)
						return
					}
					reply(ctx, ws, q.ID, nil, rpcError{-32004, "unknown handle"})
					return
				}
			}
			var result any
			var err error
			switch {
			case q.Method == "fs/open" && p.Mode == "replace":
				if nonnull(p.Sandbox) {
					err = errors.New("sandboxed file streaming unsupported")
					break
				}
				var path string
				path, err = localPath(p.Path)
				if err != nil {
					break
				}
				var file *os.File
				file, err = os.OpenFile(path, os.O_WRONLY|os.O_CREATE|os.O_TRUNC, 0666)
				if err == nil {
					writeHandles[p.HandleID] = file
					result = map[string]any{}
				}
			case q.Method == "fs/writeBlock":
				file := writeHandles[p.HandleID]
				if file == nil {
					err = errors.New("handle is not writable")
					break
				}
				if p.Offset < 0 || len(p.Chunk) == 0 || len(p.Chunk) > 1<<20 {
					err = errors.New("invalid write block")
					break
				}
				_, err = file.WriteAt(p.Chunk, p.Offset)
				result = map[string]any{}
			case q.Method == "fs/close" && writeHandles[p.HandleID] != nil:
				err = writeHandles[p.HandleID].Close()
				delete(writeHandles, p.HandleID)
				result = map[string]any{}
			case q.Method == "fs/readBlock" && writeHandles[p.HandleID] != nil:
				err = errors.New("handle is not readable")
			default:
				result, err = dispatch(ctx, s, q.Method, p)
			}
			if err == nil && q.Method == "fs/open" {
				handles[p.HandleID] = true
				result = map[string]any{"handleId": originalHandle}
			}
			if err == nil && q.Method == "fs/close" {
				delete(handles, p.HandleID)
			}
			if err != nil && strings.HasPrefix(q.Method, "fs/") {
				err = filesystemRPCError(err)
			}
			reply(ctx, ws, q.ID, result, err)
			if err == nil && q.Method == "process/start" {
				go streamProcess(ctx, ws, s, p.ProcessID)
			}
		}(q, p, session)
	}
}

func reply(ctx context.Context, ws *websocket.Conn, id json.RawMessage, result any, err error) {
	if len(id) == 0 {
		return
	}
	v := map[string]any{"id": id}
	if err != nil {
		code := -32602
		var re rpcError
		if errors.As(err, &re) {
			code = re.code
		}
		v["error"] = map[string]any{"code": code, "message": err.Error()}
	} else {
		v["result"] = result
	}
	b, e := json.Marshal(v)
	if e == nil {
		_ = ws.Write(ctx, websocket.MessageText, b)
	}
}

func dispatch(ctx context.Context, s *native.Session, method string, p params) (any, error) {
	if nonnull(p.Sandbox) || nonnull(p.ManagedNetwork) || nonnull(p.NetworkProxy) || nonnull(p.ShellSnapshot) || p.EnforceManagedNetwork {
		return nil, errors.New("sandbox, managed network and shell snapshots are not supported")
	}
	switch method {
	case "http/request":
		return httpRequest(ctx, p)
	case "capabilityRoots/discoverV1":
		return discover(p)
	case "environment/info":
		return environmentInfo(), nil
	case "environment/status":
		return map[string]any{"status": "ready"}, nil
	case "process/start":
		var policy *native.EnvPolicy
		if p.EnvPolicy != nil {
			policy = &native.EnvPolicy{Inherit: p.EnvPolicy.Inherit, IgnoreDefaultExcludes: p.EnvPolicy.IgnoreDefaultExcludes, Exclude: p.EnvPolicy.Exclude, Set: p.EnvPolicy.Set, IncludeOnly: p.EnvPolicy.IncludeOnly}
		}
		env, err := native.BuildEnvironment(policy, p.Env)
		if err != nil {
			return nil, err
		}
		err = s.Start(native.StartRequest{ProcessID: p.ProcessID, Argv: p.Argv, Cwd: p.Cwd, Environment: env, TTY: p.TTY, PipeStdin: p.PipeStdin, Arg0: p.Arg0})
		if errors.Is(err, native.ErrProcessExists) || errors.Is(err, native.ErrInvalidRequest) {
			return nil, rpcError{-32600, err.Error()}
		}
		if err != nil {
			return nil, rpcError{-32603, err.Error()}
		}
		return map[string]any{"processId": p.ProcessID}, err
	case "process/read":
		v, err := s.Read(ctx, p.ProcessID, p.AfterSeq, p.MaxBytes, time.Duration(p.WaitMS)*time.Millisecond)
		if errors.Is(err, native.ErrUnknownProcess) {
			return nil, rpcError{-32600, "unknown process id " + p.ProcessID}
		}
		chunks := make([]map[string]any, 0, len(v.Chunks))
		for _, c := range v.Chunks {
			chunks = append(chunks, map[string]any{"seq": c.Seq, "stream": c.Stream, "chunk": c.Data})
		}
		return map[string]any{"chunks": chunks, "nextSeq": v.NextSeq, "exited": v.Exited, "exitCode": v.ExitCode, "closed": v.Closed, "failure": v.Failure}, err
	case "process/write":
		v, err := s.WriteContext(ctx, p.ProcessID, p.WriteID, p.Chunk)
		return map[string]any{"status": v}, err
	case "process/signal":
		if p.Signal != "interrupt" {
			return nil, errors.New("unsupported signal")
		}
		return map[string]any{}, s.Signal(p.ProcessID)
	case "process/terminate":
		v, err := s.Terminate(p.ProcessID)
		return map[string]any{"running": v}, err
	case "fs/copy":
		return copyRequest(p)
	case "fs/getMetadata", "fs/readFile", "fs/writeFile", "fs/createDirectory", "fs/canonicalize", "fs/readDirectory", "fs/remove", "fs/walk":
		return filesystem(method, p)
	case "fs/open":
		if p.Mode != "" && p.Mode != "read" {
			return nil, errors.New("unsupported open mode")
		}
		if _, err := localPath(p.Path); err != nil {
			return nil, err
		}
		err := s.OpenFile(p.HandleID, p.Path)
		return map[string]any{"handleId": p.HandleID}, err
	case "fs/readBlock":
		b, eof, err := s.ReadFile(p.HandleID, p.Offset, p.Len)
		return map[string]any{"chunk": b, "eof": eof}, err
	case "fs/close":
		return map[string]any{}, s.CloseFile(p.HandleID)
	default:
		return nil, rpcError{-32601, fmt.Sprintf("unsupported executor method %s", strings.TrimSpace(method))}
	}
}

// streamProcess mirrors native process notifications while the attachment is live.
func streamProcess(ctx context.Context, ws *websocket.Conn, s *native.Session, id string) {
	var after *uint64
	for {
		result, err := s.Read(ctx, id, after, 1<<20, 30*time.Second)
		if err != nil {
			return
		}
		type event struct {
			seq    uint64
			method string
			params map[string]any
		}
		events := make([]event, 0, len(result.Chunks)+2)
		for _, c := range result.Chunks {
			events = append(events, event{c.Seq, "process/output", map[string]any{"processId": id, "seq": c.Seq, "stream": c.Stream, "chunk": c.Data}})
		}
		if result.ExitSeq != nil && *result.ExitSeq <= result.NextSeq && (after == nil || *result.ExitSeq > *after) {
			events = append(events, event{*result.ExitSeq, "process/exited", map[string]any{"processId": id, "seq": *result.ExitSeq, "exitCode": result.ExitCode}})
		}
		if result.ClosedSeq != nil && *result.ClosedSeq <= result.NextSeq && (after == nil || *result.ClosedSeq > *after) {
			events = append(events, event{*result.ClosedSeq, "process/closed", map[string]any{"processId": id, "seq": *result.ClosedSeq}})
		}
		sort.Slice(events, func(i, j int) bool { return events[i].seq < events[j].seq })
		for _, e := range events {
			b, err := json.Marshal(map[string]any{"method": e.method, "params": e.params})
			if err != nil {
				return
			}
			if err = ws.Write(ctx, websocket.MessageText, b); err != nil {
				return
			}
			seq := e.seq
			after = &seq
		}
		if result.Closed && result.ClosedSeq != nil && after != nil && *after >= *result.ClosedSeq {
			return
		}
	}
}

func environmentInfo() map[string]any {
	shell := os.Getenv("SHELL")
	if shell == "" {
		shell = "/bin/sh"
	}
	cwd, _ := os.Getwd()
	home, _ := os.UserHomeDir()
	uri := func(p string) string { return (&url.URL{Scheme: "file", Path: p}).String() }
	return map[string]any{"shell": map[string]any{"name": filepath.Base(shell), "path": shell}, "cwd": uri(cwd), "userHomeDir": uri(home), "platformOs": "linux", "executorVersion": "0.0.0", "capabilities": map[string]any{"httpHeaderEnvVars": true, "fileWriteStreaming": true}}
}
