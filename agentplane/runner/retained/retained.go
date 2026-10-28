package retained

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"sync"
	"syscall"

	runnerpb "github.com/agentydragon/ducktape/agentplane/proto/runner"
	"github.com/google/uuid"
)

var sessionIDPattern = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$`)

// ownershipError means another runner holds the retained state directory or the
// filesystem could not establish the ownership fence.
type ownershipError struct{ Err error }

func (e *ownershipError) Error() string { return "runner state ownership: " + e.Err.Error() }
func (e *ownershipError) Unwrap() error { return e.Err }

// Owner holds the exclusive, lifetime writer lock for a runner state directory.
// File is read-only so a native child may inherit the lock without gaining write access.
type Owner struct {
	file *os.File
}

func OpenOwner(root string) (*Owner, error) {
	if err := EnsureDirectory(root); err != nil {
		return nil, err
	}
	file, err := os.OpenFile(filepath.Join(root, ".agentplane-runner-owner"), os.O_RDONLY|os.O_CREATE, 0o600)
	if err != nil {
		return nil, &ownershipError{Err: fmt.Errorf("cannot fence runner state directory %s: %w", root, err)}
	}
	if err := syscall.Flock(int(file.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); err != nil {
		_ = file.Close()
		if errors.Is(err, syscall.EWOULDBLOCK) || errors.Is(err, syscall.EAGAIN) {
			return nil, &ownershipError{Err: fmt.Errorf("runner state directory %s is already owned", root)}
		}
		return nil, &ownershipError{Err: fmt.Errorf("cannot fence runner state directory %s: %w", root, err)}
	}
	return &Owner{file: file}, nil
}

// File returns the locked descriptor for inheritance by native child processes.
func (o *Owner) File() *os.File { return o.file }

func (o *Owner) Close() error {
	if o.file == nil {
		return nil
	}
	file := o.file
	o.file = nil
	unlockErr := syscall.Flock(int(file.Fd()), syscall.LOCK_UN)
	closeErr := file.Close()
	return errors.Join(unlockErr, closeErr)
}

// EnsureDirectory durably creates each missing directory entry, syncing the parent after
// each mkdir so a crash cannot leave a session record pointing into a lost directory.
func EnsureDirectory(path string) error {
	abs, err := filepath.Abs(path)
	if err != nil {
		return err
	}
	var missing []string
	parent := filepath.Clean(abs)
	for {
		_, err := os.Stat(parent)
		if err == nil {
			break
		}
		if !errors.Is(err, os.ErrNotExist) {
			return err
		}
		missing = append(missing, parent)
		next := filepath.Dir(parent)
		if next == parent {
			return fmt.Errorf("no existing parent for %s", path)
		}
		parent = next
	}
	// Also fence a prior attempt that left the target visible but failed its parent fsync.
	if err := syncDirectory(filepath.Dir(parent)); err != nil {
		return err
	}
	for i := len(missing) - 1; i >= 0; i-- {
		if err := os.Mkdir(missing[i], 0o777); err != nil && !errors.Is(err, os.ErrExist) {
			return err
		}
		if err := syncDirectory(filepath.Dir(missing[i])); err != nil {
			return err
		}
	}
	return nil
}

func syncDirectory(path string) error {
	file, err := os.Open(path)
	if err != nil {
		return err
	}
	defer file.Close()
	return file.Sync()
}

// WriteDurable atomically replaces path with data, syncing the file and parent directory.
func WriteDurable(path string, data []byte) error {
	file, err := os.OpenFile(path+".tmp", os.O_CREATE|os.O_TRUNC|os.O_WRONLY, 0o600)
	if err != nil {
		return err
	}
	_, writeErr := file.Write(data)
	syncErr := file.Sync()
	closeErr := file.Close()
	if err := errors.Join(writeErr, syncErr, closeErr); err != nil {
		return err
	}
	if err := os.Rename(path+".tmp", path); err != nil {
		return err
	}
	return syncDirectory(filepath.Dir(path))
}

// Record is the durable session.json format. Its JSON names and string-valued
// harness enum match the Python runner's persisted records.
type Record struct {
	Harness                  runnerpb.Harness
	CWD                      string
	Model                    string
	TotalContextBudgetTokens *int
	ReasoningEffort          string
	Instructions             string
	SetupScript              string
	// SetupScriptSHA256 is retained only for records written by older Runner versions.
	SetupScriptSHA256 *string
	EventSourceID     string
	NativeSessionID   *string
}

type sessionRecordJSON struct {
	Harness                  string  `json:"harness"`
	CWD                      string  `json:"cwd"`
	Model                    string  `json:"model"`
	TotalContextBudgetTokens *int    `json:"total_context_budget_tokens"`
	ReasoningEffort          string  `json:"reasoning_effort"`
	Instructions             string  `json:"instructions"`
	SetupScript              string  `json:"setup_script,omitempty"`
	SetupScriptSHA256        *string `json:"setup_script_sha256"`
	EventSourceID            string  `json:"event_source_id"`
	NativeSessionID          *string `json:"native_session_id"`
}

func (r Record) MarshalJSON() ([]byte, error) {
	name, ok := runnerpb.Harness_name[int32(r.Harness)]
	if !ok || (r.Harness != runnerpb.Harness_HARNESS_CLAUDE && r.Harness != runnerpb.Harness_HARNESS_CODEX) {
		return nil, fmt.Errorf("invalid persisted harness value %d", r.Harness)
	}
	setupScriptSHA256 := r.SetupScriptSHA256
	if r.SetupScript != "" {
		// TODO: Remove the compatibility digest after the old-Runner rollback window closes.
		value := setupScriptDigest(r.SetupScript)
		setupScriptSHA256 = &value
	}
	return json.Marshal(sessionRecordJSON{
		Harness: name, CWD: r.CWD, Model: r.Model, TotalContextBudgetTokens: r.TotalContextBudgetTokens,
		ReasoningEffort: r.ReasoningEffort, Instructions: r.Instructions, SetupScript: r.SetupScript,
		SetupScriptSHA256: setupScriptSHA256,
		EventSourceID:     r.EventSourceID, NativeSessionID: r.NativeSessionID,
	})
}

func (r *Record) UnmarshalJSON(data []byte) error {
	var wire sessionRecordJSON
	if err := json.Unmarshal(data, &wire); err != nil {
		return err
	}
	harness, ok := runnerpb.Harness_value[wire.Harness]
	if !ok || (harness != int32(runnerpb.Harness_HARNESS_CLAUDE) && harness != int32(runnerpb.Harness_HARNESS_CODEX)) {
		return fmt.Errorf("invalid persisted harness %q", wire.Harness)
	}
	legacySetupScriptSHA256 := wire.SetupScriptSHA256
	if wire.SetupScript != "" {
		// New records carry source as authority. The digest is serialized only for
		// rollback compatibility with Runner versions that understand hash-only records.
		legacySetupScriptSHA256 = nil
	}
	*r = Record{
		Harness: runnerpb.Harness(harness), CWD: wire.CWD, Model: wire.Model,
		TotalContextBudgetTokens: wire.TotalContextBudgetTokens, ReasoningEffort: wire.ReasoningEffort,
		Instructions: wire.Instructions, SetupScript: wire.SetupScript, SetupScriptSHA256: legacySetupScriptSHA256,
		EventSourceID: wire.EventSourceID, NativeSessionID: wire.NativeSessionID,
	}
	return nil
}

func NewRecord(spec *runnerpb.SessionSpec, setupScript string, totalContextBudgetTokens *int) (Record, error) {
	if spec == nil {
		return Record{}, errors.New("session spec is required")
	}
	if totalContextBudgetTokens != nil && *totalContextBudgetTokens <= 0 {
		return Record{}, errors.New("total context budget must be positive")
	}
	record := Record{
		Harness: spec.GetHarness(), CWD: spec.GetCwd(), Model: spec.GetModel(),
		TotalContextBudgetTokens: totalContextBudgetTokens, ReasoningEffort: spec.GetReasoningEffort(),
		Instructions: spec.GetInstructions(), SetupScript: setupScript, EventSourceID: uuid.NewString(),
	}
	return record, nil
}

// MatchesSetupScript compares a supplied script against the retained source, or
// against the legacy digest when reading a hash-only record. Callers should skip
// this check when the OpenSession request omits setup_script.
func (r Record) MatchesSetupScript(script string) bool {
	if r.SetupScript != "" {
		return r.SetupScript == script
	}
	if r.SetupScriptSHA256 != nil {
		return *r.SetupScriptSHA256 == setupScriptDigest(script)
	}
	return script == ""
}

// HasSetupScript reports whether the source or a legacy setup identity was retained.
func (r Record) HasSetupScript() bool { return r.SetupScript != "" || r.SetupScriptSHA256 != nil }

func setupScriptDigest(script string) string {
	digest := sha256.Sum256([]byte(script))
	return hex.EncodeToString(digest[:])
}

func (r Record) Spec() *runnerpb.SessionSpec {
	return &runnerpb.SessionSpec{
		Harness: r.Harness, Cwd: r.CWD, Model: r.Model,
		ReasoningEffort: r.ReasoningEffort, Instructions: r.Instructions,
	}
}

type Acknowledgement struct {
	Cursor uint64 `json:"cursor"`
	Sealed bool   `json:"sealed"`
}

// Store owns session JSON files and the runner-wide lameduck marker.
type Store struct {
	mu             sync.Mutex
	root           string
	incarnationUID string
	incarnationSet bool
}

func NewStore(root string) (*Store, error) {
	if err := EnsureDirectory(root); err != nil {
		return nil, err
	}
	return &Store{root: root}, nil
}

func ValidateSessionID(sessionID string) (string, error) {
	if !sessionIDPattern.MatchString(sessionID) {
		return "", fmt.Errorf("invalid session ID %q: expected [A-Za-z0-9][A-Za-z0-9._-]{0,127}", sessionID)
	}
	return sessionID, nil
}

func (s *Store) Directory(sessionID string) (string, error) {
	if _, err := ValidateSessionID(sessionID); err != nil {
		return "", err
	}
	return filepath.Join(s.root, sessionID), nil
}

func (s *Store) SessionIDs() ([]string, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	entries, err := os.ReadDir(s.root)
	if err != nil {
		return nil, err
	}
	ids := make([]string, 0, len(entries))
	for _, entry := range entries {
		if !entry.IsDir() {
			continue
		}
		info, err := os.Stat(filepath.Join(s.root, entry.Name(), "session.json"))
		if err == nil && info.Mode().IsRegular() {
			ids = append(ids, entry.Name())
		} else if err != nil && !errors.Is(err, os.ErrNotExist) {
			return nil, err
		}
	}
	sort.Strings(ids)
	return ids, nil
}

func (s *Store) Lameduck() bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	info, err := os.Stat(filepath.Join(s.root, "sealed"))
	return err == nil && info.Mode().IsRegular()
}

func (s *Store) IncarnationUID() (string, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.incarnationUID, s.incarnationSet
}

func (s *Store) RequireIncarnationUID() (string, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.requireIncarnationUIDLocked()
}

func (s *Store) requireIncarnationUIDLocked() (string, error) {
	if !s.incarnationSet {
		return "", errors.New("the runner cannot quiesce before the gateway names its incarnation UID")
	}
	return s.incarnationUID, nil
}

func (s *Store) AdoptIncarnation(incarnationUID string) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if incarnationUID == "" {
		return errors.New("runner incarnation UID is required")
	}
	s.incarnationUID, s.incarnationSet = incarnationUID, true
	marker := filepath.Join(s.root, "sealed")
	data, err := os.ReadFile(marker)
	if errors.Is(err, os.ErrNotExist) {
		return nil
	}
	if err != nil {
		return err
	}
	if string(data) == incarnationUID {
		return nil
	}
	if err := os.Remove(marker); err != nil {
		return err
	}
	return syncDirectory(s.root)
}

func (s *Store) EnterLameduck() error {
	s.mu.Lock()
	defer s.mu.Unlock()
	incarnationUID, err := s.requireIncarnationUIDLocked()
	if err != nil {
		return err
	}
	marker := filepath.Join(s.root, "sealed")
	info, err := os.Stat(marker)
	if err == nil && info.Mode().IsRegular() {
		return nil
	}
	if err != nil && !errors.Is(err, os.ErrNotExist) {
		return err
	}
	temporary := filepath.Join(s.root, "sealed.tmp")
	file, err := os.OpenFile(temporary, os.O_CREATE|os.O_WRONLY|os.O_TRUNC, 0o666)
	if err != nil {
		return err
	}
	_, writeErr := io.WriteString(file, incarnationUID)
	syncErr := file.Sync()
	closeErr := file.Close()
	if err := errors.Join(writeErr, syncErr, closeErr); err != nil {
		return err
	}
	if err := os.Rename(temporary, marker); err != nil {
		return err
	}
	return syncDirectory(s.root)
}

func (s *Store) Exists(sessionID string) (bool, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	directory, err := s.Directory(sessionID)
	if err != nil {
		return false, err
	}
	info, err := os.Stat(filepath.Join(directory, "session.json"))
	if errors.Is(err, os.ErrNotExist) {
		return false, nil
	}
	if err != nil {
		return false, err
	}
	return info.Mode().IsRegular(), nil
}

func (s *Store) Read(sessionID string) (Record, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	directory, err := s.Directory(sessionID)
	if err != nil {
		return Record{}, err
	}
	data, err := os.ReadFile(filepath.Join(directory, "session.json"))
	if err != nil {
		return Record{}, err
	}
	var record Record
	if err := json.Unmarshal(data, &record); err != nil {
		return Record{}, err
	}
	if record.TotalContextBudgetTokens != nil && *record.TotalContextBudgetTokens <= 0 {
		return Record{}, errors.New("total context budget must be positive")
	}
	return record, nil
}

func (s *Store) Write(sessionID string, record Record) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.replace(sessionID, "session.json", record)
}

func (s *Store) ReadAcknowledgement(sessionID string) (*Acknowledgement, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	directory, err := s.Directory(sessionID)
	if err != nil {
		return nil, err
	}
	data, err := os.ReadFile(filepath.Join(directory, "acknowledgement.json"))
	if errors.Is(err, os.ErrNotExist) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	var acknowledgement Acknowledgement
	if err := json.Unmarshal(data, &acknowledgement); err != nil {
		return nil, err
	}
	return &acknowledgement, nil
}

func (s *Store) WriteAcknowledgement(sessionID string, acknowledgement Acknowledgement) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.replace(sessionID, "acknowledgement.json", acknowledgement)
}

func (s *Store) replace(sessionID, name string, value any) error {
	directory, err := s.Directory(sessionID)
	if err != nil {
		return err
	}
	if err := EnsureDirectory(directory); err != nil {
		return err
	}
	data, err := json.MarshalIndent(value, "", "  ")
	if err != nil {
		return err
	}
	temporary := filepath.Join(directory, name+".tmp")
	file, err := os.OpenFile(temporary, os.O_CREATE|os.O_WRONLY|os.O_TRUNC, 0o666)
	if err != nil {
		return err
	}
	_, writeErr := file.Write(data)
	syncErr := file.Sync()
	closeErr := file.Close()
	if err := errors.Join(writeErr, syncErr, closeErr); err != nil {
		return err
	}
	if err := os.Rename(temporary, filepath.Join(directory, name)); err != nil {
		return err
	}
	return syncDirectory(directory)
}
