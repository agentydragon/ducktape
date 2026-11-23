package coordinator

import (
	"context"
	"errors"
	"fmt"
	"sync"

	"github.com/agentydragon/ducktape/agentplane/env/runtime/internal/cosistate"
	"github.com/cosi-project/runtime/pkg/state"
)

// EnvironmentState keeps the Gateway-bound incarnation in memory and restores the
// sealed incarnation UID from COSI. Persistence errors block identity transitions until
// Runtime restarts and reloads COSI's durable state.
type EnvironmentState struct {
	mu                 sync.Mutex
	state              state.State
	incarnationUID     string
	incarnationSet     bool
	sealedUID          string
	sealed             bool
	sealPersistenceErr error
}

func NewEnvironmentState(lifecycleState state.State) (*EnvironmentState, error) {
	seal, found, err := cosistate.ReadEnvironmentSeal(context.Background(), lifecycleState)
	if err != nil {
		return nil, err
	}
	environment := &EnvironmentState{state: lifecycleState}
	if found {
		environment.sealedUID, environment.sealed = seal.IncarnationUID, true
	}
	return environment, nil
}

// Lameduck reports that a seal exists or seal persistence is uncertain; both states fail closed.
func (s *EnvironmentState) Lameduck() bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.sealed || s.sealPersistenceErr != nil
}

func (s *EnvironmentState) SealedUID() (string, bool, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.sealPersistenceErr != nil {
		return "", false, s.uncertainSealErrorLocked()
	}
	if !s.sealed {
		return "", false, nil
	}
	return s.sealedUID, true, nil
}

func (s *EnvironmentState) uncertainSealErrorLocked() error {
	return fmt.Errorf("environment seal persistence outcome is uncertain; restart Runtime to reload COSI state: %w", s.sealPersistenceErr)
}

func (s *EnvironmentState) IncarnationUID() (string, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.incarnationUID, s.incarnationSet
}

func (s *EnvironmentState) RequireIncarnationUID() (string, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.sealPersistenceErr != nil {
		return "", s.uncertainSealErrorLocked()
	}
	if !s.incarnationSet {
		return "", errors.New("the Environment Runtime cannot quiesce before the Gateway names its incarnation UID")
	}
	return s.incarnationUID, nil
}

func (s *EnvironmentState) AdoptIncarnation(incarnationUID string) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if incarnationUID == "" {
		return errors.New("Environment Runtime incarnation UID is required")
	}
	if s.sealPersistenceErr != nil {
		return s.uncertainSealErrorLocked()
	}
	if s.incarnationSet && s.incarnationUID == incarnationUID && (!s.sealed || s.sealedUID == incarnationUID) {
		return nil
	}
	if s.sealed && s.sealedUID != incarnationUID {
		if err := cosistate.DeleteEnvironmentSeal(context.Background(), s.state, s.sealedUID); err != nil {
			s.sealPersistenceErr = err
			return s.uncertainSealErrorLocked()
		}
		s.sealedUID, s.sealed = "", false
	}
	s.incarnationUID, s.incarnationSet = incarnationUID, true
	return nil
}

func (s *EnvironmentState) EnterLameduck() error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.sealPersistenceErr != nil {
		return s.uncertainSealErrorLocked()
	}
	if !s.incarnationSet {
		return errors.New("the Environment Runtime cannot quiesce before the Gateway names its incarnation UID")
	}
	if s.sealed {
		return nil
	}
	if err := cosistate.WriteEnvironmentSeal(context.Background(), s.state, cosistate.EnvironmentSeal{IncarnationUID: s.incarnationUID}); err != nil {
		s.sealPersistenceErr = err
		return s.uncertainSealErrorLocked()
	}
	s.sealedUID, s.sealed = s.incarnationUID, true
	return nil
}
