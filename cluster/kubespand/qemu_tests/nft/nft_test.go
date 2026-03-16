package nft_test

import (
	"fmt"
	"strings"
	"testing"
	"time"

	h "github.com/agentydragon/ducktape/cluster/kubespand/qemu_tests"
)

func TestNftSmoke(t *testing.T) {
	sw := h.NewStopwatch(t)

	vmlinuz := h.RunfilePath(t, h.VmlinuzPath)
	initramfs := h.RunfilePath(t, h.NftInitramfs)
	out := h.OutputDir(t)
	sw.Lap("resolve runfiles")

	levels := "1,2,3,4,5,6"
	v := h.BootVM(t, "nft-smoke", vmlinuz, initramfs,
		fmt.Sprintf("mode=nft_smoke levels=%s", levels), nil)
	sw.Lap("boot VM")

	if !h.WaitVMDone(t, v, 120*time.Second) {
		v.SaveLogs(t, out)
		sw.Lap("VM timeout")
		sw.Summary(out)
		t.FailNow()
	}
	sw.Lap("VM done")

	v.SaveLogs(t, out)

	// Parse structured result lines from the VM's raw log.
	// The nft init writes lines like: NFT_RESULT level=1 pass
	rawLog := v.GetRawLog()
	var foundResult bool
	for _, line := range strings.Split(rawLog, "\n") {
		if strings.HasPrefix(line, "NFT_RESULT ") {
			foundResult = true
			if strings.HasSuffix(line, " fail") {
				t.Errorf("nft-smoke: %s", line)
			}
		}
		if strings.HasPrefix(line, "NFT_DONE fail") {
			t.Errorf("nft-smoke done with failures")
		}
	}
	if !foundResult {
		t.Error("no NFT_RESULT lines found in VM log")
	}
	sw.Lap("assertions")

	sw.Summary(out)
}
