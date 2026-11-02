package lifecycle

import (
	"context"

	runtimepb "github.com/agentydragon/ducktape/agentplane/env/runtime/runtimepb"
	"github.com/cosi-project/runtime/pkg/controller"
	"github.com/cosi-project/runtime/pkg/resource"
	"github.com/cosi-project/runtime/pkg/resource/meta/spec"
	"github.com/cosi-project/runtime/pkg/resource/typed"
	"github.com/cosi-project/runtime/pkg/safe"
	"google.golang.org/protobuf/proto"
)

const HostReportType = "HostReports.agentplane"

// HostReport is the Host-owned observation for one exact intent, including the
// same-generation Execution fence. Intent is the report's only binding.
type HostReport struct {
	Intent *runtimepb.LifecycleIntent
	Report *runtimepb.HostReport
}

// Equal lets COSI suppress semantically unchanged protobuf reports even when
// their in-memory representations differ (for example, nil and empty lists).
func (v *HostReport) Equal(other any) bool {
	value, ok := other.(*HostReport)
	return ok && proto.Equal(v.Intent, value.Intent) && proto.Equal(v.Report, value.Report)
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

func WriteHostReport(ctx context.Context, writer controller.Writer, value HostReport) error {
	return safe.WriterModify(ctx, writer, HostReportResource(value), func(current *typed.Resource[HostReport, hostReportExtension]) error {
		*current.TypedSpec() = value
		return nil
	})
}
