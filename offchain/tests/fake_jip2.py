"""A fake JIP-2 node for tests: a WebSocket server (RFC 6455, written independently of
offchain/jip2.py so the two cannot share a framing bug) answering JSON-RPC 2.0 calls from a
table of methods. Knobs let a test script the awkward cases: fragmented or oversized
answers, a ping or a notification ahead of the answer, a dropped connection, a close
frame, a refused or mis-keyed handshake.
"""
import base64, hashlib, json, socket, struct, threading

RFC6455_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class RpcError(Exception):
    def __init__(self, code, message, data=None):
        super().__init__(message)
        self.code, self.message, self.data = code, message, data


def server_frame(opcode, payload, fin=True):
    # server-to-client frames are never masked
    b0 = (0x80 if fin else 0) | opcode
    n = len(payload)
    if n < 126:
        head = struct.pack("!BB", b0, n)
    elif n < 65536:
        head = struct.pack("!BBH", b0, 126, n)
    else:
        head = struct.pack("!BBQ", b0, 127, n)
    return head + payload


class FakeJip2Node:
    def __init__(self, methods=None):
        self.methods = dict(methods or {})
        self.requests = []           # every JSON-RPC request received, in order
        self.masked = []             # was each client frame masked?
        self.pongs = []              # payloads of pong frames received
        self.connections = 0
        # scripting knobs
        self.fragment = 0            # >0: send each answer in fragments of this many bytes
        self.ping_first = False      # send a ping before each answer
        self.notify_first = False    # send a notification (no "id") before each answer
        self.drop_on = set()         # method names: close the TCP connection instead of answering
        self.drop_once = set()       # like drop_on, but only the first time
        self.close_on = set()        # method names: send a close frame instead of answering
        self.bad_accept = False
        self.status = 101
        self._sock = socket.socket()
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(16)
        self.url = "ws://127.0.0.1:%d" % self._sock.getsockname()[1]
        self._stop = False
        threading.Thread(target=self._accept_loop, daemon=True).start()

    def stop(self):
        self._stop = True
        try:
            self._sock.close()
        except OSError:
            pass

    def _accept_loop(self):
        while not self._stop:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            self.connections += 1
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    @staticmethod
    def _read_exact(conn, n):
        buf = b""
        while len(buf) < n:
            chunk = conn.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("client went away")
            buf += chunk
        return buf

    def _read_frame(self, conn):
        b0, b1 = self._read_exact(conn, 2)
        n = b1 & 0x7F
        if n == 126:
            n = struct.unpack("!H", self._read_exact(conn, 2))[0]
        elif n == 127:
            n = struct.unpack("!Q", self._read_exact(conn, 8))[0]
        masked = bool(b1 & 0x80)
        key = self._read_exact(conn, 4) if masked else b"\0\0\0\0"
        data = bytearray(self._read_exact(conn, n))
        for i in range(len(data)):
            data[i] ^= key[i % 4]
        self.masked.append(masked)
        return b0 & 0x0F, bytes(data)

    def _send_message(self, conn, text):
        data = text.encode()
        if self.fragment and len(data) > self.fragment:
            chunks = [data[i:i + self.fragment] for i in range(0, len(data), self.fragment)]
            for i, c in enumerate(chunks):
                conn.sendall(server_frame(0x1 if i == 0 else 0x0, c, fin=i == len(chunks) - 1))
        else:
            conn.sendall(server_frame(0x1, data))

    def _serve(self, conn):
        with conn:
            try:
                req = b""
                while b"\r\n\r\n" not in req:
                    chunk = conn.recv(4096)
                    if not chunk:
                        return
                    req += chunk
                headers = {}
                for ln in req.decode().split("\r\n")[1:]:
                    if ":" in ln:
                        k, v = ln.split(":", 1)
                        headers[k.strip().lower()] = v.strip()
                if self.status != 101:
                    conn.sendall(b"HTTP/1.1 %d Nope\r\nContent-Length: 0\r\n\r\n" % self.status)
                    return
                accept = base64.b64encode(hashlib.sha1(
                    (headers["sec-websocket-key"] + RFC6455_GUID).encode()).digest()).decode()
                if self.bad_accept:
                    accept = base64.b64encode(b"x" * 20).decode()
                conn.sendall(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                              "Connection: Upgrade\r\nSec-WebSocket-Accept: %s\r\n\r\n" % accept).encode())
                while True:
                    op, data = self._read_frame(conn)
                    if op == 0xA:
                        self.pongs.append(data)
                        continue
                    if op == 0x8:
                        return
                    msg = json.loads(data)
                    self.requests.append(msg)
                    method = msg["method"]
                    if method in self.drop_on or method in self.drop_once:
                        self.drop_once.discard(method)
                        return
                    if method in self.close_on:
                        conn.sendall(server_frame(0x8, struct.pack("!H", 1011) + b"going away"))
                        return
                    if self.ping_first:
                        conn.sendall(server_frame(0x9, b"are-you-there"))
                    if self.notify_first:
                        self._send_message(conn, json.dumps(
                            {"jsonrpc": "2.0", "method": "subscribeBestBlock",
                             "params": {"subscription": 1, "result": {"slot": 0}}}))
                    try:
                        fn = self.methods[method]
                        out = {"jsonrpc": "2.0", "id": msg["id"], "result": fn(*msg["params"])}
                    except RpcError as e:
                        err = {"code": e.code, "message": e.message}
                        if e.data is not None:
                            err["data"] = e.data
                        out = {"jsonrpc": "2.0", "id": msg["id"], "error": err}
                    except KeyError:
                        out = {"jsonrpc": "2.0", "id": msg["id"],
                               "error": {"code": -32601, "message": "Method not found"}}
                    self._send_message(conn, json.dumps(out))
            except (ConnectionError, OSError):
                return
