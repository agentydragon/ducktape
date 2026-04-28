package main

import (
	"flag"
	"fmt"
	"log"
	"net/http"
	"os"
)

var (
	addrFlag = flag.String("addr", "127.0.0.1:8080", "listen address")
)

func main() {
	flag.Parse()

	fmt.Fprintf(os.Stderr, "Starting server on %s\n", *addrFlag)

	mux := http.NewServeMux()

	// Register handlers
	mux.HandleFunc("/v1/register", handleRegister)
	mux.HandleFunc("/v1/note/put", handleNotePut)
	mux.HandleFunc("/v1/note/get", handleNoteGet)
	mux.HandleFunc("/v1/note/list", handleNoteList)

	server := &http.Server{
		Addr:    *addrFlag,
		Handler: mux,
	}

	log.Fatal(server.ListenAndServe())
}

func handleRegister(w http.ResponseWriter, r *http.Request) {
	w.WriteHeader(http.StatusOK)
	fmt.Fprintf(w, "ok")
}

func handleNotePut(w http.ResponseWriter, r *http.Request) {
	w.WriteHeader(http.StatusOK)
	fmt.Fprintf(w, "ok")
}

func handleNoteGet(w http.ResponseWriter, r *http.Request) {
	w.WriteHeader(http.StatusOK)
	fmt.Fprintf(w, "ok")
}

func handleNoteList(w http.ResponseWriter, r *http.Request) {
	w.WriteHeader(http.StatusOK)
	fmt.Fprintf(w, "ok")
}
