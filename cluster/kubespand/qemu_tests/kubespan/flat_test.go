package kubespan_test

import (
	"testing"

	h "github.com/agentydragon/ducktape/cluster/kubespand/qemu_tests"
)

func TestTopology(t *testing.T) {
	t.Parallel()
	for _, topology := range []string{"flat", "cross_subnet"} {
		for _, wt := range []h.WorkerType{h.WorkerTypeKubespand, h.WorkerTypeTalos} {
			t.Run(topology+"/"+string(wt), func(t *testing.T) {
				t.Parallel()
				runTopology(t, topology, wt)
			})
		}
	}
}
