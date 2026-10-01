#!/usr/bin/env python3
"""Tests for server.py. Run with:  python3 -m unittest -v"""

import socket
import threading
import time
import unittest

import server


class ServerTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        server.IDLE_TIMEOUT = 1.0   # keep the timeout test fast
        cls.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        cls.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        cls.listener.bind(("127.0.0.1", 0))
        cls.listener.listen(16)
        cls.port = cls.listener.getsockname()[1]

        def accept_loop():
            while True:
                try:
                    conn, addr = cls.listener.accept()
                except OSError:
                    return
                threading.Thread(target=server.serve_connection,
                                 args=(conn, addr), daemon=True).start()

        threading.Thread(target=accept_loop, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.listener.close()

    # -- helpers ---------------------------------------------------------

    def connect(self):
        s = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        self.addCleanup(s.close)
        return s, s.makefile("rb")

    @staticmethod
    def read_response(f):
        status_line = f.readline()
        if not status_line:
            return None
        status = int(status_line.split(b" ")[1])
        headers = {}
        while True:
            line = f.readline().rstrip(b"\r\n")
            if not line:
                break
            k, _, v = line.decode().partition(":")
            headers[k.strip().lower()] = v.strip()
        body = f.read(int(headers["content-length"])).decode().strip()
        return status, headers, body

    @staticmethod
    def get(path, extra=""):
        return f"GET {path} HTTP/1.1\r\nHost: localhost\r\n{extra}\r\n".encode()

    def assert_closed(self, s):
        s.settimeout(2)
        self.assertEqual(s.recv(1), b"")

    def assert_open(self, s):
        s.setblocking(False)
        try:
            self.assertNotEqual(s.recv(1, socket.MSG_PEEK), b"")
        except BlockingIOError:
            pass
        finally:
            s.setblocking(True)


class TestFeatureSet(ServerTestCase):
    CASES = [
        (b"GET /add?a=2&b=3 HTTP/1.1\r\nHost: x\r\n\r\n", 200, "5"),
        (b"GET /sub?a=10&b=4 HTTP/1.1\r\nHost: x\r\n\r\n", 200, "6"),
        (b"GET /mul?a=6&b=7 HTTP/1.1\r\nHost: x\r\n\r\n", 200, "42"),
        (b"GET /div?a=9&b=3 HTTP/1.1\r\nHost: x\r\n\r\n", 200, "3"),
        (b"GET /div?a=1&b=0 HTTP/1.1\r\nHost: x\r\n\r\n", 400, None),
        (b"GET /add?a=x&b=3 HTTP/1.1\r\nHost: x\r\n\r\n", 400, None),
        (b"GET /pow?a=2&b=8 HTTP/1.1\r\nHost: x\r\n\r\n", 404, None),
        (b"POST /add HTTP/1.1\r\nHost: x\r\nContent-Length: 0\r\n\r\n", 405, None),
        (b"POST /add HTTP/1.1\r\nHost: x\r\n\r\n", 405, None),
        (b"GET /add?a=2&b=3 HTTP/1.1\r\n\r\n", 400, None),   # no Host
    ]

    def test_every_case_on_one_socket(self):
        s, f = self.connect()
        for raw, status, body in self.CASES:
            with self.subTest(raw=raw):
                s.sendall(raw)
                got = self.read_response(f)
                self.assertIsNotNone(got, "server closed the connection")
                self.assertEqual(got[0], status)
                if body is not None:
                    self.assertEqual(got[2], body)
                self.assertEqual(got[1]["connection"], "keep-alive")
        self.assert_open(s)

    def test_marker_script(self):
        """Exactly what the marker runs: 6 requests, 1 handshake."""
        s, f = self.connect()
        for raw, status in [
            (self.get("/add?a=2&b=3"), 200), (self.get("/sub?a=10&b=4"), 200),
            (self.get("/mul?a=6&b=7"), 200), (self.get("/div?a=1&b=0"), 400),
            (self.get("/pow?a=2&b=8"), 404),
            (b"POST /add HTTP/1.1\r\nHost: localhost\r\n\r\n", 405),
        ]:
            s.sendall(raw)
            self.assertEqual(self.read_response(f)[0], status)
        self.assert_open(s)

    def test_405_has_allow_header(self):
        s, f = self.connect()
        s.sendall(b"POST /add HTTP/1.1\r\nHost: x\r\nContent-Length: 0\r\n\r\n")
        self.assertIn("GET", self.read_response(f)[1]["allow"])

    def test_arithmetic_edge_cases(self):
        s, f = self.connect()
        for path, status, body in [
            ("/div?a=10&b=4", 200, "2.5"),
            ("/add?a=-2&b=3", 200, "1"),
            ("/add?a=1.5&b=1.5", 200, "3"),
            ("/add?a=nan&b=1", 400, None),
            ("/add?a=inf&b=1", 400, None),
            ("/add?a=1", 400, None),
            ("/add", 400, None),
            ("/add?a=&b=1", 400, None),
            ("/div?a=1e308&b=1e-308", 400, None),
        ]:
            with self.subTest(path=path):
                s.sendall(self.get(path))
                got = self.read_response(f)
                self.assertEqual(got[0], status)
                if body is not None:
                    self.assertEqual(got[2], body)


class TestFraming(ServerTestCase):
    def test_body_consumed_exactly(self):
        """A body that looks like a request must not be parsed as one."""
        s, f = self.connect()
        body = b"GET /pow?a=1&b=1 HTTP/1.1\r\nHost: x\r\n\r\n"
        s.sendall(b"POST /add HTTP/1.1\r\nHost: x\r\nContent-Length: %d\r\n\r\n" % len(body)
                  + body + self.get("/add?a=1&b=1"))
        self.assertEqual(self.read_response(f)[0], 405)
        self.assertEqual(self.read_response(f)[:3:2], (200, "2"))

    def test_request_split_across_writes(self):
        s, f = self.connect()
        raw = b"POST /add HTTP/1.1\r\nHost: x\r\nContent-Length: 5\r\n\r\nhello"
        for i in range(len(raw)):
            s.sendall(raw[i:i + 1])
            time.sleep(0.001)
        self.assertEqual(self.read_response(f)[0], 405)
        s.sendall(self.get("/mul?a=3&b=3"))
        self.assertEqual(self.read_response(f)[2], "9")

    def test_bare_lf_tolerated(self):
        s, f = self.connect()
        s.sendall(b"GET /add?a=2&b=2 HTTP/1.1\nHost: x\n\n")
        self.assertEqual(self.read_response(f)[2], "4")

    def test_invalid_content_length_closes(self):
        for cl in (b"-1", b"abc", b"+5", b"1, 2"):
            with self.subTest(cl=cl):
                s, f = self.connect()
                s.sendall(b"POST /add HTTP/1.1\r\nHost: x\r\nContent-Length: " + cl + b"\r\n\r\n")
                self.assertEqual(self.read_response(f)[0], 400)
                self.assert_closed(s)

    def test_cl_and_te_together_rejected(self):
        s, f = self.connect()
        s.sendall(b"POST /add HTTP/1.1\r\nHost: x\r\nContent-Length: 3\r\n"
                  b"Transfer-Encoding: chunked\r\n\r\n0\r\n\r\n")
        self.assertEqual(self.read_response(f)[0], 400)
        self.assert_closed(s)

    def test_garbage_request_line_closes(self):
        s, f = self.connect()
        s.sendall(b"HELLO\r\n\r\n")
        self.assertEqual(self.read_response(f)[0], 400)
        self.assert_closed(s)

    def test_oversized_headers(self):
        s, f = self.connect()
        s.sendall(b"GET /add?a=1&b=1 HTTP/1.1\r\nX: " + b"a" * 10000 + b"\r\n\r\n")
        self.assertEqual(self.read_response(f)[0], 431)
        self.assert_closed(s)

    def test_unsupported_version(self):
        s, f = self.connect()
        s.sendall(b"GET /add?a=1&b=1 HTTP/2.0\r\nHost: x\r\n\r\n")
        self.assertEqual(self.read_response(f)[0], 505)


class TestStretch(ServerTestCase):
    def test_connection_close_honoured(self):
        s, f = self.connect()
        s.sendall(self.get("/add?a=1&b=1", "Connection: close\r\n"))
        status, headers, body = self.read_response(f)
        self.assertEqual((status, body), (200, "2"))
        self.assertEqual(headers["connection"], "close")
        self.assert_closed(s)

    def test_http10_closes_by_default(self):
        s, f = self.connect()
        s.sendall(b"GET /add?a=1&b=1 HTTP/1.0\r\n\r\n")
        self.assertEqual(self.read_response(f)[2], "2")
        self.assert_closed(s)

    def test_http10_keep_alive(self):
        s, f = self.connect()
        for _ in range(2):
            s.sendall(b"GET /add?a=1&b=1 HTTP/1.0\r\nConnection: keep-alive\r\n\r\n")
            self.assertEqual(self.read_response(f)[2], "2")
        self.assert_open(s)

    def test_idle_timeout(self):
        s, f = self.connect()
        s.sendall(self.get("/add?a=1&b=1"))
        self.assertEqual(self.read_response(f)[0], 200)
        start = time.monotonic()
        self.assert_closed(s)
        self.assertLess(time.monotonic() - start, server.IDLE_TIMEOUT + 1)

    def test_stalled_request_gets_408(self):
        s, f = self.connect()
        s.sendall(b"GET /add?a=1&b=1 HTTP/1.1\r\nHost: x\r\n")   # never finished
        self.assertEqual(self.read_response(f)[0], 408)
        self.assert_closed(s)

    def test_chunked_body(self):
        s, f = self.connect()
        s.sendall(b"POST /add HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\n"
                  b"5;ext=1\r\nhello\r\n6\r\n world\r\n0\r\nX-Trailer: y\r\n\r\n"
                  + self.get("/sub?a=5&b=8"))
        self.assertEqual(self.read_response(f)[0], 405)
        self.assertEqual(self.read_response(f)[2], "-3")

    def test_chunked_split_across_writes(self):
        s, f = self.connect()
        raw = (b"POST /add HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\n"
               b"a\r\n0123456789\r\n0\r\n\r\n")
        for i in range(0, len(raw), 3):
            s.sendall(raw[i:i + 3])
            time.sleep(0.001)
        self.assertEqual(self.read_response(f)[0], 405)
        s.sendall(self.get("/add?a=1&b=1"))
        self.assertEqual(self.read_response(f)[2], "2")

    def test_pipelining_in_order(self):
        s, f = self.connect()
        reqs = [self.get(f"/add?a={i}&b=1") for i in range(50)]
        reqs.insert(10, b"POST /add HTTP/1.1\r\nHost: x\r\nContent-Length: 3\r\n\r\nabc")
        reqs.insert(20, self.get("/pow?a=1&b=1"))
        s.sendall(b"".join(reqs))      # one write, many requests
        statuses, bodies = [], []
        for _ in reqs:
            st, _, body = self.read_response(f)
            statuses.append(st)
            if st == 200:
                bodies.append(int(body))
        self.assertEqual(statuses[10], 405)
        self.assertEqual(statuses[20], 404)
        self.assertEqual(bodies, list(range(1, 51)))
        self.assert_open(s)

    def test_pipelined_close_stops_processing(self):
        s, f = self.connect()
        s.sendall(self.get("/add?a=1&b=1", "Connection: close\r\n")
                  + self.get("/add?a=2&b=2"))
        self.assertEqual(self.read_response(f)[2], "2")
        self.assertIsNone(self.read_response(f))

    def test_head(self):
        s, f = self.connect()
        s.sendall(b"HEAD /add?a=1&b=1 HTTP/1.1\r\nHost: x\r\n\r\n" + self.get("/add?a=3&b=3"))
        line = f.readline()
        self.assertIn(b"200", line)
        while f.readline() not in (b"\r\n", b""):
            pass
        # no body after HEAD: next bytes are the second response
        self.assertEqual(self.read_response(f)[2], "6")


if __name__ == "__main__":
    unittest.main(verbosity=2)
