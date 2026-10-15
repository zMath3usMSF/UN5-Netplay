"""The `core` object of node.py for PCSX2 + the C filter in c/netpad_core.h, over PINE.

The in-game filter (NewPadRead -> np_filter) keeps one shared block, g_np (NpShared, 0x3E30 bytes). This module is
the other half: it reads the pad the game captured, writes the agreed inputs, reads the state hashes.

  base + 0x0000 header: magic, active, abort, status, frame, stalls, cur, players, dbg_calls, dbg_len,
                        dbg_port, dbg_skip, hold, idle, entering, live_state             (16 x u32)
  base + 0x0040 OUT[128]   32-byte entries: raw[24] | tag u32 (frame+1) | aux u32    captured local pad
  base + 0x1040 IN[0][128], base + 0x2040 IN[1][128]                                 agreed input per player
  base + 0x3040 HASH[128]  8-byte entries:  tag u32 (frame+1) | hash u32
  base + 0x3440 INFO       name[2][16] (ASCII, NUL padded) | ping[2] u32 (ms, 0xFFFFFFFF = unknown): only for the
                           on-screen display (Online.c), the lockstep never reads it.
  base + 0x3468 CFG        6 x u32 runtime config (hash mask, RNG address, range start, range end, flags, watch
                           address), see NetPad.c.
  base + 0x3480 SYNC       NpSync (0x968 bytes): RNG sync config, selection snapshot (characters / skin / colour /
                           map), rand diagnostics.
  base + 0x3DE8 FORCE      NpForce (0x48 bytes): role | stable | writes | pad | NpSel of the host (the guest follows
                           it).                                                          Total 0x3E30 bytes.

Commit protocol: data words first, the tag word LAST (an entry is valid only when tag == frame + 1).
`base` is the address of g_np in your link map.

captured() and hashes() first read ONE word (the tag of the first wanted frame): when nothing new is there (almost
every call of the pump loop) that is a single PINE read instead of ~300, which keeps the loop fast and the remote
input reaching the game sooner.

Host-authoritative selection: the HOST sends selection_words('cur') to the guest whenever sync_get('sel_seq')
changes; the GUEST calls follow_selection(words). The in-game np_sel_poll writes it into the menu slots (only on the
character / stage selection screen, only after the local selection was stable for NP_FORCE_STABLE polls).
When the fight starts, the in-game np_sync_scene clears force.sel.valid; follow_selection() notices that (the valid
word reads 0) and re-arms the same words when the host sends them again.
"""
import struct

import json

from pine import Pine, same_serial

SLOTS = 128
ENTRY = 32
HDR = dict(magic=0x00, active=0x04, abort=0x08, status=0x0C, frame=0x10, stalls=0x14, cur=0x18, players=0x1C,
           dbg_calls=0x20, dbg_len=0x24, dbg_port=0x28, dbg_skip=0x2C, hold=0x30,
           idle=0x34, entering=0x38, live_state=0x3C)
OUT, IN0, IN1, HASH, INFO, SYNC, FORCE, SIZE = 0x0040, 0x1040, 0x2040, 0x3040, 0x3440, 0x3480, 0x3DE8, 0x3E30
FORCE_ROLE, FORCE_STABLE, FORCE_WRITES, FORCE_SEL = FORCE + 0x00, FORCE + 0x04, FORCE + 0x08, FORCE + 0x10
CFG = INFO + 0x28                          # NpInfo.reserved[6]: runtime config written by netplay.py
NAME_LEN = 16                              # INFO: name[2][16] | ping[2] (u32) | 24 reserved bytes
PING_UNKNOWN = 0xFFFFFFFF
NP_MAGIC = 0x3150504E
REPORT_SIG = 0x3152504E                     # 'NPR1': first word of g_np_report in NetPad.c
SERIAL = 'SLES-55605'

# NpSync field offsets (relative to SYNC), see netpad_core.h
SY = dict(seed=0x00, rng_mode=0x04, rng_addr=0x08, cos_mode=0x18, cos_lo=0x1C, cos_hi=0x3C, cos_state=0x5C,
          acc=0x64, sel_seq=0x68, sel_final_tag=0x6C, sel_cur=0x70, sel_final=0xA8, dg_ra=0xE0, dg_cnt=0x120,
          rngpre=0x160, rngseq=0x560, sel_hash=0x960, epoch=0x964)
SEL_NAMES = ('valid', 'col0', 'col1', 'row0', 'row1', 'chr0', 'chr1', 'skin0', 'skin1', 'color0', 'color1',
             'rnd0', 'rnd1', 'map')
COS_N, DIAG_N = 8, 16


def decode_selection(words):
    """14 words of an NpSel -> dict."""
    return dict(zip(SEL_NAMES, words))


def format_selection(sel):
    """One readable line: what each PC sees as the selection of the match (compare the two lines)."""
    if not sel['valid']:
        return 'no selection data (no menu objects were seen while the filter ran)'
    parts = []
    for p in (0, 1):
        if sel['valid'] >> p & 1:
            parts.append(f"P{p + 1}: char={sel[f'chr{p}']} skin={sel[f'skin{p}']} color={sel[f'color{p}']} "
                         f"(col={sel[f'col{p}']} row={sel[f'row{p}']} random={sel[f'rnd{p}']})")
        else:
            parts.append(f'P{p + 1}: ?')
    parts.append(f"map={sel['map']}" if sel['valid'] & 4 else 'map=?')
    return '  '.join(parts)


def symbol_address(name='g_np', path='temp_build/build/symbols.json'):
    """Address of a symbol of the injected code, from the symbols.json that the build script writes (see the notes:
    three lines after collect_all_symbols). Accepts '0x..' strings or plain ints."""
    with open(path, 'r', encoding='utf-8') as f:
        table = json.load(f)
    if name not in table:
        raise KeyError(f'{name} is not in {path} (is NetPad.o in linker.asm and was the build run?)')
    value = table[name]
    return int(value, 16) if isinstance(value, str) else int(value)


def resolve_base(pine, path='temp_build/build/symbols.json'):
    """The REAL address of g_np, as reported by the running code (NewPadRead stores &g_np in g_np_report).
    Returns (base, script_base, calls): script_base is what the build script computed for g_np (they can differ if
    the script's section layout is not armips' one). Raises RuntimeError with the reason when it cannot tell."""
    rep = symbol_address('g_np_report', path)
    sig, addr, size, calls = [pine.read(rep + 4 * i, 4) for i in range(4)]
    try:
        script_base = symbol_address('g_np', path)
    except KeyError:
        script_base = None
    if sig != REPORT_SIG:
        raise RuntimeError(f'g_np_report at 0x{rep:08X} holds 0x{sig:08X}, not the NetPad signature: the build '
                           f'was not applied to this PCSX2, or .data is laid out differently from the script')
    if not addr or not calls:
        raise RuntimeError('NewPadRead has not run yet (calls = 0): get into a state where the game reads the pads '
                           '(a menu or a fight) and try again')
    if size != SIZE:
        raise RuntimeError(f'the game was built with a g_np of 0x{size:X} bytes, this tool expects 0x{SIZE:X}: '
                           f'rebuild with the current netpad_core.h')
    return addr, script_base, calls


class PineCore:
    def __init__(self, base, slot=28011, pine=None, serial=SERIAL):
        self.base = base
        self._force_last = None
        self.pine = pine or Pine(slot)
        if self.pine.sock is None and pine is None:
            self.pine.connect()
        if serial and pine is None and not same_serial(self.pine.game_id(), serial):
            raise RuntimeError(f'running game is {self.pine.game_id()!r}, expected {serial!r}')

    # ---- low level ----------------------------------------------------------------------------------------
    def _read64(self, addrs):
        out = []
        for i in range(0, len(addrs), 200):
            out += self.pine.batch([('r', a, 8) for a in addrs[i:i + 200]])
        return out

    def _write64(self, writes):
        for i in range(0, len(writes), 100):
            self.pine.batch([('w', a, v, 8) for a, v in writes[i:i + 100]])

    def get(self, name):
        return self.pine.read(self.base + HDR[name], 4)

    def put(self, name, value):
        self.pine.write(self.base + HDR[name], value, 4)

    # ---- control --------------------------------------------------------------------------------------------
    def start(self):
        """Call AFTER both PCs loaded the same savestate (loading it overwrites the block with old contents)."""
        self.put('active', 0)
        self._write64([(self.base + off, 0) for off in range(0, SIZE, 8)])      # clean rings, counters, flags
        self._force_last = None
        self.put('players', 2)
        self.put('magic', NP_MAGIC)

    def arm(self):
        """Arm the filter in HOLD: zero the block, set the magic, hold = 1, active = 1. The game stalls at its next pad
        read (frame 0) and waits forever. Runtime hold=2 is reserved for the tick barrier.
        SAVE THE STATE NOW: it holds a frozen, armed game that every player loads."""
        self.put('active', 0)
        self._write64([(self.base + off, 0) for off in range(0, SIZE, 8)])
        self._force_last = None
        self.put('players', 2)
        self.put('hold', 1)
        self.put('magic', NP_MAGIC)
        self.put('active', 1)

    def frozen(self):
        """True when the game sits in the first stall of an armed match (what a loaded match state looks like)."""
        return (self.get('magic') == NP_MAGIC and self.get('active') == 1 and self.get('hold') == 1
                and self.get('abort') == 0 and self.get('frame') >= 1)

    def release(self):
        """The match is going: the stall timeout counts again."""
        self.put('stalls', 0)
        self.put('hold', 0)

    def activate(self):
        """After the neutral frames 0..delay-1 were published (node.py does it on its first pump)."""
        self.put('active', 1)

    def stop(self):
        self.put('active', 0)

    def set_config(self, mask=0, rng=0, start=0, end=0, flags=0, watch=0):
        """Runtime knobs of the in-game hash (see NetPad.c): hash mask (0 = compiled default), RNG address, extra
        RAM range [start, end), flags (bit 0: resync the RNG at every scene start), watch address (its value travels
        in the aux word, so node.py's TICK lines show it for both PCs). Write it BEFORE the freeze is released, with
        the same values on both PCs."""
        for i, v in enumerate((mask, rng, start, end, flags, watch)):
            self.pine.write(self.base + CFG + 4 * i, int(v) & 0xFFFFFFFF, 4)

    # ---- sync block (RNG + selection) ---------------------------------------------------------------------
    def set_sync(self, seed, rng_mode, rng_addrs=(), cos_mode=0, cos_ranges=()):
        """Write the NpSync config. Same values on both PCs, BEFORE the freeze is released.
        rng_mode: bits 0-1 = 0 off / 1 reseed at scene entry / 2 = 1 + every frame; bit 2 (4) = mix the selection hash
        into the state hash; bit 3 (8) = mix the logical rand call sequence into it (diagnosis); bit 4 (16) = one
        private rand stream per call site (cosmetic callers only disturb themselves).
        Suggested: 18 (= 2 | 16) to play, 26 (= 2 | 8 | 16) to diagnose.
        rng_addrs: up to 4 extra 64-bit LCG state addresses (the main one, 0x00412B90, is always handled).
        cos_ranges: up to 8 (lo, hi) ranges of return addresses whose rand calls use a private state (per PC)."""
        w = lambda off, v: self.pine.write(self.base + SYNC + off, int(v) & 0xFFFFFFFF, 4)
        w(SY['seed'], seed)
        w(SY['rng_mode'], rng_mode)
        addrs = list(rng_addrs)[:4]
        for i in range(4):
            w(SY['rng_addr'] + 4 * i, addrs[i] if i < len(addrs) else 0)
        ranges = list(cos_ranges)[:COS_N]
        w(SY['cos_mode'], 1 if (cos_mode and ranges) else 0)
        for i in range(COS_N):
            lo, hi = ranges[i] if i < len(ranges) else (0, 0)
            w(SY['cos_lo'] + 4 * i, lo)
            w(SY['cos_hi'] + 4 * i, hi)

    def sync_get(self, name):
        return self.pine.read(self.base + SYNC + SY[name], 4)

    def selection_words(self, which='cur'):
        """The 14 raw words of the NpSel snapshot ('cur' = last seen in the menus, 'final' = locked at scene entry).
        HOST: this is what is sent to the guest."""
        off = SY['sel_final'] if which == 'final' else SY['sel_cur']
        words = []
        for w in self._read64([self.base + SYNC + off + 8 * i for i in range(7)]):
            words += struct.unpack('<II', w.to_bytes(8, 'little'))
        return words

    def selection(self, which='final'):
        """The selection snapshot ('final' = locked at scene entry, 'cur' = last seen) as a dict (SEL_NAMES)."""
        return decode_selection(self.selection_words(which))

    # ---- host-authoritative selection ---------------------------------------------------------------------
    def follow_selection(self, words):
        """GUEST: make the game use the HOST's selection (the 14 words of NpSel from the host's selection_words()).
        The in-game np_sel_poll writes it into the menu slots, only on the right screen and only once the local
        selection has been stable for NP_FORCE_STABLE polls. Returns True when something was written.
        The game clears force.sel.valid when a fight starts: if the words are the same as last time but the game no
        longer has them armed (valid reads 0), they are written again, so the next selection screen follows the host."""
        words = [int(w) & 0xFFFFFFFF for w in words]
        if len(words) != 14:
            raise ValueError('an NpSel has 14 words')
        if not words[0]:
            return False                                    # nothing selected on the host yet
        if words == self._force_last and self.pine.read(self.base + FORCE_SEL, 4) != 0:
            return False                                    # same words and still armed in the game
        w = lambda off, v: self.pine.write(self.base + off, v, 4)
        w(FORCE_SEL, 0)                                     # valid = 0 while the other words change
        for i in range(1, 14):
            w(FORCE_SEL + 4 * i, words[i])
        w(FORCE_SEL, words[0])                              # valid LAST
        w(FORCE_ROLE, 1)
        self._force_last = words
        return True

    def force_off(self):
        """Stop following the host (role = 0)."""
        self.pine.write(self.base + FORCE_ROLE, 0, 4)
        self._force_last = None

    def force_info(self):
        """role, polls since the local selection changed, and how many slot fields were overwritten with the
        host's value (0 in a normal match: both PCs already agree)."""
        return dict(role=self.pine.read(self.base + FORCE_ROLE, 4),
                    stable=self.pine.read(self.base + FORCE_STABLE, 4),
                    writes=self.pine.read(self.base + FORCE_WRITES, 4))

    def rng_diag(self):
        """[(return address, calls)] of every rand call site seen since the session started (compare both PCs: a
        call site whose count grows at a different rate on each PC is a cosmetic caller). Idle frames (intro,
        cutscenes, between rounds) are not counted."""
        base = self.base + SYNC
        ras = self._read64([base + SY['dg_ra'] + 8 * i for i in range(DIAG_N // 2)])
        cnt = self._read64([base + SY['dg_cnt'] + 8 * i for i in range(DIAG_N // 2)])
        out = []
        for r, c in zip(ras, cnt):
            ra = struct.unpack('<II', r.to_bytes(8, 'little'))
            n = struct.unpack('<II', c.to_bytes(8, 'little'))
            out += [(a, k) for a, k in zip(ra, n) if a]
        return out

    def health(self):
        """Counters of the in-game filter. idle = 1: no fighters now (the frame counter is paused);
        live_state bit 0 = the filter has seen the fighters alive (the scene barrier is armed)."""
        return {k: self.get(k) for k in ('active', 'abort', 'status', 'frame', 'stalls',
                                         'dbg_calls', 'dbg_len', 'dbg_port', 'dbg_skip', 'idle', 'live_state')}

    # ---- node.py interface ------------------------------------------------------------------------------------
    def captured(self, first, limit=64):
        """{frame: (raw24, aux)} of the local pad the game captured, contiguous from `first`."""
        # fast path: one read, the tag word of frame `first`. Nothing new -> done (this is almost every call).
        head = self._read64([self.base + OUT + (first % SLOTS) * ENTRY + 24])[0]
        if struct.unpack('<II', head.to_bytes(8, 'little'))[0] != first + 1:
            return {}
        out = {}
        frames = list(range(first, first + min(limit, SLOTS)))
        addrs = [self.base + OUT + (f % SLOTS) * ENTRY + w * 8 for f in frames for w in range(4)]
        words = self._read64(addrs)
        for n, f in enumerate(frames):
            w = words[n * 4:n * 4 + 4]
            tag, aux = struct.unpack('<II', w[3].to_bytes(8, 'little'))
            if tag != f + 1:
                break
            out[f] = (b''.join(x.to_bytes(8, 'little') for x in w[:3]), aux)
        return out

    def publish(self, slot, frames):
        """Commit {frame: (raw24, aux)} into IN[slot]: every data word first, every tag+aux word last."""
        ring = self.base + (IN0 if slot == 0 else IN1)
        data, commit = [], []
        for f, (raw, aux) in sorted(frames.items()):
            addr = ring + (f % SLOTS) * ENTRY
            raw = bytes(raw).ljust(24, b'\0')[:24]
            data += [(addr + 8 * i, int.from_bytes(raw[8 * i:8 * i + 8], 'little')) for i in range(3)]
            commit.append((addr + 24, struct.unpack('<Q', struct.pack('<II', (f + 1) & 0xFFFFFFFF,
                                                                     aux & 0xFFFFFFFF))[0]))
        self._write64(data)
        self._write64(commit)

    def hashes(self, first, limit=32):
        # fast path: one read, the entry of frame `first`. Nothing new -> done.
        head = self._read64([self.base + HASH + (first % SLOTS) * 8])[0]
        if struct.unpack('<II', head.to_bytes(8, 'little'))[0] != first + 1:
            return {}
        out = {}
        frames = list(range(first, first + min(limit, SLOTS)))
        words = self._read64([self.base + HASH + (f % SLOTS) * 8 for f in frames])
        for f, w in zip(frames, words):
            tag, h = struct.unpack('<II', w.to_bytes(8, 'little'))
            if tag != f + 1:
                break
            out[f] = h
        return out

    def set_info(self, names, pings):
        """Nicks and ping (ms) of player 1 / player 2 for the on-screen display. names: two str (ASCII, cut to 15
        characters), pings: two ints (0xFFFFFFFF = unknown)."""
        raw = b''
        for name in list(names)[:2] + [''] * (2 - len(names)):
            raw += name.encode('ascii', 'replace')[:NAME_LEN - 1].ljust(NAME_LEN, b'\0')
        raw += struct.pack('<II', *[int(p) & 0xFFFFFFFF for p in (list(pings) + [PING_UNKNOWN] * 2)[:2]])
        self._write64([(self.base + INFO + 8 * i, int.from_bytes(raw[8 * i:8 * i + 8], 'little'))
                       for i in range(len(raw) // 8)])

    def close(self):
        self.pine.close()