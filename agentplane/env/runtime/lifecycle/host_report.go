package lifecycle

import (
	runtimepb "github.com/agentydragon/ducktape/agentplane/env/runtime/runtimepb"
	"github.com/cosi-project/runtime/pkg/resource"
	"github.com/cosi-project/runtime/pkg/resource/meta/spec"
	"github.com/cosi-project/runtime/pkg/resource/typed"
	"google.golang.org/protobuf/proto"
)

const HostReportType = "HostReports.agentplane"

// HostReport is the Host-owned observation for one exact intent, including the
// same-generation Execution fence. The report's binding alone does not carry it.
type HostReport struct {
	Intent *runtimepb.LifecycleIntent
	Report *runtimepb.HostReport
}

func (v HostReport) DeepCopy() HostReport {
	if v.Intent != nil {
		v.Intent = proto.Clone(v.Intent).(*runtimepb.LifecycleIntent)
	}
	if v.Report != nil {
		v.Report = proto.Clone(v.Report).(*runtimepb.HostReport)
	}
	return v
}

type hostReportExtension struct{}

func (hostReportExtension) ResourceDefinition() spec.ResourceDefinitionSpec {
	return spec.ResourceDefinitionSpec{Type: HostReportType, DefaultNamespace: Namespace}
}

func HostReportResource(v HostReport) *typed.Resource[HostReport, hostReportExtension] {
	return typed.NewResource[HostReport, hostReportExtension](resource.NewMetadata(Namespace, HostReportType, "host", resource.VersionUndefined), v)
}
