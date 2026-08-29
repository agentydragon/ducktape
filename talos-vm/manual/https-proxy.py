#!/usr/bin/env python3
"""
HTTP CONNECT proxy that forwards to an upstream authenticated proxy.
Used to allow QEMU VM (without auth) to connect through the environment's authenticated proxy.
"""
import socket
import select
import threading
import sys
import os
from urllib.parse import urlparse

LISTEN_HOST = '0.0.0.0'
LISTEN_PORT = 3128
BUFFER_SIZE = 8192

# Get upstream proxy from environment
UPSTREAM_PROXY = os.getenv('HTTPS_PROXY', os.getenv('HTTP_PROXY', ''))
if not UPSTREAM_PROXY:
    print("ERROR: No HTTPS_PROXY or HTTP_PROXY environment variable set", file=sys.stderr)
    sys.exit(1)

# Parse upstream proxy URL
parsed = urlparse(UPSTREAM_PROXY)
UPSTREAM_HOST = parsed.hostname
UPSTREAM_PORT = parsed.port or 80
UPSTREAM_AUTH = None
if parsed.username:
    # Build auth string
    if parsed.password:
        UPSTREAM_AUTH = f"{parsed.username}:{parsed.password}"
    else:
        UPSTREAM_AUTH = parsed.username

print(f"Upstream proxy: {UPSTREAM_HOST}:{UPSTREAM_PORT} (auth: {'yes' if UPSTREAM_AUTH else 'no'})", file=sys.stderr)

def handle_client(client_socket, client_address):
    """Handle a client connection"""
    upstream_socket = None
    try:
        # Read the CONNECT request from client
        request = client_socket.recv(BUFFER_SIZE).decode('utf-8')
        print(f"Request from {client_address}:\n{request[:200]}", file=sys.stderr)

        # Parse the CONNECT request
        lines = request.split('\r\n')
        if not lines[0].startswith('CONNECT'):
            client_socket.sendall(b'HTTP/1.1 400 Bad Request\r\n\r\n')
            return

        # Extract target host and port
        parts = lines[0].split()
        if len(parts) < 2:
            client_socket.sendall(b'HTTP/1.1 400 Bad Request\r\n\r\n')
            return

        target = parts[1]  # e.g., "ghcr.io:443"
        print(f"Client wants to connect to {target}", file=sys.stderr)

        # Connect to upstream proxy
        print(f"Connecting to upstream proxy {UPSTREAM_HOST}:{UPSTREAM_PORT}", file=sys.stderr)
        upstream_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        upstream_socket.settimeout(30)
        upstream_socket.connect((UPSTREAM_HOST, UPSTREAM_PORT))

        # Send CONNECT request to upstream proxy (with auth if needed)
        upstream_request = f"CONNECT {target} HTTP/1.1\r\n"
        upstream_request += f"Host: {target}\r\n"
        if UPSTREAM_AUTH:
            import base64
            auth_b64 = base64.b64encode(UPSTREAM_AUTH.encode()).decode()
            upstream_request += f"Proxy-Authorization: Basic {auth_b64}\r\n"
        upstream_request += "\r\n"

        print(f"Sending CONNECT to upstream (auth: {'yes' if UPSTREAM_AUTH else 'no'})", file=sys.stderr)
        upstream_socket.sendall(upstream_request.encode())

        # Read response from upstream proxy
        upstream_response = b''
        while b'\r\n\r\n' not in upstream_response:
            chunk = upstream_socket.recv(BUFFER_SIZE)
            if not chunk:
                raise Exception("Upstream proxy closed connection")
            upstream_response += chunk

        response_str = upstream_response.decode('utf-8', errors='ignore')
        print(f"Upstream response: {response_str.split()[0:3]}", file=sys.stderr)

        # Check if upstream proxy accepted the connection
        if not response_str.startswith('HTTP/1.1 200') and not response_str.startswith('HTTP/1.0 200'):
            print(f"Upstream proxy rejected connection: {response_str[:100]}", file=sys.stderr)
            client_socket.sendall(upstream_response)
            return

        # Send success response to client
        client_socket.sendall(b'HTTP/1.1 200 Connection Established\r\n\r\n')
        print(f"Connection established through upstream proxy to {target}", file=sys.stderr)

        # Bi-directional forwarding between client and upstream proxy
        client_socket.setblocking(False)
        upstream_socket.setblocking(False)

        while True:
            # Wait for data from either socket
            read_sockets, _, _ = select.select([client_socket, upstream_socket], [], [], 1.0)

            if not read_sockets:
                continue

            for sock in read_sockets:
                try:
                    data = sock.recv(BUFFER_SIZE)
                    if not data:
                        return

                    # Forward data to the other socket
                    if sock is client_socket:
                        upstream_socket.sendall(data)
                    else:
                        client_socket.sendall(data)
                except Exception as e:
                    print(f"Error forwarding data: {e}", file=sys.stderr)
                    return

    except socket.timeout:
        print(f"Connection timed out", file=sys.stderr)
        try:
            client_socket.sendall(b'HTTP/1.1 504 Gateway Timeout\r\n\r\n')
        except:
            pass
    except Exception as e:
        print(f"Error handling client {client_address}: {e}", file=sys.stderr)
        try:
            client_socket.sendall(f'HTTP/1.1 500 Internal Server Error\r\n\r\n{e}'.encode())
        except:
            pass
    finally:
        try:
            client_socket.close()
        except:
            pass
        try:
            if upstream_socket:
                upstream_socket.close()
        except:
            pass

def main():
    """Main proxy server loop"""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((LISTEN_HOST, LISTEN_PORT))
    server.listen(5)

    print(f"HTTPS proxy listening on {LISTEN_HOST}:{LISTEN_PORT}", file=sys.stderr)

    while True:
        client_socket, client_address = server.accept()
        print(f"Connection from {client_address}", file=sys.stderr)

        # Handle each client in a separate thread
        client_thread = threading.Thread(
            target=handle_client,
            args=(client_socket, client_address)
        )
        client_thread.daemon = True
        client_thread.start()

if __name__ == '__main__':
    main()
