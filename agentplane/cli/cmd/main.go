package main

import (
	"os"

	"github.com/agentydragon/ducktape/agentplane/cli"
)

func main() { os.Exit(cli.Execute(os.Args[1:], os.Getenv, os.Stdin, os.Stdout, os.Stderr)) }
