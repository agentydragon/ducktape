// Binary init is the PID-1 process for nft-smoke test VMs.
// Runs nftables smoke test levels and emits structured log lines.
package main

import (
	"fmt"
	"log"
	"strconv"
	"strings"

	"github.com/agentydragon/ducktape/cluster/kubespand/qemu_tests/vms/initlib"
)

func main() {
	params := initlib.Init()

	initlib.LoadNftablesModules()

	levels := params["levels"]
	if levels == "" {
		log.Printf("FATAL: no levels= on kernel cmdline")
		initlib.Poweroff()
	}

	log.Printf("nft_smoke mode, levels=%s", levels)

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
			log.Printf("invalid level %s: %v", strconv.Quote(level), err)
			anyFail = true
			continue
		}
		exitCode := runNftSmokeLevel(levelNum)
		success := exitCode == 0
		if !success {
			anyFail = true
		}
		// Structured result line parsed by the test host.
		if success {
			fmt.Printf("NFT_RESULT level=%s pass\n", level)
		} else {
			fmt.Printf("NFT_RESULT level=%s fail\n", level)
		}
	}

	if anyFail {
		fmt.Println("NFT_DONE fail")
	} else {
		fmt.Println("NFT_DONE pass")
	}
	initlib.Poweroff()
}
