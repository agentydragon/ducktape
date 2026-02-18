package main

import (
	"flag"
	"fmt"
	"log"
	"time"

	"github.com/agentydragon/worthy/ibdock"
)

var login = flag.String("login", "", "IB login to test")
var password = flag.String("password", "", "IB password to test")

func main() {
	flag.Parse()
	if *login == "" || *password == "" {
		panic("login and password are required")
	}

	dock, err := ibdock.StartNew(*login, *password, log.Default())
	if err != nil {
		panic(err)
	}
	fmt.Println("Will RunExec in 10 s.")
	time.Sleep(10 * time.Second)
	fmt.Println("RunExec")
	if _, err = dock.RunExec(); err != nil {
		panic(err)
	}
	time.Sleep(30 * time.Second)
	// TODO(prvak): Now here we should run the Python thing to get the
	// stocks out of there.
	fmt.Println("Killing.")
	dock.Kill()
}
