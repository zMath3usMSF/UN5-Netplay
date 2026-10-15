"""Client side of the UN5 online server (relay_server.py): rooms, files, public address discovery and the relay wrapper.

Stdlib only. Used by netplay.py (host / guest --server) and by the launcher (room list).
"""
from __future__ import annotations

import hashlib
import http.client
import json
import os
import socket
import threading
import time
from urllib.parse import quote

MAGIC = b"UN5\xfe"
DEFAULT_PORT = 47500


class ServerError(Exception):
    pass


# ---- server address ----------------------------------------------------------------------------------------------
def parse_server(text, default_port=DEFAULT_PORT):
    """'host', 'host:port' or 'http://host:port' -> (host, port)."""
    text = str(text or "").strip()
    for prefix in ("http://", "https://"):
        if text.lower().startswith(prefix):
            text = text[len(prefix):]
    text = text.split("/")[0]
    if not text:
        raise ServerError("no server address")
    host, sep, port = text.rpartition(":")
    if not sep:
        return text, default_port
    if not host or not port.isdigit() or not 1 <= int(port) <= 65535:
        raise ServerError(f"bad server address {text!r} (use HOST or HOST:PORT)")
    return host, int(port)


def resolve(text):
    """(ip, port) of the server for UDP."""
    host, port = parse_server(text)
    try:
        return socket.gethostbyname(host), port
    except OSError as error:
        raise ServerError(f"cannot resolve {host}: {error}") from None


# ---- UDP socket, public address, relay wrapper ---------------------------------------------------------------------
def open_socket(bind_port=0, bind="0.0.0.0"):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind((bind, bind_port))
    if os.name == "nt":
        try:
            s.ioctl(socket.SIO_UDP_CONNRESET, False)    # no spurious WinError 10054 on recvfrom
        except (AttributeError, OSError):
            pass
    return s


def whoami(sock, server_addr, tries=6):
    """The public (ip, port) the server sees for THIS socket. The same socket must then be used for the match, so the
    NAT mapping that the other player will punch through is the one the server just saw."""
    nonce = os.urandom(16)
    packet = (MAGIC + b"W" + nonce).ljust(64, b"\0")
    old = sock.gettimeout()
    try:
        for _ in range(tries):
            sock.sendto(packet, server_addr)
            end = time.time() + 0.8
            while time.time() < end:
                sock.settimeout(max(0.05, end - time.time()))
                try:
                    data, addr = sock.recvfrom(2048)
                except socket.timeout:
                    break
                except OSError:
                    continue
                if addr == server_addr and data[:5] == MAGIC + b"W" and data[5:21] == nonce:
                    ip, _, port = data[21:].decode("ascii", "replace").rpartition(":")
                    return ip, int(port)
    finally:
        sock.settimeout(old)
    raise ServerError(f"the server did not answer on UDP {server_addr[0]}:{server_addr[1]} "
                      f"(is the UDP port open on the server?)")


def wrap(token, payload):
    """Datagram for the relay: it forwards `payload` to the other player of the room."""
    return MAGIC + b"F" + token + payload


def keepalive(token):
    return MAGIC + b"K" + token


def lan_candidates(port):
    """Private IPv4 addresses of this PC (ip, port): a player in the same home network can be reached directly."""
    ips = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("10.255.255.255", 1))
            ips.append(s.getsockname()[0])
        finally:
            s.close()
    except OSError:
        pass
    try:
        ips += socket.gethostbyname_ex(socket.gethostname())[2]
    except OSError:
        pass
    out = []
    for ip in ips:
        parts = ip.split(".")
        private = (ip.startswith("10.") or ip.startswith("192.168.")
                   or (ip.startswith("172.") and len(parts) > 1 and parts[1].isdigit() and 16 <= int(parts[1]) <= 31))
        if private and [ip, port] not in out:
            out.append([ip, port])
    return out[:6]


# ---- HTTP API ----------------------------------------------------------------------------------------------------
class _Progress:
    """File wrapper that reports the upload progress (http.client reads the body through .read)."""

    def __init__(self, f, size, say):
        self.f, self.size, self.say, self.done, self.next = f, size, say, 0, 0.1

    def read(self, n=-1):
        chunk = self.f.read(n)
        self.done += len(chunk)
        if self.say and self.size and self.done / self.size >= self.next:
            self.say(f"    uploaded {int(self.done * 100 / self.size)}%")
            self.next += 0.2
        return chunk


class Api:
    def __init__(self, server, timeout=8.0):
        self.https = str(server or "").strip().lower().startswith("https://")
        self.host, self.port = parse_server(server, 443 if self.https else DEFAULT_PORT)
        self.timeout = timeout

    def _conn(self, timeout=None):
        cls = http.client.HTTPSConnection if self.https else http.client.HTTPConnection
        return cls(self.host, self.port, timeout=timeout or self.timeout)

    def _req(self, method, path, body=None):
        conn = self._conn()
        try:
            data = json.dumps(body).encode("utf-8") if body is not None else None
            conn.request(method, path, body=data, headers={"Content-Type": "application/json"} if data else {})
            resp = conn.getresponse()
            raw = resp.read()
        except (OSError, http.client.HTTPException) as error:
            raise ServerError(f"cannot reach the server {self.host}:{self.port} ({error})") from None
        finally:
            conn.close()
        try:
            obj = json.loads(raw.decode("utf-8") or "{}")
        except ValueError:
            obj = {}
        if resp.status >= 400:
            raise ServerError(obj.get("error") or f"the server answered HTTP {resp.status}")
        return obj

    # rooms
    def list_rooms(self):
        return self._req("GET", "/rooms")["rooms"]

    def get_room(self, room_id):
        return self._req("GET", f"/rooms/{quote(room_id)}")

    def create_room(self, nick, delay, tag, sym, addr, lan):
        return self._req("POST", "/rooms", dict(nick=nick, delay=delay, tag=tag, sym=sym, addr=list(addr), lan=lan))

    def poll(self, room_id, token, status):
        return self._req("GET", f"/rooms/{quote(room_id)}/poll?token={token}&status={status}")

    def join_room(self, room_id, nick, addr, lan):
        return self._req("POST", f"/rooms/{quote(room_id)}/join", dict(nick=nick, addr=list(addr), lan=lan))

    def close_room(self, room_id, token):
        return self._req("DELETE", f"/rooms/{quote(room_id)}?token={token}")

    # files (content addressed: the name is the first 16 hex digits of the sha256)
    def blob_has(self, tag):
        conn = self._conn()
        try:
            conn.request("HEAD", f"/blob/{tag}")
            resp = conn.getresponse()
            resp.read()
            return resp.status == 200
        except (OSError, http.client.HTTPException) as error:
            raise ServerError(f"cannot reach the server {self.host}:{self.port} ({error})") from None
        finally:
            conn.close()

    def blob_put(self, tag, path, token, say=None):
        size = os.path.getsize(path)
        conn = self._conn(timeout=60)
        try:
            with open(path, "rb") as f:
                conn.request("PUT", f"/blob/{tag}?token={token}", body=_Progress(f, size, say),
                             headers={"Content-Length": str(size), "Content-Type": "application/octet-stream"})
                resp = conn.getresponse()
                raw = resp.read()
        except (OSError, http.client.HTTPException) as error:
            raise ServerError(f"upload failed ({error})") from None
        finally:
            conn.close()
        if resp.status >= 400:
            try:
                message = json.loads(raw.decode("utf-8")).get("error")
            except ValueError:
                message = None
            raise ServerError(message or f"upload failed (HTTP {resp.status})")

    def blob_get(self, tag, path, say=None, backup=False, json_check=False):
        """Download blob `tag` to `path` (atomically; the old file can be kept as <path>.bak)."""
        path = os.path.abspath(path)
        tmp = path + ".part"
        conn = self._conn(timeout=60)
        try:
            conn.request("GET", f"/blob/{tag}")
            resp = conn.getresponse()
            if resp.status != 200:
                resp.read()
                raise ServerError("the server does not have that file" if resp.status == 404
                                  else f"download failed (HTTP {resp.status})")
            size = int(resp.getheader("Content-Length") or 0)
            digest, done, next_mark = hashlib.sha256(), 0, 0.2
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(tmp, "wb") as f:
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    digest.update(chunk)
                    f.write(chunk)
                    done += len(chunk)
                    if say and size and done / size >= next_mark:
                        say(f"    downloaded {int(done * 100 / size)}%")
                        next_mark += 0.2
            if digest.hexdigest()[:16] != tag:
                raise ServerError("the download is corrupted (tag mismatch)")
            if json_check:
                with open(tmp, "rb") as f:
                    try:
                        json.loads(f.read().decode("utf-8"))
                    except ValueError:
                        raise ServerError("the downloaded file is not valid JSON") from None
            if backup and os.path.isfile(path):
                try:
                    import shutil
                    shutil.copyfile(path, path + ".bak")
                except OSError:
                    pass
            os.replace(tmp, path)
        except (OSError, http.client.HTTPException) as error:
            raise ServerError(f"download failed ({error})") from None
        finally:
            conn.close()
            try:
                os.unlink(tmp)
            except OSError:
                pass


class RoomPoller:
    """Host: keeps the room alive on the server (heartbeat) and learns when a guest joined. Runs in a thread."""

    def __init__(self, api, room_id, token):
        self.api, self.room_id, self.token = api, room_id, token
        self.status = "waiting"          # set by the caller: 'waiting' until the match runs, then 'playing'
        self.host_ip = ""                # our public ip: a guest with the same one is in our home network
        self.lost = False                # the server forgot the room (restart / timeout)
        self._seen_gen = 0
        self._guest = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self):
        while True:
            try:
                r = self.api.poll(self.room_id, self.token, self.status)
                self.lost = False
                g = r.get("guest")
                if g and g.get("gen") != self._seen_gen:
                    with self._lock:
                        self._guest = g
            except ServerError as error:
                if "room not found" in str(error):
                    self.lost = True
            if self._stop.wait(1.0 if self.status == "waiting" else 5.0):
                return

    def take_guest(self):
        """The newest guest that joined since the last call, or None."""
        with self._lock:
            g, self._guest = self._guest, None
        if g:
            self._seen_gen = g["gen"]
        return g

    def lan_cands(self, guest):
        if guest["addr"][0] == self.host_ip:
            return [tuple(c) for c in guest.get("lan", [])]
        return []

    def close(self):
        self._stop.set()
        try:
            self.api.close_room(self.room_id, self.token)
        except ServerError:
            pass