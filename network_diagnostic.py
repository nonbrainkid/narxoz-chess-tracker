"""Public host connectivity only. Never loads or sends credentials."""
import socket
import ssl
import time

HOST = "sport.narxoz.kz"
context = ssl.create_default_context()
for family, socktype, proto, _, address in socket.getaddrinfo(HOST, 443, type=socket.SOCK_STREAM):
    start = time.monotonic()
    stage = "TCP"
    try:
        with socket.socket(family, socktype, proto) as sock:
            sock.settimeout(8)
            sock.connect(address)
            stage = "TLS"
            with context.wrap_socket(sock, server_hostname=HOST) as tls:
                tls.sendall(b"GET / HTTP/1.1\r\nHost: sport.narxoz.kz\r\nConnection: close\r\n\r\n")
                stage = "HTTP"
                status = tls.recv(64).split(b"\r\n", 1)[0].decode("ascii", "replace")
                print(f"{address[0]} {tls.version()} {status} {time.monotonic()-start:.1f}s", flush=True)
    except Exception as exc:
        print(f"{address[0]} {stage} {type(exc).__name__}: {str(exc)[:150]} {time.monotonic()-start:.1f}s", flush=True)
