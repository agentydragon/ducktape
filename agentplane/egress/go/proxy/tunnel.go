package proxy

import (
	"bufio"
	"crypto"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"errors"
	"math/big"
	"net"
	"net/netip"
	"sync"
	"time"
)

// tlsRecordHandshake is the first byte of a TLS ClientHello.
const tlsRecordHandshake = 0x16

// readBufferedConn serves bytes the CONNECT parser already read ahead before the live connection.
type readBufferedConn struct {
	net.Conn
	reader *bufio.Reader
}

func (c *readBufferedConn) Read(p []byte) (int, error) { return c.reader.Read(p) }

// closeNotifyConn reports Close so the single-connection listener can end its server.
type closeNotifyConn struct {
	net.Conn
	closed chan struct{}
	once   sync.Once
}

func (c *closeNotifyConn) Close() error {
	c.once.Do(func() { close(c.closed) })
	return c.Conn.Close()
}

// singleConnListener accepts one connection, then blocks until that connection closes or the
// listener does, so the http.Server serving it ends with the connection.
type singleConnListener struct {
	conn   net.Conn
	closed <-chan struct{}
	once   sync.Once

	done      chan struct{}
	closeOnce sync.Once
}

func newSingleConnListener(conn net.Conn, closed <-chan struct{}) *singleConnListener {
	return &singleConnListener{conn: conn, closed: closed, done: make(chan struct{})}
}

func (l *singleConnListener) Accept() (net.Conn, error) {
	var conn net.Conn
	l.once.Do(func() { conn = l.conn })
	if conn != nil {
		return conn, nil
	}
	select {
	case <-l.closed:
	case <-l.done:
	}
	return nil, net.ErrClosed
}

func (l *singleConnListener) Close() error {
	l.closeOnce.Do(func() { close(l.done) })
	return nil
}

func (l *singleConnListener) Addr() net.Addr { return l.conn.LocalAddr() }

// leafIssuer signs a short-lived interception certificate per handshake. One leaf key
// is reused and nothing is cached, so client-chosen names cannot grow memory.
type leafIssuer struct {
	ca     *x509.Certificate
	signer crypto.Signer
	key    *ecdsa.PrivateKey
}

func newLeafIssuer(ca tls.Certificate) (*leafIssuer, error) {
	parsed, err := x509.ParseCertificate(ca.Certificate[0])
	if err != nil {
		return nil, err
	}
	signer, ok := ca.PrivateKey.(crypto.Signer)
	if !ok {
		return nil, errors.New("interception CA key cannot sign")
	}
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		return nil, err
	}
	return &leafIssuer{ca: parsed, signer: signer, key: key}, nil
}

func (i *leafIssuer) issue(name string) (*tls.Certificate, error) {
	serial, err := rand.Int(rand.Reader, new(big.Int).Lsh(big.NewInt(1), 127))
	if err != nil {
		return nil, err
	}
	now := time.Now()
	template := &x509.Certificate{
		SerialNumber: serial, NotBefore: now.Add(-time.Minute), NotAfter: now.Add(time.Hour),
		KeyUsage: x509.KeyUsageDigitalSignature, ExtKeyUsage: []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth}, BasicConstraintsValid: true,
	}
	if i.ca.NotAfter.Before(template.NotAfter) {
		template.NotAfter = i.ca.NotAfter
	}
	if address, err := netip.ParseAddr(name); err == nil {
		template.IPAddresses = []net.IP{address.AsSlice()}
	} else {
		template.DNSNames = []string{name}
	}
	der, err := x509.CreateCertificate(rand.Reader, template, i.ca, i.key.Public(), i.signer)
	if err != nil {
		return nil, err
	}
	return &tls.Certificate{Certificate: [][]byte{der}, PrivateKey: i.key}, nil
}
