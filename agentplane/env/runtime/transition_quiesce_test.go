package runtime

import (
	"context"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/env/runtime/lifecycle"
	runtimepb "github.com/agentydragon/ducktape/agentplane/env/runtime/runtimepb"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
)

func TestQuiesceStatusRetryRequiresExactCurrentHostGeneration(t *testing.T) {
	binding := &runtimepb.Binding{Generation: 7}
	for _, test := range []struct {
		name          string
		desired       lifecycle.Desired
		desiredFound  bool
		progress      lifecycle.Status
		progressFound bool
		want          bool
	}{
		{name: "current error", desired: lifecycle.Desired{Generation: 7}, desiredFound: true, progress: lifecycle.Status{Generation: 7, Error: "failed"}, progressFound: true, want: true},
		{name: "current healthy", desired: lifecycle.Desired{Generation: 7}, desiredFound: true, progress: lifecycle.Status{Generation: 7}, progressFound: true},
		{name: "missing desired", progress: lifecycle.Status{Generation: 7, Error: "failed"}, progressFound: true},
		{name: "missing progress", desired: lifecycle.Desired{Generation: 7}, desiredFound: true},
		{name: "stale progress error", desired: lifecycle.Desired{Generation: 7}, desiredFound: true, progress: lifecycle.Status{Generation: 6, Error: "failed"}, progressFound: true},
		{name: "newer desired and progress", desired: lifecycle.Desired{Generation: 8}, desiredFound: true, progress: lifecycle.Status{Generation: 8, Error: "failed"}, progressFound: true},
	} {
		t.Run(test.name, func(t *testing.T) {
			got := quiesceStatusNeedsRetry(binding, test.desired, test.desiredFound, test.progress, test.progressFound)
			if got != test.want {
				t.Fatalf("retry = %t, want %t", got, test.want)
			}
		})
	}
}

func TestQuiesceStatusBarrierRejectsStaleHostBinding(t *testing.T) {
	proxy := modelConfigServer(t)
	r := newLifecycleRunner(t, lifecycleConfig(t, proxy.URL, t.TempDir(), nil, "/bin/false"))
	r.mu.Lock()
	host := r.host
	binding := proto.Clone(r.intent.Binding).(*runtimepb.Binding)
	r.mu.Unlock()
	binding.Generation--
	c := &runtimeTransitionSequence{r: r, quiesce: quiescePlan{host: host, binding: binding, currentReport: true}}
	if _, err := c.runQuiesceBarrier(context.Background(), quiesceCheckHostStatus); status.Code(err) != codes.Unavailable {
		t.Fatalf("stale Host status binding = %v, want unavailable", err)
	}
}
