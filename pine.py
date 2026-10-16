"""Minimal PCSX2 PINE client.

Packet:  u32 total_size (includes these 4 bytes) | commands...
Command: u8 opcode | args (little endian)
Reply:   u32 total_size | u8 result (0 = ok, 0xFF = fail) | data...

Enable in PCSX2: Settings > Advanced > Enable PINE (default slot 28011).
Windows: TCP 127.0.0.1:<slot>.  Linux: unix socket $XDG_RUNTIME_DIR/pcsx2.sock (or /tmp/pcsx2.sock).
"""
import os
import socket
import struct

READ8, READ16, READ32, READ64 = 0, 1, 2, 3
WRITE8, WRITE16, WRITE32, WRITE64 = 4, 5, 6, 7
STATUS = 0xF  # reply u32: 0 running, 1 paused, 2 shutdown
SAVE_STATE, LOAD_STATE, GAME_ID = 0x9, 0xA, 0xC   # u8 slot / u8 slot / reply: u32 length + string

_SIZES = {READ8: 1, READ16: 2, READ32: 4, READ64: 8}


def same_serial(a, b):
    """'SLES_556.05' (ELF name, what PCSX2 reports) == 'SLES-55605' (disc id): compare letters and digits only."""
    norm = lambda x: ''.join(c for c in x.upper() if c.isalnum())
    return norm(a) == norm(b)


class PineError(Exception):
    pass


BUSY_HINT = ('PCSX2 did not answer. Most likely ANOTHER program is already connected to PINE (PCSX2 serves one client '
             'at a time): an old PowerShell window still running a script of this kit or probe_un5.py, or gen_pnach.py. '
             'Close them (Ctrl+C), or find them with:  netstat -ano | findstr 28011   and   Get-Process python')


class Pine:
    def __init__(self, slot=28011, timeout=5.0):
        self.slot = slot
        self.timeout = timeout
        self.sock = None

    def connect(self):
        if os.name == 'nt':
            try:
                s = socket.create_connection(('127.0.0.1', self.slot), timeout=self.timeout)
            except ConnectionRefusedError:
                raise PineError(f'nothing listens on PINE port {self.slot}: is PCSX2 open with a game running and '
                                f'Settings > Advanced > Enable PINE ticked (slot {self.slot})?') from None
        else:
            base = os.environ.get('XDG_RUNTIME_DIR', '/tmp')
            path = os.path.join(base, 'pcsx2.sock')
            if self.slot != 28011:
                path += f'.{self.slot}'
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(self.timeout)
            s.connect(path)
        s.settimeout(self.timeout)                       # never wait forever for a PCSX2 that does not answer
        if os.name == 'nt':
            s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.sock = s
        return self

    def close(self):
        if self.sock:
            self.sock.close()
            self.sock = None

    def _recv_exact(self, n):
        buf = b''
        while len(buf) < n:
            try:
                chunk = self.sock.recv(n - len(buf))
            except socket.timeout:
                raise PineError(BUSY_HINT) from None
            if not chunk:
                raise PineError('PCSX2 closed the PINE connection')
            buf += chunk
        return buf

    def _transact(self, body):
        self.sock.sendall(struct.pack('<I', len(body) + 4) + body)
        size = struct.unpack('<I', self._recv_exact(4))[0]
        reply = self._recv_exact(size - 4)
        if reply[0] != 0:
            raise PineError('PINE command failed (bad address or no game running)')
        return reply[1:]

    # ---- single operations ------------------------------------------------------------------
    def read(self, addr, size=4):
        op = {1: READ8, 2: READ16, 4: READ32, 8: READ64}[size]
        data = self._transact(struct.pack('<BI', op, addr))
        return int.from_bytes(data, 'little')

    def write(self, addr, value, size=4):
        op = {1: WRITE8, 2: WRITE16, 4: WRITE32, 8: WRITE64}[size]
        fmt = {1: '<BIB', 2: '<BIH', 4: '<BII', 8: '<BIQ'}[size]
        self._transact(struct.pack(fmt, op, addr, value))

    def status(self):
        return int.from_bytes(self._transact(bytes([STATUS])), 'little')

    # NOTE: the three calls below follow the PINE spec as I know it; test them against your PCSX2 build.
    def game_id(self):
        """Serial of the running game, e.g. 'SLES-55605' (use it to refuse writing into the wrong game)."""
        data = self._transact(bytes([GAME_ID]))
        n = int.from_bytes(data[:4], 'little')
        return data[4:4 + n].split(b'\0')[0].decode('ascii', 'replace')

    def game_crc(self):
        """CRC do jogo aberto, como o PCSX2 mostra (PINE MsgUUID, 0xD), ex.: 'C071D4C1'."""
        data = self._transact(bytes([0xD]))
        n = int.from_bytes(data[:4], 'little')
        crc = data[4:4 + n].split(b'\0')[0].decode('ascii', 'replace').strip().upper()
        return crc[2:] if crc.startswith('0X') else crc

    def save_state(self, slot):
        self._transact(bytes([SAVE_STATE, slot]))

    def load_state(self, slot):
        self._transact(bytes([LOAD_STATE, slot]))

    # ---- batch: several commands in one round trip (important for speed) --------------------
    def batch(self, ops):
        """ops: list of ('r', addr, size) or ('w', addr, value, size). Returns the list of read values."""
        body, reads = b'', []
        for op in ops:
            if op[0] == 'r':
                _, addr, size = op
                body += struct.pack('<BI', {1: READ8, 2: READ16, 4: READ32, 8: READ64}[size], addr)
                reads.append(size)
            else:
                _, addr, value, size = op
                fmt = {1: '<BIB', 2: '<BIH', 4: '<BII', 8: '<BIQ'}[size]
                body += struct.pack(fmt, {1: WRITE8, 2: WRITE16, 4: WRITE32, 8: WRITE64}[size], addr, value)
        data = self._transact(body)
        out, pos = [], 0
        for size in reads:
            out.append(int.from_bytes(data[pos:pos + size], 'little'))
            pos += size
        return out


if __name__ == '__main__':                      # python pine.py : is PINE reachable and answering?
    import sys
    slot = int(sys.argv[1]) if len(sys.argv) > 1 else 28011
    try:
        p = Pine(slot).connect()
        names = {0: 'running', 1: 'paused', 2: 'no game / shutting down'}
        print(f'PINE ok on port {slot}: PCSX2 is {names.get(p.status(), "?")}, game = {p.game_id()!r}')
        p.close()
    except (PineError, OSError) as error:
        print(f'PINE problem: {error}')
        sys.exit(1)