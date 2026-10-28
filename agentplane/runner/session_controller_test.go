package runner

import (
	"reflect"
	"strconv"
	"testing"

	"github.com/siderolabs/gen/optional"
)

func TestSessionControllerUsesConfiguredReconcileConcurrency(t *testing.T) {
	for _, concurrency := range []int{1000, 2048} {
		t.Run(strconv.Itoa(concurrency), func(t *testing.T) {
			c := &sessionLifecycleController{l: &lifecycle{runner: &Runner{config: Config{
				SessionReconcileConcurrency: concurrency,
			}}}}
			settings := c.Settings()
			want := optional.Some(uint(concurrency))
			if !reflect.DeepEqual(settings.Concurrency, want) {
				t.Fatalf("Session controller concurrency = %#v, want %#v", settings.Concurrency, want)
			}
		})
	}
}
