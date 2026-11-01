// Command agentplane is the agent-facing client of the Action, Session Dispatcher, Notification and Egress rules APIs.
package main

import "os"

func main() {
	os.Exit(newApp(os.Getenv, os.Stdin, os.Stdout, os.Stderr).execute(os.Args[1:]))
}
