#!/usr/bin/env python3
"""
Reproduces the marking script: one socket, every request.

    python3 server.py &      # in another terminal
    python3 grade.py

Responses are read by parsing the status line and headers, then reading
exactly Content-Length body bytes -- the same discipline the server uses.
"""

import socket
import sys

HOST, PORT = "localhost", int(sys.argv[1]) if len(sys.argv) > 1 else 8080

CASES = [
    ("GET", "/add?a=2&b=3", 200, "5"),
    ("GET", "/sub?a=10&b=4", 200, "6"),
    ("GET", "/mul?a=6&b=7", 200, "42"),
    ("GET", "/div?a=1&b=0", 400, None),
    ("GET", "/pow?a=2&b=8", 404, None),
    ("POST", "/add", 405, None),
]


def read_response(f):
    status_line = f.readline().decode("latin-1").rstrip("\r\n")
    if not status_line:
        raise ConnectionError("server closed the connection")
    status = int(status_line.split(" ", 2)[1])
    headers = {}
    while True:
        line = f.readline().decode("latin-1").rstrip("\r\n")
        if not line:
            break
        k, _, v = line.partition(":")
        headers[k.strip().lower()] = v.strip()
    body = f.read(int(headers.get("content-length", 0)))
    return status, headers, body.decode().strip()


def main():
    s = socket.create_connection((HOST, PORT))
    f = s.makefile("rb")
    ok = True
    for method, path, want_status, want_body in CASES:
        extra = "Content-Length: 0\r\n" if method == "POST" else ""
        s.sendall(f"{method} {path} HTTP/1.1\r\nHost: {HOST}\r\n{extra}\r\n".encode())
        status, _, body = read_response(f)
        good = status == want_status and (want_body is None or body == want_body)
        ok &= good
        print(f"  {method:4} {path:16} -> {status}  {body if status == 200 else '':4}"
              f"{'' if good else '   <-- expected ' + str(want_status)}")

    # Still open? A non-blocking peek returns b"" only if the peer closed.
    s.setblocking(False)
    try:
        still_open = s.recv(1, socket.MSG_PEEK) != b""
    except BlockingIOError:
        still_open = True
    s.close()

    print(f"\n  socket still open: {still_open}")
    print(f"  1 TCP handshake, {len(CASES)} responses")
    sys.exit(0 if ok and still_open else 1)


if __name__ == "__main__":
    main()
