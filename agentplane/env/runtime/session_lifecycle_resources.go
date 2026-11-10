package runtime

import (
	lifecycle "github.com/agentydragon/ducktape/agentplane/env/runtime/lifecycle"
	"github.com/cosi-project/runtime/pkg/resource"
)

const (
	sessionLifecycleControllerName = "environment-session-lifecycle"
)

type environmentAdmissionCountSpec struct {
	Admissions         int
	AcceptedGeneration uint64
}

func (environmentAdmissionCountSpec) ResourceType() resource.Type {
	return lifecycle.AdmissionCountType
}
func (value environmentAdmissionCountSpec) DeepCopy() environmentAdmissionCountSpec { return value }
