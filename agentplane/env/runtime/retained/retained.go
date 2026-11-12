package retained

import (
	"context"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"sync"

	persistencepb "github.com/agentydragon/ducktape/agentplane/env/runtime/cosistate/persistentpb"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/lifecycle"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	"github.com/cosi-project/runtime/pkg/safe"
	"github.com/cosi-project/runtime/pkg/state"
	"github.com/google/uuid"
	"google.golang.org/protobuf/proto"
)

var sessionIDPattern = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$`)

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
		return fmt.Errorf("open directory %s for sync: %w", path, err)
	}
	defer file.Close()
	return file.Sync()
}

// Record is the durable Session reservation and native recovery identity.
// The generated protobuf spec is the sole in-memory and on-disk representation.
type Record = persistencepb.SessionRecordSpec

func NewRecord(spec *sessionpb.SessionSpec, setupScript string, totalContextBudgetTokens *int) (*Record, error) {
	if spec == nil || spec.GetPlacement().GetEnvironmentHosted() == nil {
		return nil, errors.New("native session placement is required")
	}
	if totalContextBudgetTokens != nil && *totalContextBudgetTokens <= 0 {
		return nil, errors.New("total context budget must be positive")
	}
	record := &Record{
		Placement: proto.Clone(spec.GetPlacement().GetEnvironmentHosted()).(*sessionpb.EnvironmentHostedPlacement), Cwd: spec.GetCwd(), InitialModel: spec.GetModel(),
		InitialReasoningEffort: spec.GetReasoningEffort(), Instructions: spec.GetInstructions(),
		SetupScript: []byte(setupScript), EventSourceId: uuid.NewString(),
	}
	if totalContextBudgetTokens != nil {
		budget := int64(*totalContextBudgetTokens)
		record.TotalContextBudgetTokens = &budget
	}
	return record, nil
}

// MatchesSetupScript compares a supplied script against the retained source.
// Callers should skip this check when the CreateSession request omits setup_script.
func MatchesSetupScript(record *Record, script string) bool {
	return string(record.SetupScript) == script
}

// HasSetupScript reports whether a setup source was retained.
func HasSetupScript(record *Record) bool { return len(record.SetupScript) != 0 }

func ReservationSpec(record *Record) *sessionpb.SessionSpec {
	return &sessionpb.SessionSpec{
		Placement: &sessionpb.SessionPlacement{Kind: &sessionpb.SessionPlacement_EnvironmentHosted{EnvironmentHosted: proto.Clone(record.Placement).(*sessionpb.EnvironmentHostedPlacement)}}, Cwd: record.Cwd, Model: record.InitialModel,
		ReasoningEffort: record.InitialReasoningEffort, Instructions: record.Instructions,
	}
}

type Acknowledgement struct {
	Cursor uint64
	Sealed bool
}

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
	}, state.WithUpdateOwner(lifecycle.EnvironmentOwner))
}
