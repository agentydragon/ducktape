package session

import (
	"context"
	"errors"

	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	"go.uber.org/zap"
)

// The retained reservation admits setup before lifecycle reconciliation. Keep
// the claim for the lifetime of this owner, even if setup cannot journal an outcome.
func (s *Session) advanceSetup() error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.setupDone == nil && s.setup == sessionpb.SetupState_SETUP_STATE_RUNNING {
		script := string(s.record.SetupScript)
		done := make(chan struct{})
		s.setupDone = done
		s.work.Go(func() {
			defer close(done)
			if err := s.runSetup(s.deps.ctx, script); err != nil {
				s.deps.fail(err)
				s.mu.Lock()
				s.setupErr = err
				// Without a committed outcome, the setup snapshot may be unchanged.
				publishErr := s.deps.signalOperation(s.id)
				s.mu.Unlock()
				s.deps.fail(publishErr)
			}
			s.wake()
		})
	}
	err := s.setupErr
	s.setupErr = nil
	return err
}

func (s *Session) runSetup(ctx context.Context, script string) error {
	err := s.deps.waitBootstrap(ctx)
	if err == nil {
		err = s.Emit(ctx, observation(&eventpb.SetupStarted{}), nil)
	}
	if err == nil {
		cwd, e := s.deps.ensureWorkspace(ctx, s.Session().Cwd)
		err = e
		if err == nil {
			var code int
			code, err = s.deps.scripts.Run(ctx, ControllerName+"/"+s.id+"/setup", script, cwd, func(stdout bool, data []byte) error {
				out := &eventpb.SetupOutput{}
				if stdout {
					out.Stream = &eventpb.SetupOutput_Stdout{Stdout: data}
				} else {
					out.Stream = &eventpb.SetupOutput_Stderr{Stderr: data}
				}
				return s.Emit(ctx, observation(out), nil)
			})
			if err == nil {
				err = s.Emit(ctx, observation(&eventpb.SetupFinished{ExitCode: int32(code)}), nil)
			}
		}
	}
	if err != nil {
		zap.L().Error("session setup interrupted", zap.String("session", s.id), zap.Error(err))
		if outcomeErr := s.Emit(ctx, observation(&eventpb.SetupInterrupted{}), nil); outcomeErr != nil {
			return errors.Join(err, outcomeErr)
		}
	}
	return nil
}
