package runtime

import (
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"sync"
)

const sealedMarkerName = "sealed"

// EnvironmentSnapshot is the environment identity the Runtime publishes to its Host.
// Quiesced is backed by the legacy durable sealed marker so a Runtime restart cannot reopen
// a Sandbox that was already quiesced.
type EnvironmentSnapshot struct {
	IncarnationUID string
	Known          bool
	Quiesced       bool
}

// EnvironmentState owns environment identity and the durable quiesce marker. Session files
// remain in the existing StateDir/sessions directory and are owned by the Harness Host.
type EnvironmentState struct {
	mu             sync.Mutex
	root           string
	incarnationUID string
	incarnationSet bool
}

func NewEnvironmentState(stateDir string) (*EnvironmentState, error) {
	root := filepath.Join(stateDir, "sessions")
	if err := ensureDirectory(root); err != nil {
		return nil, err
	}
	return &EnvironmentState{root: root}, nil
}

func (s *EnvironmentState) Snapshot() EnvironmentSnapshot {
	s.mu.Lock()
	defer s.mu.Unlock()
	return EnvironmentSnapshot{
		IncarnationUID: s.incarnationUID,
		Known:          s.incarnationSet,
		Quiesced:       s.lameduckLocked(),
	}
}

func (s *EnvironmentState) Lameduck() bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.lameduckLocked()
}

// SealedUID returns the durable legacy marker payload when the current state is sealed.
// Runtime uses it to finish the old incarnation's exact flush before adopting a successor.
func (s *EnvironmentState) SealedUID() (string, bool, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	marker := filepath.Join(s.root, sealedMarkerName)
	info, err := os.Stat(marker)
	if errors.Is(err, os.ErrNotExist) {
		return "", false, nil
	}
	if err != nil {
		return "", false, err
	}
	if !info.Mode().IsRegular() {
		return "", false, nil
	}
	data, err := os.ReadFile(marker)
	if err != nil {
		return "", false, err
	}
	if len(data) == 0 {
		return "", false, errors.New("legacy sealed marker has an empty incarnation UID")
	}
	return string(data), true, nil
}

func (s *EnvironmentState) lameduckLocked() bool {
	info, err := os.Stat(filepath.Join(s.root, sealedMarkerName))
	return err == nil && info.Mode().IsRegular()
}

func (s *EnvironmentState) IncarnationUID() (string, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.incarnationUID, s.incarnationSet
}

func (s *EnvironmentState) RequireIncarnationUID() (string, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if !s.incarnationSet {
		return "", errors.New("the runner cannot quiesce before the gateway names its incarnation UID")
	}
	return s.incarnationUID, nil
}

// AdoptIncarnation clears a legacy seal only when the Gateway names a new workload
// incarnation. The on-disk filename and UID payload match Runner's existing format.
func (s *EnvironmentState) AdoptIncarnation(incarnationUID string) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if incarnationUID == "" {
		return errors.New("runner incarnation UID is required")
	}
	s.incarnationUID, s.incarnationSet = incarnationUID, true
	marker := filepath.Join(s.root, sealedMarkerName)
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

func (s *EnvironmentState) EnterLameduck() error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if !s.incarnationSet {
		return errors.New("the runner cannot quiesce before the gateway names its incarnation UID")
	}
	marker := filepath.Join(s.root, sealedMarkerName)
	info, err := os.Stat(marker)
	if err == nil && info.Mode().IsRegular() {
		return nil
	}
	if err != nil && !errors.Is(err, os.ErrNotExist) {
		return err
	}
	temporary := filepath.Join(s.root, sealedMarkerName+".tmp")
	file, err := os.OpenFile(temporary, os.O_CREATE|os.O_WRONLY|os.O_TRUNC, 0o666)
	if err != nil {
		return err
	}
	_, writeErr := io.WriteString(file, s.incarnationUID)
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

func syncDirectory(path string) error {
	file, err := os.Open(path)
	if err != nil {
		return fmt.Errorf("open directory %s for sync: %w", path, err)
	}
	defer file.Close()
	return file.Sync()
}

// ensureDirectory syncs each parent after creating its child, matching the retained
// state directory durability contract without making Runtime depend on Host-owned storage.
func ensureDirectory(path string) error {
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
