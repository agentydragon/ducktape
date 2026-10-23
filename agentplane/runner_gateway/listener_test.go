package main

import (
	"context"
	"net"
	"strconv"
	"syscall"
	"testing"
)

func TestListenAndServeClosesGRPCListenerWhenHTTPListenerFails(t *testing.T) {
	original, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer original.Close()
	address := original.Addr().String()
	file, err := original.(*net.TCPListener).File()
	if err != nil {
		t.Fatal(err)
	}
	fd, err := syscall.Dup(int(file.Fd()))
	file.Close()
	if err != nil {
		t.Fatal(err)
	}
	original.Close()
	c := config{ListenAddress: "fd:" + strconv.Itoa(fd), HTTPListenAddress: "fd:not-a-number"}
	if err := listenAndServe(context.Background(), c); err == nil {
		t.Fatal("invalid HTTP listener did not fail startup")
	}
	rebound, err := net.Listen("tcp", address)
	if err != nil {
		t.Fatalf("gRPC listener leaked after startup failed: %v", err)
	}
	rebound.Close()
}
