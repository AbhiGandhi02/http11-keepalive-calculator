# HTTP/1.1 keep-alive calculator

A calculator that runs over HTTP/1.1 and keeps the connection open. It uses only Python's standard library: a raw `socket`, a byte buffer and a hand-written request parser. It doesn't use a framework or `http.server`.

```
GET /add?a=2&b=3   -> 200  5
GET /sub?a=10&b=4  -> 200  6
GET /mul?a=6&b=7   -> 200  42
GET /div?a=9&b=3   -> 200  3

GET /div?a=1&b=0   -> 400
GET /add?a=x&b=3   -> 400
GET /pow?a=2&b=8   -> 404
POST /add          -> 405
GET /add (no Host) -> 400
```

## Run it

Requires Python 3.8 or later. There's nothing to install.

```sh
python3 server.py                 # listens on 127.0.0.1:8080
python3 server.py -v              # log every request and connection
python3 server.py --port 9000 --idle-timeout 30
```

In a second terminal:

```sh
python3 grade.py                  # the marking script: one socket, every request
python3 -m unittest -v            # full test suite (22 tests)
```

Expected output of `grade.py`:

```
  GET  /add?a=2&b=3     -> 200  5
  GET  /sub?a=10&b=4    -> 200  6
  GET  /mul?a=6&b=7     -> 200  42
  GET  /div?a=1&b=0     -> 400
  GET  /pow?a=2&b=8     -> 404
  POST /add             -> 405

  socket still open: True
  1 TCP handshake, 6 responses
```

## The hard part: where does a request end?

With HTTP/1.0 the server hung up after every response, so a request ended at EOF. With a persistent connection, many requests share one byte stream, so the server has to find each boundary itself.

Each connection owns a single `bytearray` buffer:

1. Bytes from `recv()` are appended to the buffer.
2. `parse_request(buf)` tries to carve one request off the front of the buffer:
   - It looks for the blank line that ends the headers. If the blank line hasn't arrived yet, it raises `NeedMoreData`.
   - It reads the body using the framing the headers declare:
     - With `Content-Length: N`, it waits until N bytes are buffered and takes exactly N bytes.
     - With `Transfer-Encoding: chunked`, it decodes chunks up to the `0` chunk and its trailers.
     - With neither header, the body is empty.
   - It returns the request and the number of bytes it consumed.
3. `del buf[:consumed]` removes that request and nothing more. Whatever remains, from byte n+1 onwards, belongs to the next request.
4. The loop repeats until the buffer holds no complete request, then goes back to `recv()`.

Pipelining needs no extra code. If six requests arrive in one TCP segment, step 2 runs six times before the next `recv()`. Each response goes out as soon as its request is handled, so responses are always in request order.

Every response carries `Content-Length` too, because that's how the client finds the end of each response without the server closing the socket.

### When the stream can't be trusted

Some errors make the request boundary unknowable:

- a bad or negative `Content-Length`
- `Content-Length` and `Transfer-Encoding` together, the classic request-smuggling setup
- a garbled request line
- oversized headers

For these, the server sends the error and then closes the connection. Guessing where the next request starts is how request smuggling happens.

Other errors leave the framing intact: a missing `Host`, a bad number, an unknown path or the wrong method. For these, the server consumes the whole request, sends the error and keeps the connection open. That's why the marker's 400, 404 and 405 don't kill the socket.

## Stretch goals (all implemented)

| Feature | Behaviour |
|---|---|
| `Connection: close` | Honoured on HTTP/1.1. HTTP/1.0 closes by default unless it sends `Connection: keep-alive`. Requests pipelined after a `close` aren't processed. |
| Idle timeout | 15 s between requests, then the server closes the socket. A client that stalls halfway through a request gets `408` and the socket closes. The timeout is advertised in a `Keep-Alive: timeout=15` header. |
| Chunked encoding | Request bodies with `Transfer-Encoding: chunked` are decoded. Chunk extensions and trailers are handled, and the parser copes with chunks split across reads. |
| Pipelining | Send all six requests (or fifty) in one write and the answers come back in order. |

**Why 15 seconds?** An idle keep-alive socket costs a thread and a file descriptor, so the timeout is a trade-off. Too long, and idle or abandoned clients pile up; it also gives slowloris-style attackers, who trickle a request in slowly to hold connections open, more time. Too short, and a client that pauses between requests pays for a new TCP handshake every time. 15 s covers a client sending a burst of requests, or a human typing into `nc`, while freeing idle sockets quickly. The value sits between Apache's default of 5 s and nginx's of 75 s. You can change it with `--idle-timeout`.

## Other details

- **Limits:** request headers are capped at 8 KiB (`431`), bodies at 1 MiB (`413`) and each connection at 1000 requests. Without these caps, a client could make the server buffer unbounded data.
- **Strict parsing:** the parser rejects these, as RFC 9112 recommends:
  - obsolete header line folding
  - whitespace before a header's colon
  - conflicting duplicate `Content-Length` headers
  - `Content-Length` values like `+5` or `0x10`
- **Lenient parsing:** bare `\n` line endings are accepted, so hand-typed `nc` sessions work.
- **Numbers:** integers stay exact (`9/3` returns `3`, not `3.0`). Floats work (`10/4` returns `2.5`). `nan`, `inf`, missing values and overflowing results get `400`.
- **`HEAD`:** sends the same headers as `GET` with no body.
- **`405`:** includes an `Allow: GET, HEAD` header.
- **Concurrency:** each connection gets its own thread, so one slow client doesn't block the others.

## Files

| File | Purpose |
|---|---|
| `server.py` | The server: parser, calculator and connection loop |
| `grade.py` | Reproduces the marking script |
| `test_server.py` | Unit tests for the feature set, framing edge cases and every stretch goal |
