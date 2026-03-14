package nft_test

import (
	"fmt"
	"testing"
	"time"

	h "github.com/agentydragon/ducktape/cluster/kubespand/qemu_tests"
)

func TestNftSmoke(t *testing.T) {
	vmlinuz := h.RunfilePath(t, h.VmlinuzPath)
	initramfs := h.RunfilePath(t, h.NftInitramfs)
	out := h.OutputDir(t)

	levels := "1,2,3,4,5,6"
	v := h.BootVM(t, "nft-smoke", vmlinuz, initramfs,
		fmt.Sprintf("mode=nft_smoke levels=%s", levels))

	if !h.WaitVMDone(t, v, 120*time.Second) {
		v.SaveLogs(t, out)
		t.FailNow()
	}

	v.SaveLogs(t, out)

	events := v.GetEvents()
	for _, e := range events {
		if e.Type == h.EventProbe && e.Success != nil && !*e.Success {
			t.Errorf("nft-smoke probe failed: %s (target=%s)", e.Message, e.Target)
		}
	}

	var foundDone bool
	for _, e := range events {
		if e.Type == h.EventDone {
			foundDone = true
			if e.Error != "" {
				t.Errorf("nft-smoke done with error: %s", e.Error)
			}
		}
	}
	if !foundDone {
		t.Error("no done event received from VM")
	}
}
