package sandboxcontroller

import (
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	"k8s.io/apimachinery/pkg/api/meta"
)

// sandboxRunning reports whether the controller runs the Sandbox: `spec.running` holds and the Sandbox is not
// idle-suspended at the current wake generation.
func sandboxRunning(s *sandboxapi.Sandbox) bool {
	idle := s.Status.SuspendedBy != nil && *s.Status.SuspendedBy == sandboxapi.SuspendedByIdle &&
		s.Status.SuspendedAtWakeGeneration != nil && *s.Status.SuspendedAtWakeGeneration == s.Spec.WakeGeneration
	return s.Spec.Running && !idle
}

func sandboxOperatingMode(s *sandboxapi.Sandbox) sandboxapi.OperatingMode {
	if sandboxRunning(s) {
		return sandboxapi.OperatingModeRunning
	}
	return sandboxapi.OperatingModeSuspended
}

// sessionsQuiesced reports whether every Session was sealed.
func sandboxSessionsQuiesced(s *sandboxapi.Sandbox) bool {
	return meta.IsStatusConditionTrue(s.Status.Conditions, Quiesced)
}

// quiescedIncarnation reports whether uid is the incarnation whose runner sealed every Session.
func sandboxQuiescedIncarnation(s *sandboxapi.Sandbox, uid string) bool {
	return s.Status.QuiescedIncarnation != nil && *s.Status.QuiescedIncarnation == uid
}

// quiescing reports whether the controller has begun quiescing the current incarnation's runner. A
// runner that has entered lameduck refuses every Session for good, so a Sandbox resumed meanwhile
// still waits for the quiesce to finish and replaces the incarnation, as for one resumed after it.
func sandboxQuiescing(s *sandboxapi.Sandbox) bool {
	running := meta.FindStatusCondition(s.Status.Conditions, Running)
	return running != nil && running.Reason == "Quiescing"
}
