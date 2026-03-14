// Binary init is the PID-1 process for nft-smoke test VMs.
// Runs nftables smoke test levels and emits structured events.
package main

import (
	"strconv"
	"strings"

	qemu_tests "github.com/agentydragon/ducktape/cluster/kubespand/qemu_tests"
	"github.com/agentydragon/ducktape/cluster/kubespand/qemu_tests/vms/initlib"
)

func main() {
	initlib.InitBasic()
	params := initlib.ParseCmdline()
	if v, ok := params["role"]; ok {
		initlib.Role = v
	}

	initlib.LoadNftablesModules()

	levels := params["levels"]
	if levels == "" {
		initlib.EmitEvent(qemu_tests.Event{Type: qemu_tests.EventError, Message: "no levels= on kernel cmdline", Error: "missing levels parameter"})
		initlib.Poweroff()
	}

	initlib.EmitEvent(qemu_tests.Event{Type: qemu_tests.EventBoot, Message: "nft_smoke mode, levels=" + levels})

	// Enable EBUSY retry for all nftables operations (QEMU TCG is slow).
	ebusyRetry = true

	anyFail := false
	for _, level := range strings.Split(levels, ",") {
		level = strings.TrimSpace(level)
		if level == "" {
			continue
		}
		levelNum, err := strconv.Atoi(level)
		if err != nil {
			initlib.EmitEvent(qemu_tests.Event{Type: qemu_tests.EventError, Message: "invalid level " + strconv.Quote(level), Error: err.Error()})
			anyFail = true
			continue
		}
		exitCode := runNftSmokeLevel(levelNum)
		success := exitCode == 0
		if !success {
			anyFail = true
		}
		initlib.EmitEvent(qemu_tests.Event{Type: qemu_tests.EventProbe, Message: "level " + level, Target: "nft-smoke-" + level, Success: &success})
	}

	if anyFail {
		initlib.EmitEvent(qemu_tests.Event{Type: qemu_tests.EventDone, Message: "some levels failed", Error: "not all nft-smoke levels passed"})
	} else {
		initlib.EmitEvent(qemu_tests.Event{Type: qemu_tests.EventDone, Message: "all levels passed"})
	}
	initlib.Poweroff()
}
