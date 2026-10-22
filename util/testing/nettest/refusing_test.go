package nettest

import (
	"errors"
	"net"
	"syscall"
	"testing"
	"time"
)

func TestRefusingAddressStaysUnavailableToConnectionsAndBinders(t *testing.T) {
	address := RefusingAddress(t)
	connection, err := net.DialTimeout("tcp", address, time.Second)
	if connection != nil {
		connection.Close()
		t.Fatal("refusing endpoint accepted a connection")
	}
	if !errors.Is(err, syscall.ECONNREFUSED) {
		t.Fatalf("connect: %v", err)
	}
	listener, err := net.Listen("tcp", address)
	if listener != nil {
		listener.Close()
		t.Fatal("another listener acquired the refusing endpoint")
	}
	if !errors.Is(err, syscall.EADDRINUSE) {
		t.Fatalf("competing bind: %v", err)
	}
}
