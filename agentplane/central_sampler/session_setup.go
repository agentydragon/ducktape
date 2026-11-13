package centralsampler

import (
	"context"
	"errors"
	"fmt"
	"time"

	samplerpb "github.com/agentydragon/ducktape/agentplane/central_sampler/samplerpb"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	"google.golang.org/protobuf/proto"
)

func (r *Runtime) setup(ctx context.Context, sessionID string, owner Owner) (bool, error) {
	session, err := r.store.GetSession(ctx, sessionID, "")
	if err != nil {
		return false, err
	}
	switch session.SetupState {
	case sessionpb.SetupState_SETUP_STATE_NOT_REQUIRED, sessionpb.SetupState_SETUP_STATE_SUCCEEDED:
		return true, nil
	case sessionpb.SetupState_SETUP_STATE_FAILED, sessionpb.SetupState_SETUP_STATE_INTERRUPTED:
		return false, nil
	case sessionpb.SetupState_SETUP_STATE_RUNNING:
	default:
		return false, fmt.Errorf("unknown setup state %s", session.SetupState)
	}
	started, err := r.store.StartSetup(ctx, owner, sessionID)
	if err != nil || !started {
		return false, err
	}
	configuration := &samplerpb.CreateSessionRequest{}
	if err := proto.Unmarshal(session.Configuration, configuration); err != nil {
		return false, err
	}
	setupCtx, cancel := context.WithTimeout(ctx, time.Duration(r.settings.TurnTimeoutSeconds)*time.Second)
	defer cancel()
	executor, closeTransport, err := r.newExecutor(setupCtx, r.store, owner, session, r.settings)
	if err != nil {
		if ctx.Err() == nil {
			finishCtx, finishCancel := context.WithTimeout(context.Background(), time.Duration(r.settings.RPCTimeoutSeconds)*time.Second)
			finishErr := r.store.FinishSetup(finishCtx, owner, sessionID, nil)
			finishCancel()
			return false, errors.Join(err, finishErr)
		}
		return false, err
	}
	defer closeTransport()
	exitCode, err := executor.Setup(setupCtx, configuration.GetSetupScript(), func(stream string, data []byte) error {
		return r.store.SetupOutput(setupCtx, owner, sessionID, stream, data)
	})
	if err != nil {
		if ctx.Err() != nil {
			return false, err
		}
		finishCtx, finishCancel := context.WithTimeout(context.Background(), time.Duration(r.settings.RPCTimeoutSeconds)*time.Second)
		finishErr := r.store.FinishSetup(finishCtx, owner, sessionID, nil)
		finishCancel()
		return false, errors.Join(err, finishErr)
	}
	finishCtx, finishCancel := context.WithTimeout(context.Background(), time.Duration(r.settings.RPCTimeoutSeconds)*time.Second)
	finishErr := r.store.FinishSetup(finishCtx, owner, sessionID, &exitCode)
	finishCancel()
	if finishErr != nil {
		return false, finishErr
	}
	return exitCode == 0, nil
}
