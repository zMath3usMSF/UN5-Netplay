"""Simulated game for the tests: the np_filter state machine of c/netpad_core.h on a fake PINE memory."""
import hashlib
import struct

import core_link as C

BASE, DELAY = 0x00700000, 3


def tagword(f, aux=0):
    return struct.unpack('<Q', struct.pack('<II', f + 1, aux))[0]


class SimGame:
    """c/netpad_core.h np_filter, one phase per step(): p0 (capture) -> w0 (wait IN[0]) -> w1 (wait IN[1]) -> p0 ..."""

    def __init__(self, p, pad_fn, limit=200):
        self.p, self.pad_fn, self.limit = p, pad_fn, limit
        self.phase, self.spins, self.hist, self.applied, self.raw0 = 'p0', 0, b'', [], None

    def h(self, name): return self.p.read(BASE + C.HDR[name], 4)
    def seth(self, name, v): self.p.write(BASE + C.HDR[name], v, 4)
    def word(self, addr): return self.p.m.get(addr, 0)

    def step(self):
        if self.h('magic') != C.NP_MAGIC or not self.h('active') or self.h('abort'):
            return
        if self.phase == 'p0':
            f = self.h('frame')
            self.seth('cur', f)
            digest = hashlib.sha256(self.hist + struct.pack('<I', f)).digest()[:4]
            self.p.m[BASE + C.HASH + (f % 128) * 8] = struct.unpack('<Q', struct.pack('<I', f + 1) + digest)[0]
            raw = self.pad_fn(f)
            e = BASE + C.OUT + (f % 128) * 32
            for i in range(3):
                self.p.m[e + 8 * i] = int.from_bytes(raw[8 * i:8 * i + 8], 'little')
            self.p.m[e + 24] = tagword(f)
            self.seth('frame', f + 1)
            self.phase, self.spins = 'w0', 0
            return
        port = 0 if self.phase == 'w0' else 1
        ring = BASE + (C.IN0 if port == 0 else C.IN1)
        f = self.h('cur')
        e = ring + (f % 128) * 32
        if self.word(e + 24) != tagword(f):
            self.spins += 1
            self.seth('stalls', self.h('stalls') + 1)
            if not self.h('hold') and self.spins > self.limit:
                self.seth('status', 1)
                self.seth('abort', 1)
            return
        raw = b''.join(self.word(e + 8 * i).to_bytes(8, 'little') for i in range(3))
        if port == 0:
            self.raw0, self.phase = raw, 'w1'
        else:
            self.applied.append((f, self.raw0, raw))
            self.hist = hashlib.sha256(self.hist + self.raw0 + raw).digest()
            self.phase = 'p0'


def pad_of(machine):
    return lambda f: bytes([0x00, 0x79, (machine * 60 + f * 3) & 0xFF, 0xFF, 0x7F, 0x7F, 0x7F, 0x7F]) + bytes(16)