"""A TCP proxy that can break the connection between a component and Redis or Postgres.

Put it between the thing under test and the real server, then misbehave on command:

    proxy = ChaosProxy("localhost", 6379)        # listens on a free local port
    ... point the component at proxy.port ...
    proxy.set_mode("reset")      # every connection is cut at once and new ones are cut on arrival
                                 #   (a crashed or restarted server)
    proxy.set_mode("blackhole")  # connections stay open but nothing gets through, in either direction
                                 #   (a network partition: no error, no answer)
    proxy.set_mode("up")         # back to normal

Pure Python on purpose: no extra binaries, so it runs the same on Linux, macOS, Windows and CI.
"""

import socket
import struct
import threading

UP, RESET, BLACKHOLE = "up", "reset", "blackhole"


class ChaosProxy:
    def __init__(self, target_host: str, target_port: int):
        self._target = (target_host, target_port)
        self._mode = UP
        self._lock = threading.Lock()
        self._pairs: set[tuple[socket.socket, socket.socket]] = set()
        self._held: list[socket.socket] = []  # connections accepted during a blackhole
        self._closed = False
        self._listener = socket.socket()
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(64)
        self.port = self._listener.getsockname()[1]
        threading.Thread(target=self._accept_loop, daemon=True, name="chaos-accept").start()

    # ---- control ----

    @property
    def mode(self) -> str:
        return self._mode

    def set_mode(self, mode: str) -> None:
        assert mode in (UP, RESET, BLACKHOLE)
        with self._lock:
            self._mode = mode
            if mode == RESET:
                for pair in list(self._pairs):
                    self._rst(*pair)
                for held in self._held:
                    self._rst(held)
                self._held.clear()
            elif mode == UP:
                for held in self._held:
                    self._rst(held)  # whatever was parked in the black hole is gone
                self._held.clear()

    def close(self) -> None:
        self._closed = True
        self.set_mode(RESET)
        try:
            self._listener.close()
        except OSError:
            pass

    # ---- plumbing ----

    @staticmethod
    def _rst(*socks: socket.socket) -> None:
        """Close abruptly (a TCP reset, not a polite goodbye)."""
        for s in socks:
            try:
                # shutdown() first: close() alone does NOT wake another thread that is blocked in
                # recv() on the same socket, and the "reset" connection would silently stay half
                # alive and swallow whatever the client sends next (found the hard way: a test hung).
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
                s.close()
            except OSError:
                pass

    def _accept_loop(self) -> None:
        while not self._closed:
            try:
                client, _ = self._listener.accept()
            except OSError:
                return
            with self._lock:
                mode = self._mode
                if mode == RESET:
                    self._rst(client)
                    continue
                if mode == BLACKHOLE:
                    self._held.append(client)  # accepted, never answered
                    continue
            try:
                upstream = socket.create_connection(self._target, timeout=5)
                upstream.settimeout(None)  # the 5 s was for connecting only; idle must stay idle
            except OSError:
                self._rst(client)
                continue
            pair = (client, upstream)
            with self._lock:
                self._pairs.add(pair)
            threading.Thread(target=self._pump, args=(client, upstream, pair), daemon=True).start()
            threading.Thread(target=self._pump, args=(upstream, client, pair), daemon=True).start()

    def _pump(self, src, dst, pair) -> None:
        try:
            while True:
                data = src.recv(65536)
                if not data:
                    break
                if self._mode == BLACKHOLE:
                    continue  # swallow it: the sender gets no error and no answer
                dst.sendall(data)
        except OSError:
            pass
        finally:
            with self._lock:
                self._pairs.discard(pair)
            self._rst(*pair)


def through(url: str, proxy: ChaosProxy) -> str:
    """The same connection URL, but pointed at the proxy instead of the real server."""
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(url)
    auth = f"{parts.username}:{parts.password}@" if parts.username else ""
    return urlunsplit((parts.scheme, f"{auth}127.0.0.1:{proxy.port}", parts.path, parts.query, ""))
