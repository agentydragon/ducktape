// Command export_python emits the Python roster consumed by cdk8s.
package main

import (
	"fmt"
	"strings"

	"github.com/agentydragon/ducktape/agentplane/action_service/sandbox/contract"
)

func main() {
	fmt.Println("# Code generated from agentplane/action_service/sandbox/contract; DO NOT EDIT.")
	fmt.Println("from enum import StrEnum")
	fmt.Printf("\nSANDBOX_GROUP = %q\n", contract.GroupName)
	fmt.Println("\nclass SandboxAction(StrEnum):")
	for _, action := range contract.Roster() {
		fmt.Printf("    %s = %q\n", strings.ToUpper(string(action)), action)
	}
}
