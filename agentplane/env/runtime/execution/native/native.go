// Package native owns execution sessions and their local processes.
package native

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"math"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"runtime"
	"sort"
	"strings"
	"sync"
	"sync/atomic"
	"syscall"
	"time"
	"unsafe"

	"golang.org/x/sys/unix"
)

var (
	ErrUnknownSession  = errors.New("session not found")
	ErrSessionAttached = errors.New("session already attached")
	ErrUnknownProcess  = errors.New("process not found")
	ErrProcessExists   = errors.New("process ID already exists")
	ErrInvalidRequest  = errors.New("invalid request")
)

// Limits bounds native execution resources and retention within one server.
type Limits struct {
	MaxSessions                   int `json:"max_sessions"`
	MaxProcessesPerSession        int `json:"max_processes_per_session"`
	MaxFilesPerSession            int `json:"max_files_per_session"`
	MaxWritesPerProcess           int `json:"max_writes_per_process"`
	MaxProcesses                  int `json:"max_processes"`
	MaxOutputBytesPerProcess      int `json:"max_output_bytes_per_process"`
	MaxOutputBytes                int `json:"max_output_bytes"`
	MaxWrites                     int `json:"max_writes"`
	MaxReadBytes                  int `json:"max_read_bytes"`
	MaxStdinQueueBytesPerProcess  int `json:"max_stdin_queue_bytes_per_process"`
	MaxStdinQueueBytes            int `json:"max_stdin_queue_bytes"`
	MaxStdinQueueChunksPerProcess int `json:"max_stdin_queue_chunks_per_process"`
	SessionRetentionSeconds       int `json:"session_retention_seconds"`
	ProcessRetentionSeconds       int `json:"process_retention_seconds"`
}

// DefaultLimits returns the limits used by New.
func DefaultLimits() Limits {
	return Limits{
		MaxSessions:                   256,
		MaxProcessesPerSession:        512,
		MaxFilesPerSession:            128,
		MaxWritesPerProcess:           4096,
		MaxProcesses:                  1024,
		MaxOutputBytesPerProcess:      1 << 20,
		MaxOutputBytes:                128 << 20,
		MaxWrites:                     65536,
		MaxReadBytes:                  1 << 20,
		MaxStdinQueueBytesPerProcess:  4 << 20,
		MaxStdinQueueBytes:            64 << 20,
		MaxStdinQueueChunksPerProcess: 128,
		SessionRetentionSeconds:       3600,
		ProcessRetentionSeconds:       30,
	}
}

// Validate checks that every limit is positive and retention fits a duration.
func (l Limits) Validate() error {
	bounds := []struct {
		name  string
		value int
	}{
		{"max sessions", l.MaxSessions},
		{"max processes per session", l.MaxProcessesPerSession},
		{"max files per session", l.MaxFilesPerSession},
		{"max retained writes per process", l.MaxWritesPerProcess},
		{"max processes", l.MaxProcesses},
		{"max output bytes per process", l.MaxOutputBytesPerProcess},
		{"max output bytes", l.MaxOutputBytes},
		{"max retained writes", l.MaxWrites},
		{"max read bytes", l.MaxReadBytes},
		{"max stdin queue bytes per process", l.MaxStdinQueueBytesPerProcess},
		{"max stdin queue bytes", l.MaxStdinQueueBytes},
		{"max stdin queue chunks per process", l.MaxStdinQueueChunksPerProcess},
		{"session retention", l.SessionRetentionSeconds},
		{"process retention", l.ProcessRetentionSeconds},
	}
	for _, bound := range bounds {
		if bound.value <= 0 {
			return fmt.Errorf("%s must be positive", bound.name)
		}
	}
	const maxRetentionSeconds = math.MaxInt64 / int64(time.Second)
	if int64(l.SessionRetentionSeconds) > maxRetentionSeconds || int64(l.ProcessRetentionSeconds) > maxRetentionSeconds {
		return errors.New("retention seconds exceed the supported duration")
	}
	return nil
}

// EnvPolicy describes the native execution environment policy.
type EnvPolicy struct {
	Inherit               string
	IgnoreDefaultExcludes bool
	Exclude               []string
	Set                   map[string]string
	IncludeOnly           []string
}

// BuildEnvironment applies the Codex shell policy, including credential scrubbing.
func BuildEnvironment(policy *EnvPolicy, explicit map[string]string) ([]string, error) {
	env := map[string]string{}
	matches := func(key string, patterns []string) bool {
		for _, p := range patterns {
			var expr strings.Builder
			expr.WriteString("(?i)^")
			for _, r := range p {
				switch r {
				case '*':
					expr.WriteString(".*")
				case '?':
					expr.WriteString(".")
				default:
					expr.WriteString(regexp.QuoteMeta(string(r)))
				}
			}
			expr.WriteString("$")
			if regexp.MustCompile(expr.String()).MatchString(key) {
				return true
			}
		}
		return false
	}
	if policy != nil {
		if policy.Inherit != "all" && policy.Inherit != "none" && policy.Inherit != "core" {
			return nil, errors.New("unsupported inherit policy")
		}
		core := []string{"PATH", "SHELL", "TMPDIR", "TEMP", "TMP", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "LOGNAME", "USER"}
		for _, entry := range os.Environ() {
			k, v, ok := strings.Cut(entry, "=")
			if !ok || policy.Inherit == "none" || (policy.Inherit == "core" && !matches(k, core)) {
				continue
			}
			env[k] = v
		}
		for k := range env {
			if (!policy.IgnoreDefaultExcludes && matches(k, []string{"*KEY*", "*SECRET*", "*TOKEN*"})) || matches(k, policy.Exclude) {
				delete(env, k)
			}
		}
		for k, v := range policy.Set {
			env[k] = v
		}
		if len(policy.IncludeOnly) > 0 {
			for k := range env {
				if !matches(k, policy.IncludeOnly) {
					delete(env, k)
				}
			}
		}
	}
	for k, v := range explicit {
		env[k] = v
	}
	restricted := []string{"CODEX_EXEC_SERVER_NOISE_AUTH_TOKEN", "NODE_REPL_AUTH_TOKEN", "CODEX_GUARDIAN_DECISIONS_API_KEY", "OPENAI_FEDERATION_RULE_ID", "OPENAI_IDENTITY_TOKEN_FILE", "OPENAI_WORKLOAD_IDENTITY_CONTEXT"}
	for k := range env {
		if matches(k, restricted) {
			delete(env, k)
		}
	}
	keys := make([]string, 0, len(env))
	for k := range env {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	result := make([]string, 0, len(keys))
	for _, k := range keys {
		result = append(result, k+"="+env[k])
	}
	return result, nil
}

type StartRequest struct {
	ProcessID   string
	Argv        []string
	Cwd         string
	Environment []string
	TTY         bool
	PipeStdin   bool
	Arg0        *string
}
type Chunk struct {
	Seq    uint64
	Stream string
	Data   []byte
}
type ReadResult struct {
	Chunks    []Chunk
	NextSeq   uint64
	Exited    bool
	ExitCode  *int
	Closed    bool
	Failure   *string
	ExitSeq   *uint64
	ClosedSeq *uint64
}
type FileMetadata struct {
	IsDirectory bool
	IsFile      bool
	IsSymlink   bool
	Size        uint64
}

// ManagedCommand is an externally supervised process owned until Release completes.
type ManagedCommand interface {
	Wait(context.Context) (int, error)
	Signal(context.Context, syscall.Signal) error
	Release(context.Context) error
}

// CommandLauncher starts the command in its configured resource domain and borrows
// the supplied descriptors until it returns. The caller retains descriptor ownership.
type CommandLauncher func(context.Context, *exec.Cmd, bool) (ManagedCommand, error)

type Server struct {
	mu           sync.Mutex
	limits       Limits
	sessions     map[string]*Session
	closed       bool
	outputBytes  atomic.Int64
	processCount atomic.Int64
	writeCount   atomic.Int64
	stdinBytes   atomic.Int64
	fatal        chan error
	fatalOnce    sync.Once
	waitChild    func(int, *childSiginfo) error
	launcher     CommandLauncher
}
type Session struct {
	id               string
	server           *Server
	mu               sync.Mutex
	changeMu         sync.Mutex
	changed          chan struct{}
	processes        map[string]*process
	files            map[string]*os.File
	closed           bool
	attachments      int
	expiry           *time.Timer
	expiryGeneration uint64
}
type process struct {
	mu                 sync.Mutex
	reapRequested      bool
	reaped             bool
	ownershipLost      bool
	cmd                *exec.Cmd
	managed            ManagedCommand
	chunks             []Chunk
	bytes              int
	next               uint64
	changed            chan struct{}
	exited, closed     bool
	exitCode           *int
	exitSeq, closedSeq *uint64
	streamsClosed      bool
	outputDisabled     bool
	server             *Server
	session            *Session
	id                 string
	readers            []*os.File
	failure            *string

	writeMu      sync.Mutex
	writes       map[string]struct{}
	stdin        *os.File
	stdinClosed  bool
	stdinQueue   [][]byte
	stdinBytes   int
	stdinChanged chan struct{}
	stdinDone    chan struct{}
}

func New() *Server { return newServer(DefaultLimits()) }

// NewWithLimits validates limits before using them for new sessions.
func NewWithLimits(limits Limits) (*Server, error) {
	if err := limits.Validate(); err != nil {
		return nil, err
	}
	return newServer(limits), nil
}

// NewWithLauncher uses an external owner for command placement. A nil launcher
// retains the existing direct-child behavior.
func NewWithLauncher(limits Limits, launcher CommandLauncher) (*Server, error) {
	s, err := NewWithLimits(limits)
	if err != nil {
		return nil, err
	}
	s.launcher = launcher
	return s, nil
}

func newServer(limits Limits) *Server {
	return &Server{limits: limits, sessions: make(map[string]*Session), fatal: make(chan error, 1), waitChild: waitChild}
}

// Fatal reports lost ownership of a child. The embedding owner must fence the
// whole process domain rather than continuing to accept work.
func (s *Server) Fatal() <-chan error { return s.fatal }
func (s *Server) fail(err error)      { s.fatalOnce.Do(func() { s.fatal <- err }) }

func (s *Server) OpenSession(resume string) (*Session, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.closed {
		return nil, errors.New("server closed")
	}
	if resume != "" {
		session, ok := s.sessions[resume]
		if !ok {
			return nil, ErrUnknownSession
		}
		if session.attachments != 0 {
			return nil, ErrSessionAttached
		}
		if session.expiry != nil {
			session.expiry.Stop()
			session.expiry = nil
		}
		session.attachments++
		return session, nil
	}
	if len(s.sessions) >= s.limits.MaxSessions {
		return nil, errors.New("session limit reached")
	}
	idb := make([]byte, 16)
	if _, err := rand.Read(idb); err != nil {
		return nil, err
	}
	id := hex.EncodeToString(idb)
	session := &Session{id: id, server: s, processes: make(map[string]*process), files: make(map[string]*os.File), attachments: 1, changed: make(chan struct{})}
	s.sessions[id] = session
	return session, nil
}

// Detach preserves a disconnected session for the configured retention period.
// Each successful OpenSession must have a matching Detach.
func (s *Server) Detach(id string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	session := s.sessions[id]
	if session == nil || session.attachments == 0 {
		return
	}
	session.attachments--
	if session.attachments != 0 {
		return
	}
	session.expiryGeneration++
	generation := session.expiryGeneration
	session.expiry = time.AfterFunc(time.Duration(s.limits.SessionRetentionSeconds)*time.Second, func() { s.expire(id, session, generation) })
}

func (s *Server) expire(id string, session *Session, generation uint64) {
	s.mu.Lock()
	if s.sessions[id] != session || session.attachments != 0 || session.expiryGeneration != generation {
		s.mu.Unlock()
		return
	}
	delete(s.sessions, id)
	s.mu.Unlock()
	session.close()
}

func (s *Server) Session(id string) (*Session, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.closed {
		return nil, errors.New("server closed")
	}
	session := s.sessions[id]
	if session == nil {
		return nil, ErrUnknownSession
	}
	return session, nil
}

// RemoveSession closes a session and its resources. Callers must ensure no active attachment.
func (s *Server) RemoveSession(id string) error {
	s.mu.Lock()
	session := s.sessions[id]
	if session == nil {
		s.mu.Unlock()
		return ErrUnknownSession
	}
	if session.attachments != 0 {
		s.mu.Unlock()
		return errors.New("session has active attachments")
	}
	if session.expiry != nil {
		session.expiry.Stop()
	}
	delete(s.sessions, id)
	s.mu.Unlock()
	session.close()
	return nil
}

func (s *Server) Close() error {
	s.mu.Lock()
	if s.closed {
		s.mu.Unlock()
		return nil
	}
	s.closed = true
	sessions := make([]*Session, 0, len(s.sessions))
	for _, session := range s.sessions {
		if session.expiry != nil {
			session.expiry.Stop()
		}
		sessions = append(sessions, session)
	}
	s.sessions = make(map[string]*Session)
	s.mu.Unlock()
	for _, session := range sessions {
		session.close()
	}
	return nil
}

// ProcessIDs lists processes retained by this session.
func (s *Session) ProcessIDs() []string {
	s.mu.Lock()
	defer s.mu.Unlock()
	ids := make([]string, 0, len(s.processes))
	for id := range s.processes {
		ids = append(ids, id)
	}
	sort.Strings(ids)
	return ids
}
func (s *Session) ID() string { return s.id }

// Changes returns a channel closed at the next process state or output change.
// Subscribe before inspecting state to avoid missing a concurrent change.
func (s *Session) Changes() <-chan struct{} {
	s.changeMu.Lock()
	defer s.changeMu.Unlock()
	return s.changed
}

func (s *Session) signalChange() {
	s.changeMu.Lock()
	close(s.changed)
	s.changed = make(chan struct{})
	s.changeMu.Unlock()
}

func (s *Session) close() {
	s.mu.Lock()
	if s.closed {
		s.mu.Unlock()
		return
	}
	s.closed = true
	ps := make([]*process, 0, len(s.processes))
	for _, p := range s.processes {
		ps = append(ps, p)
	}
	fs := make([]*os.File, 0, len(s.files))
	for _, f := range s.files {
		fs = append(fs, f)
	}
	s.files = make(map[string]*os.File)
	s.mu.Unlock()
	for _, p := range ps {
		p.mu.Lock()
		p.outputDisabled = true
		p.server.outputBytes.Add(-int64(p.bytes))
		p.bytes = 0
		p.chunks = nil
		for _, reader := range p.readers {
			_ = reader.Close()
		}
		p.requestReapLocked()
		p.mu.Unlock()
		p.disposeWrites()
	}
	for _, f := range fs {
		_ = f.Close()
	}
}

func filePath(value string) (string, error) {
	u, err := url.Parse(value)
	if err != nil || u.Scheme != "file" || (u.Host != "" && u.Host != "localhost") || !filepath.IsAbs(u.Path) || u.RawQuery != "" || u.Fragment != "" {
		return "", fmt.Errorf("%w: expected absolute file URI", ErrInvalidRequest)
	}
	return u.Path, nil
}

// openPTY allocates and unlocks a Linux pseudoterminal pair.
func openPTY() (*os.File, *os.File, error) {
	// Explicit nonblocking mode keeps Fd from switching the master to blocking
	// mode for the ioctl calls. Closing it must interrupt blocked reads and writes.
	master, err := os.OpenFile("/dev/ptmx", os.O_RDWR|syscall.O_NOCTTY|syscall.O_NONBLOCK, 0)
	if err != nil {
		return nil, nil, err
	}
	var unlock int32
	if _, _, errno := syscall.Syscall(syscall.SYS_IOCTL, master.Fd(), syscall.TIOCSPTLCK, uintptr(unsafe.Pointer(&unlock))); errno != 0 {
		master.Close()
		return nil, nil, errno
	}
	var number uint32
	if _, _, errno := syscall.Syscall(syscall.SYS_IOCTL, master.Fd(), syscall.TIOCGPTN, uintptr(unsafe.Pointer(&number))); errno != 0 {
		master.Close()
		return nil, nil, errno
	}
	slave, err := os.OpenFile(fmt.Sprintf("/dev/pts/%d", number), os.O_RDWR|syscall.O_NOCTTY, 0)
	if err != nil {
		master.Close()
		return nil, nil, err
	}
	return master, slave, nil
}

func (s *Session) Start(r StartRequest) error {
	if runtime.GOARCH != "amd64" && runtime.GOARCH != "arm64" {
		return errors.New("child wait ownership requires linux amd64 or arm64")
	}
	if r.ProcessID == "" || len(r.Argv) == 0 || r.Argv[0] == "" {
		return fmt.Errorf("%w: process ID and argv required", ErrInvalidRequest)
	}
	dir, err := filePath(r.Cwd)
	if err != nil {
		return err
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.closed {
		return errors.New("session closed")
	}
	if len(s.processes) >= s.server.limits.MaxProcessesPerSession {
		return errors.New("process limit reached")
	}
	if _, ok := s.processes[r.ProcessID]; ok {
		return ErrProcessExists
	}
	if len(r.ProcessID) > 256 {
		return fmt.Errorf("%w: process ID too long", ErrInvalidRequest)
	}
	for {
		n := s.server.processCount.Load()
		if n >= int64(s.server.limits.MaxProcesses) {
			return errors.New("global process limit reached")
		}
		if s.server.processCount.CompareAndSwap(n, n+1) {
			break
		}
	}
	started := false
	defer func() {
		if !started {
			s.server.processCount.Add(-1)
		}
	}()
	executable := r.Argv[0]
	if !strings.ContainsRune(executable, '/') {
		path := "/bin:/usr/bin"
		for _, e := range r.Environment {
			if k, v, ok := strings.Cut(e, "="); ok && k == "PATH" {
				path = v
			}
		}
		found := ""
		for _, part := range filepath.SplitList(path) {
			if part == "" {
				part = "."
			}
			if !filepath.IsAbs(part) {
				part = filepath.Join(dir, part)
			}
			candidate := filepath.Join(part, executable)
			if info, e := os.Stat(candidate); e == nil && info.Mode().IsRegular() && info.Mode().Perm()&0111 != 0 {
				found = candidate
				break
			}
		}
		if found == "" {
			return errors.New("executable not found in child PATH")
		}
		executable = found
	}
	cmd := exec.Command(executable, r.Argv[1:]...)
	cmd.Args[0] = r.Argv[0]
	if r.Arg0 != nil {
		cmd.Args[0] = *r.Arg0
	}
	cmd.Dir = dir
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	cmd.Env = append([]string{}, r.Environment...)
	var stdout, stderr, stdoutWrite, stderrWrite *os.File
	var stdin *os.File
	stream := "stdout"
	if r.TTY {
		stdout, stdoutWrite, err = openPTY()
		if err != nil {
			return err
		}
		cmd.Stdin, cmd.Stdout, cmd.Stderr = stdoutWrite, stdoutWrite, stdoutWrite
		cmd.SysProcAttr = &syscall.SysProcAttr{Setsid: true, Setctty: true, Ctty: 0}
		stream = "pty"
		stdin = stdout
	} else {
		stdout, stdoutWrite, err = os.Pipe()
		if err != nil {
			return err
		}
		stderr, stderrWrite, err = os.Pipe()
		if err != nil {
			stdout.Close()
			stdoutWrite.Close()
			return err
		}
		cmd.Stdout, cmd.Stderr = stdoutWrite, stderrWrite
		if r.PipeStdin {
			var read *os.File
			read, stdin, err = os.Pipe()
			if err != nil {
				stdout.Close()
				stdoutWrite.Close()
				stderr.Close()
				stderrWrite.Close()
				return err
			}
			cmd.Stdin = read
			defer read.Close()
		}
	}
	if stdin != nil {
		// Go may fall back to blocking I/O when registering a file with its
		// poller fails. The writer must be interruptible when we close stdin.
		err = stdin.SetWriteDeadline(time.Time{})
	}
	var managed ManagedCommand
	if err == nil {
		if s.server.launcher == nil {
			err = cmd.Start()
		} else {
			ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
			managed, err = s.server.launcher(ctx, cmd, r.TTY)
			cancel()
			if err == nil && managed == nil {
				err = errors.New("command launcher returned no owner")
			}
		}
	}
	if err != nil {
		stdout.Close()
		stdoutWrite.Close()
		if stderr != nil {
			stderr.Close()
			stderrWrite.Close()
		}
		if stdin != nil {
			stdin.Close()
		}
		return err
	}
	stdoutWrite.Close()
	if stderrWrite != nil {
		stderrWrite.Close()
	}
	p := &process{cmd: cmd, managed: managed, stdin: stdin, changed: make(chan struct{}), next: 1, server: s.server, session: s, id: r.ProcessID}
	if stdin != nil {
		p.writes = make(map[string]struct{})
		p.stdinChanged = make(chan struct{})
		p.stdinDone = make(chan struct{})
		go p.writeStdin()
	}
	p.readers = []*os.File{stdout}
	if stderr != nil {
		p.readers = append(p.readers, stderr)
	}
	started = true
	s.processes[r.ProcessID] = p
	s.signalChange()
	var reads sync.WaitGroup
	reads.Add(1)
	go p.readPipe(stream, stdout, &reads)
	if stderr != nil {
		reads.Add(1)
		go p.readPipe("stderr", stderr, &reads)
	}
	if managed != nil {
		go p.observeManagedExit()
	} else {
		go p.observeExit()
	}
	go func() {
		reads.Wait()
		stdout.Close()
		if stderr != nil {
			stderr.Close()
		}
		p.mu.Lock()
		p.streamsClosed = true
		p.maybeClose()
		p.notify()
		p.mu.Unlock()
	}()
	return nil
}

// childSiginfo matches Linux siginfo_t's SIGCHLD union on amd64 and arm64.
// Other architectures are rejected before process launch.
type childSiginfo struct {
	Signo  int32
	Errno  int32
	Code   int32
	_      int32
	Pid    int32
	Uid    uint32
	Status int32
	_      [100]byte
}

func waitChild(pid int, info *childSiginfo) error {
	return unix.Waitid(unix.P_PID, pid, (*unix.Siginfo)(unsafe.Pointer(info)), unix.WEXITED|unix.WNOWAIT|unix.WNOHANG, nil)
}

// observeExit retains the leader as a zombie until cleanup. This reserves its
// PID and process-group ID while descendants can still exist, and allows all
// group signals to be serialized with the one eventual Cmd.Wait.
func (p *process) observeExit() {
	ticker := time.NewTicker(50 * time.Millisecond)
	defer ticker.Stop()
	for {
		var info childSiginfo
		err := p.server.waitChild(p.cmd.Process.Pid, &info)
		if err == unix.EINTR {
			continue
		}
		if err != nil {
			p.mu.Lock()
			p.ownershipLost = true
			p.mu.Unlock()
			p.server.fail(fmt.Errorf("lost wait ownership of child %d: %w", p.cmd.Process.Pid, err))
			return
		}
		if info.Pid != 0 {
			code := int(info.Status)
			switch info.Code {
			case 1:
			case 2, 3:
				code += 128
			default:
				p.mu.Lock()
				p.ownershipLost = true
				p.mu.Unlock()
				p.server.fail(fmt.Errorf("unexpected waitid child code %d", info.Code))
				return
			}
			p.mu.Lock()
			p.exited = true
			seq := p.next
			p.next++
			p.exitSeq = &seq
			p.exitCode = &code
			p.maybeClose()
			p.notify()
			if p.reapRequested {
				p.reapLocked()
			}
			p.mu.Unlock()
			return
		}
		<-ticker.C
	}
}

// observeManagedExit waits for the external owner instead of attempting waitid on a non-child.
func (p *process) observeManagedExit() {
	code, err := p.managed.Wait(context.Background())
	p.mu.Lock()
	defer p.mu.Unlock()
	if err != nil {
		p.ownershipLost = true
		p.server.fail(fmt.Errorf("lost managed process ownership: %w", err))
		return
	}
	p.exited = true
	seq := p.next
	p.next++
	p.exitSeq = &seq
	p.exitCode = &code
	p.maybeClose()
	p.notify()
	if p.reapRequested {
		p.reapLocked()
	}
}

// requestReapLocked signals only while the unreaped leader still reserves the
// group ID. Reaping happens exactly once, after exit has been observed.
func (p *process) requestReapLocked() {
	if p.reaped || p.reapRequested || p.ownershipLost {
		return
	}
	if p.managed != nil {
		ctx, cancel := context.WithTimeout(context.Background(), 90*time.Second)
		err := p.managed.Release(ctx)
		cancel()
		if err != nil {
			p.ownershipLost = true
			p.server.fail(fmt.Errorf("release managed process: %w", err))
			return
		}
		p.reapRequested = true
		if p.exited {
			p.reapLocked()
		}
		return
	}
	if err := syscall.Kill(-p.cmd.Process.Pid, syscall.SIGKILL); err != nil && !errors.Is(err, syscall.ESRCH) {
		p.server.fail(fmt.Errorf("signal owned process group %d: %w", p.cmd.Process.Pid, err))
		return
	}
	p.reapRequested = true
	if p.exited {
		p.reapLocked()
	}
}

func (p *process) reapLocked() {
	if p.reaped {
		return
	}
	p.reaped = true
	if p.managed != nil {
		p.server.processCount.Add(-1)
		return
	}
	err := p.cmd.Wait()
	p.server.processCount.Add(-1)
	var exitErr *exec.ExitError
	if err != nil && !errors.As(err, &exitErr) {
		p.server.fail(fmt.Errorf("reap child %d: %w", p.cmd.Process.Pid, err))
	}
}

func (p *process) maybeClose() {
	if p.exited && p.streamsClosed && !p.closed {
		p.closed = true
		seq := p.next
		p.next++
		p.closedSeq = &seq
		time.AfterFunc(time.Duration(p.server.limits.ProcessRetentionSeconds)*time.Second, func() { p.session.reclaim(p.id, p) })
	}
}

// reclaim releases a closed process after its retention period.
func (s *Session) reclaim(id string, p *process) {
	s.mu.Lock()
	if s.closed || s.processes[id] != p {
		s.mu.Unlock()
		return
	}
	p.mu.Lock()
	if !p.closed {
		p.mu.Unlock()
		s.mu.Unlock()
		return
	}
	p.requestReapLocked()
	delete(s.processes, id)
	p.outputDisabled = true
	s.server.outputBytes.Add(-int64(p.bytes))
	p.bytes = 0
	p.chunks = nil
	p.mu.Unlock()
	s.mu.Unlock()
	p.disposeWrites()
}

func (p *process) disposeWrites() {
	if p.stdin == nil {
		return
	}
	p.writeMu.Lock()
	p.server.writeCount.Add(-int64(len(p.writes)))
	p.writes = nil
	p.closeStdinQueueLocked()
	p.writeMu.Unlock()
	_ = p.stdin.Close()
	<-p.stdinDone
}

func (p *process) signalStdinChangeLocked() {
	close(p.stdinChanged)
	p.stdinChanged = make(chan struct{})
}

func (p *process) closeStdinQueueLocked() {
	if p.stdinClosed {
		return
	}
	p.stdinClosed = true
	pendingBytes := 0
	for _, data := range p.stdinQueue {
		pendingBytes += len(data)
	}
	p.stdinBytes -= pendingBytes
	p.server.stdinBytes.Add(-int64(pendingBytes))
	p.stdinQueue = nil
	p.signalStdinChangeLocked()
}

// writeStdin is owned by the process. An attachment can disappear after
// admission while this worker is blocked in the kernel delivering its input.
func (p *process) writeStdin() {
	defer close(p.stdinDone)
	for {
		p.writeMu.Lock()
		if p.stdinClosed {
			p.writeMu.Unlock()
			return
		}
		if len(p.stdinQueue) == 0 {
			changed := p.stdinChanged
			p.writeMu.Unlock()
			<-changed
			continue
		}
		data := p.stdinQueue[0]
		p.stdinQueue[0] = nil
		p.stdinQueue = p.stdinQueue[1:]
		p.signalStdinChangeLocked()
		p.writeMu.Unlock()

		_, err := p.stdin.Write(data)

		p.writeMu.Lock()
		p.stdinBytes -= len(data)
		p.server.stdinBytes.Add(-int64(len(data)))
		if err != nil {
			p.closeStdinQueueLocked()
		} else {
			p.signalStdinChangeLocked()
		}
		p.writeMu.Unlock()
	}
}

func (p *process) notify() {
	close(p.changed)
	p.changed = make(chan struct{})
	if p.session != nil {
		p.session.signalChange()
	}
}

func (p *process) readPipe(stream string, r io.Reader, wg *sync.WaitGroup) {
	defer wg.Done()
	limits := p.server.limits
	b := make([]byte, min(16384, limits.MaxOutputBytesPerProcess, limits.MaxOutputBytes, limits.MaxReadBytes))
	for {
		n, err := r.Read(b)
		if n > 0 {
			p.mu.Lock()
			if p.outputDisabled {
				p.mu.Unlock()
				continue
			}
			evict := func() bool {
				if len(p.chunks) == 0 {
					return false
				}
				size := len(p.chunks[0].Data)
				p.chunks[0] = Chunk{}
				p.chunks = p.chunks[1:]
				p.bytes -= size
				p.server.outputBytes.Add(-int64(size))
				return true
			}
			for p.bytes > limits.MaxOutputBytesPerProcess-n && evict() {
			}
			reserved := false
			for {
				total := p.server.outputBytes.Load()
				if int64(n) > int64(limits.MaxOutputBytes)-total {
					if evict() {
						continue
					}
					break
				}
				if p.server.outputBytes.CompareAndSwap(total, total+int64(n)) {
					reserved = true
					break
				}
			}
			if !reserved {
				msg := "global process output buffer exhausted"
				p.failure = &msg
				p.notify()
				p.mu.Unlock()
				continue
			}
			data := append([]byte(nil), b[:n]...)
			p.chunks = append(p.chunks, Chunk{Seq: p.next, Stream: stream, Data: data})
			p.next++
			p.bytes += len(data)

			p.notify()
			p.mu.Unlock()
		}
		if err != nil {
			return
		}
	}
}

func (s *Session) get(id string) (*process, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	p := s.processes[id]
	if s.closed || p == nil {
		return nil, ErrUnknownProcess
	}
	return p, nil
}

func (s *Session) Read(ctx context.Context, id string, after *uint64, maxBytes int, wait time.Duration) (ReadResult, error) {
	p, err := s.get(id)
	if err != nil {
		return ReadResult{}, err
	}
	if maxBytes <= 0 || maxBytes > s.server.limits.MaxReadBytes {
		maxBytes = s.server.limits.MaxReadBytes
	}
	if wait < 0 {
		wait = 0
	}
	timer := time.NewTimer(wait)
	defer timer.Stop()
	for {
		p.mu.Lock()
		result := ReadResult{Chunks: []Chunk{}, Exited: p.exited, Closed: p.closed, ExitCode: p.exitCode, Failure: p.failure, ExitSeq: p.exitSeq, ClosedSeq: p.closedSeq, NextSeq: p.next}
		size := 0
		for _, c := range p.chunks {
			if after != nil && c.Seq <= *after {
				continue
			}
			if size > 0 && len(c.Data) > maxBytes-size {
				break
			}
			data := append([]byte(nil), c.Data...)
			result.Chunks = append(result.Chunks, Chunk{Seq: c.Seq, Stream: c.Stream, Data: data})
			size += len(data)
			result.NextSeq = c.Seq + 1
		}

		ch := p.changed
		p.mu.Unlock()
		hasNewExit := result.ExitSeq != nil && (after == nil || *result.ExitSeq > *after)
		if len(result.Chunks) > 0 || result.Closed || hasNewExit || result.Failure != nil || wait == 0 {
			return result, nil
		}
		select {
		case <-ch:
		case <-timer.C:
			return result, nil
		case <-ctx.Done():
			return ReadResult{}, ctx.Err()
		}
	}
}

// WriteContext accepts stdin into a bounded process-owned queue. A retry with
// the same ID acknowledges a prior admission without delivering its data again.
func (s *Session) WriteContext(ctx context.Context, id, writeID string, data []byte) (string, error) {
	if writeID == "" || len(writeID) > 256 {
		return "", fmt.Errorf("%w: write ID must have 1 to 256 bytes", ErrInvalidRequest)
	}
	p, err := s.get(id)
	if err != nil {
		return "unknownProcess", nil
	}
	if p.stdin == nil {
		return "stdinClosed", nil
	}
	for {
		p.writeMu.Lock()
		if _, ok := p.writes[writeID]; ok {
			p.writeMu.Unlock()
			return "accepted", nil
		}
		if p.stdinClosed {
			p.writeMu.Unlock()
			return "stdinClosed", nil
		}
		if err := ctx.Err(); err != nil {
			p.writeMu.Unlock()
			return "", err
		}
		limits := p.server.limits
		if len(p.writes) >= limits.MaxWritesPerProcess {
			p.writeMu.Unlock()
			return "", errors.New("write limit reached")
		}
		if len(data) > limits.MaxStdinQueueBytesPerProcess || len(data) > limits.MaxStdinQueueBytes {
			p.writeMu.Unlock()
			return "", fmt.Errorf("%w: stdin write exceeds queue byte limit", ErrInvalidRequest)
		}
		if len(p.stdinQueue) < limits.MaxStdinQueueChunksPerProcess && len(data) <= limits.MaxStdinQueueBytesPerProcess-p.stdinBytes {
			err := p.enqueueStdinLocked(writeID, data)
			p.writeMu.Unlock()
			if err != nil {
				return "", err
			}
			return "accepted", nil
		}
		changed := p.stdinChanged
		p.writeMu.Unlock()
		select {
		case <-changed:
		case <-ctx.Done():
			return "", ctx.Err()
		}
	}
}

// enqueueStdinLocked records admission and queues the data as one operation.
func (p *process) enqueueStdinLocked(writeID string, data []byte) error {
	limits := p.server.limits
	for {
		n := p.server.writeCount.Load()
		if n >= int64(limits.MaxWrites) {
			return errors.New("global write ID limit reached")
		}
		if p.server.writeCount.CompareAndSwap(n, n+1) {
			break
		}
	}
	for {
		n := p.server.stdinBytes.Load()
		if int64(len(data)) > int64(limits.MaxStdinQueueBytes)-n {
			p.server.writeCount.Add(-1)
			return errors.New("global stdin queue byte limit reached")
		}
		if p.server.stdinBytes.CompareAndSwap(n, n+int64(len(data))) {
			break
		}
	}
	p.writes[writeID] = struct{}{}
	p.stdinQueue = append(p.stdinQueue, append([]byte(nil), data...))
	p.stdinBytes += len(data)
	p.signalStdinChangeLocked()
	return nil
}

func (s *Session) Signal(id string) error {
	p, err := s.get(id)
	if err != nil {
		return nil
	}
	p.mu.Lock()
	defer p.mu.Unlock()
	if p.exited || p.reaped || p.reapRequested || p.ownershipLost {
		return nil
	}
	if p.managed != nil {
		ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		return p.managed.Signal(ctx, syscall.SIGINT)
	}
	return syscall.Kill(-p.cmd.Process.Pid, syscall.SIGINT)
}

func (s *Session) Terminate(id string) (bool, error) {
	p, err := s.get(id)
	if err != nil {
		return false, nil
	}
	p.mu.Lock()
	defer p.mu.Unlock()
	if p.exited || p.reaped || p.reapRequested || p.ownershipLost {
		return false, nil
	}
	if p.managed != nil {
		ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		return true, p.managed.Signal(ctx, syscall.SIGKILL)
	}
	return true, syscall.Kill(-p.cmd.Process.Pid, syscall.SIGKILL)
}

func (s *Session) Metadata(path string) (FileMetadata, error) {
	path, err := filePath(path)
	if err != nil {
		return FileMetadata{}, err
	}
	info, err := os.Lstat(path)
	if err != nil {
		return FileMetadata{}, err
	}
	return FileMetadata{IsDirectory: info.IsDir(), IsFile: info.Mode().IsRegular(), IsSymlink: info.Mode()&os.ModeSymlink != 0, Size: uint64(info.Size())}, nil
}

func (s *Session) OpenFile(id, path string) error {
	if id == "" {
		return errors.New("file handle required")
	}
	path, err := filePath(path)
	if err != nil {
		return err
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.closed {
		return errors.New("session closed")
	}
	if len(s.files) >= s.server.limits.MaxFilesPerSession {
		return errors.New("file handle limit reached")
	}
	if s.files[id] != nil {
		return errors.New("file handle exists")
	}
	f, err := os.Open(path)
	if err != nil {
		return err
	}
	s.files[id] = f
	return nil
}

func (s *Session) ReadFile(id string, offset int64, length int) ([]byte, bool, error) {
	if offset < 0 || length < 0 || length > s.server.limits.MaxReadBytes {
		return nil, false, fmt.Errorf("%w: invalid file range", ErrInvalidRequest)
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	f := s.files[id]
	if f == nil {
		return nil, false, errors.New("file handle not found")
	}
	b := make([]byte, length)
	n, err := f.ReadAt(b, offset)
	if err != nil && !errors.Is(err, io.EOF) {
		return nil, false, err
	}
	return b[:n], errors.Is(err, io.EOF), nil
}

func (s *Session) CloseFile(id string) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	f := s.files[id]
	if f == nil {
		return errors.New("file handle not found")
	}
	delete(s.files, id)
	return f.Close()
}
