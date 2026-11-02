package retained

import (
	"context"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"sync"
	"syscall"

	"github.com/agentydragon/ducktape/agentplane/env/runtime/lifecycle"
	delegationpb "github.com/agentydragon/ducktape/agentplane/proto/delegation"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	"github.com/cosi-project/runtime/pkg/safe"
	"github.com/cosi-project/runtime/pkg/state"
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
	file, err := os.OpenFile(filepath.Join(root, ".agentplane-harness-host-owner"), os.O_RDONLY|os.O_CREATE, 0o600)
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

// Record is the durable Session reservation and native recovery identity.
type Record struct {
	Harness                  sessionpb.Harness
	CWD                      string
	Model                    string
	TotalContextBudgetTokens *int
	ReasoningEffort          string
	Instructions             string
	SetupScript              string
	EventSourceID            string
	NativeSessionID          *string
	ExecutionPrincipal       *delegationpb.ServiceAccountIdentity
}

func NewRecord(spec *sessionpb.SessionSpec, setupScript string, totalContextBudgetTokens *int) (Record, error) {
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

// MatchesSetupScript compares a supplied script against the retained source.
// Callers should skip this check when the OpenSession request omits setup_script.
func (r Record) MatchesSetupScript(script string) bool {
	return r.SetupScript == script
}

// HasSetupScript reports whether a setup source was retained.
func (r Record) HasSetupScript() bool { return r.SetupScript != "" }

func (r Record) Spec() *sessionpb.SessionSpec {
	return &sessionpb.SessionSpec{
		Harness: r.Harness, Cwd: r.CWD, Model: r.Model,
		ReasoningEffort: r.ReasoningEffort, Instructions: r.Instructions,
	}
}

type Acknowledgement struct {
	Cursor uint64
	Sealed bool
}

func (acknowledgement Acknowledgement) DeepCopy() Acknowledgement { return acknowledgement }

// Store owns Session directories and the shared COSI state for Session records
// and ingress acknowledgements.
type Store struct {
	mu    sync.Mutex
	root  string
	state state.State
}

func NewStore(root string, resources state.State) (*Store, error) {
	if err := EnsureDirectory(root); err != nil {
		return nil, err
	}
	return &Store{root: root, state: resources}, nil
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

func (s *Store) ReadAcknowledgement(sessionID string) (*Acknowledgement, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.state == nil {
		return nil, errors.New("Session COSI state is required")
	}
	acknowledgement, found, err := ReadAcknowledgement(context.Background(), s.state, sessionID)
	if err != nil {
		return nil, err
	}
	if !found {
		return nil, nil
	}
	return &acknowledgement, nil
}

func (s *Store) WriteAcknowledgement(sessionID string, acknowledgement Acknowledgement) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.state == nil {
		return errors.New("Session COSI state is required")
	}
	if _, err := ValidateSessionID(sessionID); err != nil {
		return err
	}
	return safe.StateModify(context.Background(), s.state, AcknowledgementResource(sessionID, acknowledgement), func(current *IngressAckResource) error {
		current.TypedSpec().Value.Cursor = acknowledgement.Cursor
		current.TypedSpec().Value.Sealed = acknowledgement.Sealed
		return nil
	}, state.WithUpdateOwner(lifecycle.HostOwner))
}
