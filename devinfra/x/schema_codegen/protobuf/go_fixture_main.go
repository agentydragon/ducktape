package main

import (
	"fmt"
	"os"

	"github.com/agentydragon/ducktape/devinfra/x/schema_codegen/protobuf/producer"
)

func main() {
	payload, err := producer.ProtoJSON("go")
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	fmt.Println(string(payload))
}
