"""Match-state transfer: the host serves its .p2s over TCP, the guest downloads it when its own file differs.

The TCP port is the SAME number as the host's UDP game port (47400 by default); TCP and UDP are separate, so they do
not collide. On the internet the host must forward TCP 47400 as well as UDP 47400 (a VPN such as Radmin needs nothing).

Wire format (all little endian):
  guest -> host   b'UN5S' + 16 ASCII bytes: the tag (first 16 hex digits of sha256) of the guest's file, or 16 x '-'
  host  -> guest  b'UN5S' | u8 status | 16 ASCII bytes: the host's tag | u64 size      (29 bytes)
                  status 0: same file, nothing follows.   status 1: `size` bytes of the file follow.
The guest checks the sha256 of what it received against the tag, writes to '<dest>.part' and only then replaces dest.
"""
import hashlib
import os
import socket
import struct
import threading

MAGIC = b'UN5S'
HEADER = struct.Struct('<4sB16sQ')
NO_TAG = b'-' * 16
CHUNK = 1 << 16
MAX_SIZE = 1 << 30                     # 1 GiB: a PCSX2 state is far smaller, anything bigger is not ours


class StateError(Exception):
    pass


def file_tag(path):
    """Same id as netplay.state_tag: first 16 hex digits of the sha256 of the file."""
    digest = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            digest.update(chunk)
    return digest.hexdigest()[:16]


def _recv_exact(sock, n):
    data = b''
    while len(data) < n:
        chunk = sock.recv(n - len(data))
        if not chunk:
            raise StateError('the connection closed too early')
        data += chunk
    return data


class StateServer:
    """Serves one file to any guest that asks. Raises OSError from the constructor when the port cannot be bound."""

    def __init__(self, path, port, bind='0.0.0.0'):
        self.path, self.port = path, port
        self.tag = file_tag(path)
        self.size = os.path.getsize(path)
        self.served = 0
        self._stop = threading.Event()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if os.name != 'nt':                              # on Windows SO_REUSEADDR would let another program share the port
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self.sock.bind((bind, port))
            self.sock.listen(4)
        except OSError:
            self.sock.close()
            raise
        self.sock.settimeout(0.5)
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while not self._stop.is_set():
            try:
                conn, _addr = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn):
        try:
            conn.settimeout(15)
            req = _recv_exact(conn, 20)
            if req[:4] != MAGIC:
                return
            same = req[4:20] == self.tag.encode('ascii')
            conn.sendall(HEADER.pack(MAGIC, 0 if same else 1, self.tag.encode('ascii'), 0 if same else self.size))
            if same:
                return
            conn.settimeout(60)
            left = self.size
            with open(self.path, 'rb') as f:
                while left > 0:
                    chunk = f.read(min(CHUNK, left))
                    if not chunk:
                        return
                    conn.sendall(chunk)
                    left -= len(chunk)
            self.served += 1
        except (OSError, StateError):
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def close(self):
        self._stop.set()
        try:
            self.sock.close()
        except OSError:
            pass


def fetch(host, port, dest, have_tag=None, say=print):
    """Ask the host's StateServer for its file. Returns ('same', tag) when `have_tag` already matches (nothing is
    written) or ('downloaded', tag) after dest was replaced by the verified file. Raises StateError."""
    part = dest + '.part'
    try:
        sock = socket.create_connection((host, port), timeout=10)
    except OSError as error:
        raise StateError(f'cannot reach the host state server at {host}:{port} (TCP): {error}') from None
    try:
        with sock:
            sock.settimeout(30)
            sock.sendall(MAGIC + (have_tag.encode('ascii')[:16].ljust(16, b'-') if have_tag else NO_TAG))
            magic, status, tag_b, size = HEADER.unpack(_recv_exact(sock, HEADER.size))
            if magic != MAGIC:
                raise StateError(f'{host}:{port} is not a UN5 state server')
            tag = tag_b.decode('ascii', 'replace')
            if status == 0:
                return 'same', tag
            if not 0 < size <= MAX_SIZE:
                raise StateError(f'the host announced an invalid size ({size})')
            os.makedirs(os.path.dirname(os.path.abspath(dest)), exist_ok=True)
            digest, got, report = hashlib.sha256(), 0, 20
            say(f'downloading the host match state: {size / 1_048_576:.1f} MB ...')
            with open(part, 'wb') as f:
                while got < size:
                    chunk = sock.recv(min(CHUNK, size - got))
                    if not chunk:
                        raise StateError('the connection closed before the whole file arrived')
                    f.write(chunk)
                    digest.update(chunk)
                    got += len(chunk)
                    if got * 100 // size >= report and report < 100:
                        say(f'downloading the host match state: {report}%')
                        report += 20
        if digest.hexdigest()[:16] != tag:
            raise StateError('the downloaded file is corrupted (tag mismatch); try again')
        os.replace(part, dest)
        return 'downloaded', tag
    except OSError as error:
        raise StateError(f'download interrupted: {error}') from None
    finally:
        if os.path.exists(part):
            try:
                os.remove(part)
            except OSError:
                pass