#!/usr/bin/env python3
"""
A persistent-connection HTTP/1.1 calculator built on a raw TCP socket.

No framework, no http.server -- just `socket`, a byte buffer, and a parser
that knows exactly where one request ends and the next one begins.

    GET /add?a=2&b=3   -> 200  5
    GET /sub?a=10&b=4  -> 200  6
    GET /mul?a=6&b=7   -> 200  42
    GET /div?a=9&b=3   -> 200  3
    GET /div?a=1&b=0   -> 400
    GET /add?a=x&b=3   -> 400
    GET /pow?a=2&b=8   -> 404
    POST /add          -> 405
    GET /add (no Host) -> 400

The core idea: every connection owns a single receive buffer. Bytes are
appended as they arrive and requests are carved off the front of it. A
request is "done" when its headers are complete AND exactly Content-Length
body bytes (or a complete chunked body) have been consumed. Whatever is left
in the buffer belongs to the next request -- which is what makes keep-alive
and pipelining work.

Stretch goals implemented:
  * Connection: close is honoured (both from the client and HTTP/1.0 default)
  * idle timeout between requests (see IDLE_TIMEOUT for the reasoning)
  * chunked request bodies (Transfer-Encoding: chunked)
  * pipelining: many requests in one write are answered in order
"""

import argparse
import math
import re
import socket
import sys
import threading
from urllib.parse import parse_qsl, urlsplit

# --------------------------------------------------------------------------
# Limits. Anything that lets a client make us buffer unbounded data is a DoS
# vector, so every unbounded quantity gets a ceiling.
# --------------------------------------------------------------------------

# How long a keep-alive connection may sit with no complete request before we
# hang up. 15s is long enough for a human poking at it with `nc` or for a
# client to fire off its next request, short enough that idle sockets don't
# pile up and pin threads. (nginx defaults to 75s, Apache to 5s; this sits
# between them.) The same timeout also bounds a client that sends half a
# request and stalls (slowloris).
IDLE_TIMEOUT = 15.0

MAX_HEADER_BYTES = 8 * 1024     # request line + headers
MAX_BODY_BYTES = 1024 * 1024    # bodies are ignored, but must still be consumed
MAX_REQUESTS_PER_CONN = 1000    # keep one client from holding a thread forever
RECV_SIZE = 4096

# End of the header block. RFC 9112 says CRLF, but recommends tolerating a
# bare LF, so we accept either.
HEADER_END = re.compile(rb"\r?\n\r?\n")

REASONS = {
    200: "OK",
    400: "Bad Request",
    404: "Not Found",
    405: "Method Not Allowed",
    408: "Request Timeout",
    411: "Length Required",
    413: "Content Too Large",
    431: "Request Header Fields Too Large",
    501: "Not Implemented",
    505: "HTTP Version Not Supported",
}

OPERATIONS = {
    "/add": lambda a, b: a + b,
    "/sub": lambda a, b: a - b,
    "/mul": lambda a, b: a * b,
    "/div": lambda a, b: a / b,
}


class HTTPError(Exception):
    """A request we must reject.

    `close` means the framing of the stream can no longer be trusted (we
    don't know where the next request starts), so after replying we must
    drop the connection rather than try to parse garbage.
    """

    def __init__(self, status, message, close=False, request=None, consumed=None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.close = close
        # Set only when the error is recoverable: the request was fully
        # consumed, so the connection can carry on with the next one.
        self.request = request
        self.consumed = consumed


class NeedMoreData(Exception):
    """The buffer doesn't hold a complete request yet."""


# --------------------------------------------------------------------------
# Request parsing
# --------------------------------------------------------------------------

class Request:
    def __init__(self, method, target, version, headers, body):
        self.method = method
        self.target = target
        self.version = version
        self.headers = headers   # dict: lower-cased name -> list of values
        self.body = body

    def header(self, name):
        values = self.headers.get(name)
        return values[-1] if values else None

    def tokens(self, name):
        """Comma-separated header tokens, lower-cased (e.g. Connection)."""
        out = []
        for value in self.headers.get(name, []):
            out.extend(t.strip().lower() for t in value.split(",") if t.strip())
        return out


def parse_request(buf):
    """Try to carve one request off the front of `buf`.

    Returns (Request, bytes_consumed). Raises NeedMoreData if the request is
    incomplete, or HTTPError if it's malformed. Never consumes a byte beyond
    the end of the request -- byte n+1 belongs to somebody else.
    """
    m = HEADER_END.search(buf, 0, MAX_HEADER_BYTES + 4)
    if not m:
        if len(buf) > MAX_HEADER_BYTES:
            raise HTTPError(431, "request headers too large", close=True)
        raise NeedMoreData

    head = buf[:m.start()].decode("latin-1")
    lines = head.replace("\r\n", "\n").split("\n")

    # --- request line -----------------------------------------------------
    parts = lines[0].split(" ")
    if len(parts) != 3 or not all(parts):
        raise HTTPError(400, "malformed request line", close=True)
    method, target, version = parts
    if not re.fullmatch(r"HTTP/\d\.\d", version):
        raise HTTPError(400, "malformed HTTP version", close=True)
    if version not in ("HTTP/1.0", "HTTP/1.1"):
        raise HTTPError(505, "only HTTP/1.0 and HTTP/1.1 are supported", close=True)

    # --- headers ----------------------------------------------------------
    headers = {}
    for line in lines[1:]:
        if line[:1] in (" ", "\t"):
            # obsolete line folding; RFC 9112 says reject it
            raise HTTPError(400, "obsolete header line folding", close=True)
        name, sep, value = line.partition(":")
        if not sep or not name or name != name.strip():
            # "Name : value" (whitespace before colon) is a smuggling vector
            raise HTTPError(400, "malformed header line", close=True)
        headers.setdefault(name.lower(), []).append(value.strip(" \t"))

    req = Request(method, target, version, headers, b"")

    # --- Host -------------------------------------------------------------
    # A missing Host is a 400, but the framing is still sound, so we finish
    # consuming the body first and keep the connection alive afterwards.
    missing_host = version == "HTTP/1.1" and len(headers.get("host", [])) != 1

    # --- body framing -----------------------------------------------------
    pos = m.end()
    te = req.tokens("transfer-encoding")
    cl_values = headers.get("content-length", [])

    if te:
        if cl_values:
            # Both present: the classic request-smuggling setup. Reject and
            # close, since two parsers could disagree on where this ends.
            raise HTTPError(400, "both Transfer-Encoding and Content-Length", close=True)
        if te[-1] != "chunked":
            raise HTTPError(501, "unsupported Transfer-Encoding", close=True)
        if te != ["chunked"]:
            raise HTTPError(501, "only plain chunked encoding is supported", close=True)
        req.body, pos = read_chunked(buf, pos)
    elif cl_values:
        # Duplicate Content-Length headers are only OK if they all agree.
        lengths = {v.strip() for cl in cl_values for v in cl.split(",")}
        if len(lengths) != 1:
            raise HTTPError(400, "conflicting Content-Length", close=True)
        raw = lengths.pop()
        if not raw.isdigit():   # rejects "-1", "+5", " 5", "0x10", ""
            raise HTTPError(400, "invalid Content-Length", close=True)
        length = int(raw)
        if length > MAX_BODY_BYTES:
            raise HTTPError(413, "request body too large", close=True)
        if len(buf) - pos < length:
            raise NeedMoreData
        req.body = bytes(buf[pos:pos + length])
        pos += length
    # else: no body. A request with neither header has a zero-length body.

    if missing_host:
        raise HTTPError(400, "HTTP/1.1 requests need exactly one Host header",
                        request=req, consumed=pos)

    return req, pos


def read_chunked(buf, pos):
    """Decode a chunked body starting at buf[pos]. Returns (body, end_pos)."""
    body = bytearray()
    while True:
        eol = buf.find(b"\n", pos)
        if eol == -1:
            if len(buf) - pos > 1024:
                raise HTTPError(400, "chunk size line too long", close=True)
            raise NeedMoreData
        size_line = bytes(buf[pos:eol]).rstrip(b"\r")
        size_str = size_line.split(b";", 1)[0].strip()   # drop chunk extensions
        if not size_str or not re.fullmatch(rb"[0-9A-Fa-f]+", size_str):
            raise HTTPError(400, "invalid chunk size", close=True)
        size = int(size_str, 16)
        pos = eol + 1

        if size == 0:
            # Trailer section: zero or more header lines, then an empty line.
            while True:
                eol = buf.find(b"\n", pos)
                if eol == -1:
                    if len(buf) - pos > MAX_HEADER_BYTES:
                        raise HTTPError(431, "trailers too large", close=True)
                    raise NeedMoreData
                line = bytes(buf[pos:eol]).rstrip(b"\r")
                pos = eol + 1
                if not line:
                    return bytes(body), pos

        if len(body) + size > MAX_BODY_BYTES:
            raise HTTPError(413, "request body too large", close=True)
        if len(buf) - pos < size + 1:
            raise NeedMoreData
        body += buf[pos:pos + size]
        pos += size
        # each chunk's data is followed by CRLF (or a tolerated bare LF)
        if buf[pos:pos + 2] == b"\r\n":
            pos += 2
        elif buf[pos:pos + 1] == b"\n":
            pos += 1
        elif len(buf) - pos < 2:
            raise NeedMoreData
        else:
            raise HTTPError(400, "missing CRLF after chunk data", close=True)


# --------------------------------------------------------------------------
# Application: the calculator itself (the easy part)
# --------------------------------------------------------------------------

def parse_number(raw):
    if raw is None:
        raise ValueError("missing")
    raw = raw.strip()
    try:
        return int(raw)
    except ValueError:
        pass
    value = float(raw)          # raises ValueError for "x"
    if not math.isfinite(value):
        raise ValueError("not finite")
    return value


def format_number(value):
    if isinstance(value, float):
        if not math.isfinite(value):
            raise OverflowError
        if value.is_integer():
            return str(int(value))
        return repr(value)
    return str(value)


def handle(req):
    """Map a parsed request to (status, body, extra_headers)."""
    try:
        url = urlsplit(req.target)
    except ValueError:
        return 400, "bad request target", {}

    op = OPERATIONS.get(url.path)
    if op is None:
        return 404, f"no such operation: {url.path}", {}
    if req.method not in ("GET", "HEAD"):
        return 405, f"method {req.method} not allowed", {"Allow": "GET, HEAD"}

    params = dict(parse_qsl(url.query, keep_blank_values=True))
    try:
        a = parse_number(params.get("a"))
        b = parse_number(params.get("b"))
    except ValueError:
        return 400, "a and b must both be numbers", {}

    try:
        if url.path == "/div" and isinstance(a, int) and isinstance(b, int) \
                and b != 0 and a % b == 0:
            result = a // b     # keep exact integers exact: 9/3 -> 3
        else:
            result = op(a, b)
        text = format_number(result)
    except ZeroDivisionError:
        return 400, "division by zero", {}
    except (OverflowError, ValueError):
        # float overflow, or an integer too long for str() to print
        return 400, "result out of range", {}

    return 200, text, {}


# --------------------------------------------------------------------------
# Connection handling
# --------------------------------------------------------------------------

def build_response(status, body, headers, keep_alive, head_only=False):
    payload = (body + "\n").encode("utf-8")
    lines = [f"HTTP/1.1 {status} {REASONS[status]}"]
    all_headers = {
        "Content-Type": "text/plain; charset=utf-8",
        # Always send Content-Length: it's how the *client* finds the end of
        # our response without us closing the socket.
        "Content-Length": str(len(payload)),
        "Connection": "keep-alive" if keep_alive else "close",
    }
    if keep_alive:
        all_headers["Keep-Alive"] = f"timeout={int(IDLE_TIMEOUT)}"
    all_headers.update(headers)
    lines += [f"{k}: {v}" for k, v in all_headers.items()]
    head = ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")
    return head if head_only else head + payload


def wants_keep_alive(req):
    tokens = req.tokens("connection")
    if "close" in tokens:
        return False
    if req.version == "HTTP/1.0":
        return "keep-alive" in tokens   # 1.0 closes unless asked otherwise
    return True                          # 1.1 is persistent by default


def log(addr, msg):
    print(f"[{addr[0]}:{addr[1]}] {msg}", file=sys.stderr, flush=True)


def serve_connection(conn, addr, verbose=False):
    conn.settimeout(IDLE_TIMEOUT)
    buf = bytearray()
    served = 0
    try:
        while True:
            # 1. Answer every complete request already in the buffer. With
            #    pipelining there may be several; they're answered strictly
            #    in order because we handle them one at a time, front first.
            while True:
                try:
                    req, consumed = parse_request(buf)
                except NeedMoreData:
                    break
                except HTTPError as err:
                    if err.request is None:
                        # Framing is broken: reply, then hang up.
                        conn.sendall(build_response(err.status, err.message, {}, False))
                        if verbose:
                            log(addr, f"{err.status} {err.message} (closing)")
                        return
                    req, consumed = err.request, err.consumed
                    del buf[:consumed]
                    served += 1
                    keep = wants_keep_alive(req) and served < MAX_REQUESTS_PER_CONN
                    conn.sendall(build_response(err.status, err.message, {}, keep,
                                                head_only=req.method == "HEAD"))
                    if verbose:
                        log(addr, f"{req.method} {req.target} -> {err.status}")
                    if not keep:
                        return
                    continue

                del buf[:consumed]   # exactly this request, not one byte more
                served += 1
                status, body, extra = handle(req)
                keep = wants_keep_alive(req) and served < MAX_REQUESTS_PER_CONN
                conn.sendall(build_response(status, body, extra, keep,
                                            head_only=req.method == "HEAD"))
                if verbose:
                    log(addr, f"{req.method} {req.target} -> {status}")
                if not keep:
                    return

            # 2. Need more bytes.
            try:
                chunk = conn.recv(RECV_SIZE)
            except socket.timeout:
                if buf.strip():
                    # Client stalled mid-request.
                    conn.sendall(build_response(408, "request timed out", {}, False))
                if verbose:
                    log(addr, "idle timeout, closing")
                return
            if not chunk:
                return          # client closed its side
            buf += chunk
    except (ConnectionError, OSError):
        pass
    finally:
        try:
            conn.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        conn.close()
        if verbose:
            log(addr, f"closed after {served} request(s)")


def serve(host, port, verbose=False):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as srv:
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((host, port))
        srv.listen(128)
        print(f"calculator listening on http://{host}:{port}", file=sys.stderr, flush=True)
        while True:
            conn, addr = srv.accept()
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            if verbose:
                log(addr, "connected")
            threading.Thread(target=serve_connection, args=(conn, addr, verbose),
                             daemon=True).start()


def main():
    global IDLE_TIMEOUT
    p = argparse.ArgumentParser(description="Keep-alive HTTP/1.1 calculator")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8080)
    p.add_argument("--idle-timeout", type=float, default=IDLE_TIMEOUT)
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()
    IDLE_TIMEOUT = args.idle_timeout
    try:
        serve(args.host, args.port, args.verbose)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
