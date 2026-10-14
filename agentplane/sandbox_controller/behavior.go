package sandboxcontroller

import "k8s.io/apimachinery/pkg/api/meta"

// running reports whether the controller runs the Sandbox: `spec.running` holds and the Sandbox is not
// idle-suspended at the current wake generation.
func (s *Sandbox) running() bool {
	idle := s.Status.SuspendedBy != nil && *s.Status.SuspendedBy == SuspendedByIdle &&
		s.Status.SuspendedAtWakeGeneration != nil && *s.Status.SuspendedAtWakeGeneration == s.Spec.WakeGeneration
	return s.Spec.Running && !idle
}

func (s *Sandbox) operatingMode() OperatingMode {
	if s.running() {
		return OperatingModeRunning
	}
	return OperatingModeSuspended
}

// sessionsQuiesced reports whether every Session was sealed.
func (s *Sandbox) sessionsQuiesced() bool {
	return meta.IsStatusConditionTrue(s.Status.Conditions, Quiesced)
}

// quiescedIncarnation reports whether uid is the incarnation whose runner sealed every Session.
func (s *Sandbox) quiescedIncarnation(uid string) bool {
	return s.Status.QuiescedIncarnation != nil && *s.Status.QuiescedIncarnation == uid
}

// quiescing reports whether the controller has begun quiescing the current incarnation's runner. A
// runner that has entered lameduck refuses every Session for good, so a Sandbox resumed meanwhile
// still waits for the quiesce to finish and replaces the incarnation, as for one resumed after it.
func (s *Sandbox) quiescing() bool {
	running := meta.FindStatusCondition(s.Status.Conditions, Running)
	return running != nil && running.Reason == "Quiescing"
}
