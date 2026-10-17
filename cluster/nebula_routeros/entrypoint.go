// The RouterOS container runs Nebula and a narrow TCP proxy for switch management.
// The proxy listens on the Nebula TUN address and opens new connections to the
// switch over its private veth; it never forwards peer packets into the home LAN.
package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net"
	"os"
	"os/exec"
	"os/signal"
	"strconv"
	"syscall"
	"time"
)

const (
	nebulaInterface = "nebula1"
	proxyConfigPath = "/etc/nebula/proxy.json"
)

var managementPorts = []uint16{22, 443, 8729, 8291}

type proxyConfig struct {
	RouterOSAddress string `json:"routeros_address"`
}

func main() {
	if err := run(); err != nil {
		log.Printf("nebula-routeros: %v", err)
		os.Exit(1)
	}
}

func run() error {
	if len(os.Args) < 2 {
		return errors.New("usage: nebula-routeros-entrypoint <nebula-command> [args...]")
	}

	configData, err := os.ReadFile(proxyConfigPath)
	if err != nil {
		return fmt.Errorf("read proxy config: %w", err)
	}
	var config proxyConfig
	if err := json.Unmarshal(configData, &config); err != nil {
		return fmt.Errorf("parse proxy config: %w", err)
	}
	if address := net.ParseIP(config.RouterOSAddress); address == nil || address.To4() == nil {
		return fmt.Errorf("routeros_address must be an IPv4 address, got %q", config.RouterOSAddress)
	}

	nebula := exec.Command(os.Args[1], os.Args[2:]...)
	nebula.Stdout = os.Stdout
	nebula.Stderr = os.Stderr
	if err := nebula.Start(); err != nil {
		return fmt.Errorf("start Nebula: %w", err)
	}
	nebulaDone := make(chan error, 1)
	go func() { nebulaDone <- nebula.Wait() }()

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	proxyDone := make(chan error, 1)
	go func() { proxyDone <- serveManagementProxy(ctx, config.RouterOSAddress) }()

	signals := make(chan os.Signal, 1)
	signal.Notify(signals, syscall.SIGINT, syscall.SIGTERM)
	defer signal.Stop(signals)

	select {
	case err := <-nebulaDone:
		cancel()
		if err != nil {
			return fmt.Errorf("Nebula exited: %w", err)
		}
		return nil
	case sig := <-signals:
		cancel()
		if err := nebula.Process.Signal(sig); err != nil && !errors.Is(err, os.ErrProcessDone) {
			return fmt.Errorf("forward %s to Nebula: %w", sig, err)
		}
		select {
		case <-nebulaDone:
			return nil
		case <-time.After(10 * time.Second):
			_ = nebula.Process.Kill()
			<-nebulaDone
			return nil
		}
	case err := <-proxyDone:
		cancel()
		_ = nebula.Process.Signal(syscall.SIGTERM)
		<-nebulaDone
		if err != nil {
			return fmt.Errorf("management proxy stopped: %w", err)
		}
		return errors.New("management proxy stopped unexpectedly")
	}
}

func serveManagementProxy(ctx context.Context, targetIP string) error {
	nebulaIP, err := waitForNebulaIPv4(ctx)
	if err != nil {
		return err
	}

	listeners := make([]*net.TCPListener, 0, len(managementPorts))
	for _, port := range managementPorts {
		address := &net.TCPAddr{IP: nebulaIP, Port: int(port)}
		listener, err := net.ListenTCP("tcp4", address)
		if err != nil {
			for _, opened := range listeners {
				_ = opened.Close()
			}
			return fmt.Errorf("listen on %s: %w", address, err)
		}
		listeners = append(listeners, listener)
		log.Printf("proxying %s to %s", address, net.JoinHostPort(targetIP, strconv.Itoa(int(port))))
	}

	listenerErrors := make(chan error, len(listeners))
	for _, listener := range listeners {
		go acceptConnections(ctx, listener, targetIP, listenerErrors)
	}
	select {
	case <-ctx.Done():
		for _, listener := range listeners {
			_ = listener.Close()
		}
		return nil
	case err := <-listenerErrors:
		for _, listener := range listeners {
			_ = listener.Close()
		}
		return err
	}
}

func waitForNebulaIPv4(ctx context.Context) (net.IP, error) {
	ticker := time.NewTicker(250 * time.Millisecond)
	defer ticker.Stop()
	timeout := time.NewTimer(5 * time.Minute)
	defer timeout.Stop()

	for {
		iface, err := net.InterfaceByName(nebulaInterface)
		if err == nil {
			addresses, err := iface.Addrs()
			if err == nil {
				for _, address := range addresses {
					var ip net.IP
					switch value := address.(type) {
					case *net.IPNet:
						ip = value.IP
					case *net.IPAddr:
						ip = value.IP
					}
					if ipv4 := ip.To4(); ipv4 != nil {
						return ipv4, nil
					}
				}
			}
		}

		select {
		case <-ctx.Done():
			return nil, ctx.Err()
		case <-timeout.C:
			return nil, fmt.Errorf("%s did not acquire an IPv4 address within five minutes", nebulaInterface)
		case <-ticker.C:
		}
	}
}

func acceptConnections(ctx context.Context, listener *net.TCPListener, targetIP string, errors chan<- error) {
	for {
		connection, err := listener.AcceptTCP()
		if err != nil {
			if ctx.Err() == nil {
				errors <- fmt.Errorf("accept on %s: %w", listener.Addr(), err)
			}
			return
		}
		go proxyConnection(connection, targetIP, listener.Addr().(*net.TCPAddr).Port)
	}
}

func proxyConnection(client *net.TCPConn, targetIP string, port int) {
	defer client.Close()
	address := net.JoinHostPort(targetIP, strconv.Itoa(port))
	backend, err := net.DialTimeout("tcp4", address, 5*time.Second)
	if err != nil {
		log.Printf("connect to RouterOS at %s: %v", address, err)
		return
	}
	defer backend.Close()
	_ = client.SetKeepAlive(true)
	_ = backend.(*net.TCPConn).SetKeepAlive(true)

	copied := make(chan struct{}, 2)
	go func() {
		_, _ = io.Copy(backend, client)
		_ = backend.(*net.TCPConn).CloseWrite()
		copied <- struct{}{}
	}()
	go func() {
		_, _ = io.Copy(client, backend)
		_ = client.CloseWrite()
		copied <- struct{}{}
	}()
	<-copied
	<-copied
}
