package runner

import (
	"context"
	"errors"

	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	"go.uber.org/zap"
)

// runSetup performs one claimed script operation. The Session controller owns
// whether it runs; cancellation of an admission or preparation cannot cancel it.
func (s *Session) runSetup(ctx context.Context, script string) error {
	err := s.runner.bootstrap(ctx)
	if err == nil {
		err = s.Emit(ctx, observation(&eventpb.SetupStarted{}), nil)
	}
	if err == nil {
		cwd, e := s.runner.ensureWorkspace(ctx, s.Session().Cwd)
		err = e
		if err == nil {
			var code int
			code, err = s.runner.scripts.Run(ctx, sessionControllerName+"/"+s.id+"/setup", script, cwd, func(stdout bool, data []byte) error {
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
