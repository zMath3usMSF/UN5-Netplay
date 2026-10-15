"""Single-PC self test of the in-game filter (no network): checks the hook, then runs a LOOPBACK lockstep.

  python selftest_netpad.py --check      read only: address of g_np, magic, is the pad read really replaced?
  python selftest_netpad.py --diag       read only: which addresses the COMPILED NewPadRead really uses, symbol layout,
                                         header words. Use it when the loopback shows frame=0.
  python selftest_netpad.py --delay 1 --control p2   example: low lag, your pad drives player 2
  python selftest_netpad.py              run it: player 1's pad is captured by the game, sent back to the game
                                         `delay` frames later, player 2 stays idle. Be in a versus fight (both
                                         pads connected in PCSX2). Ctrl+C gives the controls back.

What to expect: with --control p1 (default) P1 answers your pad with `delay` frames of lag and P2 does nothing
(by design: only pad 1 is captured; use --control p2 or both to see the P2 path), the game never freezes, `stalls`
stays ~0 and the status line shows the logic frame rate (frames/s).
"""
import argparse
import struct
import sys
import time

import core_link
import wire
from pine import Pine, PineError

PAD_READ = 0x001755B0                     # FUN_001755b0, replaced by `j NewPadRead`


def hook_status(pine, symbols_path):
    word = pine.read(PAD_READ, 4)
    is_j = (word >> 26) == 2
    target = ((word & 0x03FFFFFF) << 2) | (PAD_READ & 0xF0000000) if is_j else None
    expected = None
    try:
        expected = core_link.symbol_address('NewPadRead', symbols_path)
    except (OSError, KeyError):
        pass
    return word, is_j, target, expected


MEM_OPS = {0x20, 0x21, 0x23, 0x24, 0x25, 0x28, 0x29, 0x2B, 0x1E, 0x1F, 0x37, 0x3F, 0x31, 0x39}   # lb..sd, lq/sq, lwc1/swc1


def referenced_addresses(code, lo=0x00900000, hi=0x00A00000):
    """Heuristic scan of MIPS code: addresses built with lui + addiu/ori, or lui + a load/store offset, inside
    [lo, hi). {address: count}. (It does not follow branches or invalidate registers: treat it as evidence.)"""
    regs, refs = {}, {}

    def note(a):
        if lo <= a < hi:
            refs[a] = refs.get(a, 0) + 1

    for i in range(0, len(code) - 3, 4):
        w = struct.unpack_from('<I', code, i)[0]
        op, rs, rt, imm = w >> 26, (w >> 21) & 31, (w >> 16) & 31, w & 0xFFFF
        simm = imm - 0x10000 if imm & 0x8000 else imm
        if op == 0x0F:                                              # lui
            regs[rt] = imm << 16
        elif op in (0x09, 0x0D) and rs in regs:                     # addiu / ori
            regs[rt] = ((regs[rs] + simm) & 0xFFFFFFFF) if op == 0x09 else (regs[rs] | imm)
            note(regs[rt])
        elif op in MEM_OPS and rs in regs:                          # load / store
            note((regs[rs] + simm) & 0xFFFFFFFF)
    return refs


def read_bytes(pine, addr, length):
    ops = [('r', addr + off, 8) for off in range(0, length, 8)]
    return b''.join(v.to_bytes(8, 'little') for v in pine.batch(ops))


def diag(pine, base, symbols_path):
    import json
    with open(symbols_path, 'r', encoding='utf-8') as f:
        table = {k: int(v, 16) if isinstance(v, str) else v for k, v in json.load(f).items()}
    mine = {k: v for k, v in table.items() if k in ('NewPadRead', 'g_np', 'np_filter', 'np_state_hash', 'np_player',
                                                    'np_fnv')}
    print('symbols of NetPad.o (from symbols.json):')
    for name, addr in sorted(mine.items(), key=lambda kv: kv[1]):
        print(f'   0x{addr:08X}  {name}')
    if 'NewPadRead' not in table:
        print('NewPadRead is not in symbols.json: is NetPad.o imported in linker.asm and was the build run?')
        return 1
    code = read_bytes(pine, table['NewPadRead'], 0x240)
    refs = referenced_addresses(code)
    end = base + core_link.SIZE
    inside = {a: n for a, n in refs.items() if base <= a < end}
    print(f'g_np range according to symbols.json: 0x{base:08X} .. 0x{end:08X}')
    print(f'addresses in 0x00900000..0x00A00000 that the code of NewPadRead builds ({len(refs)}):')
    for a in sorted(refs):
        print(f'   0x{a:08X}  x{refs[a]}' + ('   <- inside g_np' if a in inside else ''))
    if inside:
        print('OK: the compiled code uses the same g_np the tool writes to.')
    elif refs:
        low = min(refs)
        print(f'MISMATCH: the code does not touch the tool\'s g_np. Its lowest data reference is 0x{low:08X} '
              f'({low - base:+#x} from the tool\'s base): the layout armips used differs from the one the build '
              f'script printed.')
    else:
        print('inconclusive: no data address found in that code (NewPadRead might not be where the symbol says).')
    words = [pine.read(base + 4 * i, 4) for i in range(16)]
    print('g_np header words:', ' '.join(f'{w:08X}' for w in words))
    return 0


class Loopback:
    """Feeds the game's own captured pad back to it `delay` frames later (the lockstep with a single player)."""

    def __init__(self, core, delay=3, control='p1', resume=False):
        """control: which game player the physical pad 1 drives - 'p1', 'p2' or 'both'. The other one stays idle."""
        if control not in ('p1', 'p2', 'both'):
            raise ValueError(control)
        self.core, self.delay, self.control, self.next = core, delay, control, 0
        self.resume = resume          # True: start from a FROZEN armed match (netplay.py prepare + load): no zeroing

    def begin(self):
        c = self.core
        if self.resume:
            # The game waits at frame 0 and already captured frame 0 (stale pad of whoever made the state): ignore that
            # capture. Frames 0..delay are neutral, real input starts at frame delay + 1 (capture of frame 1).
            idle = {f: (wire.IDLE_PAD, 0) for f in range(self.delay + 1)}
            c.publish(0, idle)
            c.publish(1, idle)
            self.next = 1
            c.release()                                               # the stall timeout counts again
            return
        c.start()                                                     # zero the block, set the magic
        idle = {f: (wire.IDLE_PAD, 0) for f in range(self.delay)}     # frames 0..delay-1 are neutral
        c.publish(0, idle)
        c.publish(1, idle)
        c.activate()

    def step(self):
        """One non-blocking iteration; returns how many frames it fed."""
        caps = self.core.captured(self.next, 16)
        if not caps:
            return 0
        frames = sorted(caps)
        idle = (wire.IDLE_PAD, 0)
        self.core.publish(0, {f + self.delay: (caps[f] if self.control in ('p1', 'both') else idle) for f in frames})
        self.core.publish(1, {f + self.delay: (caps[f] if self.control in ('p2', 'both') else idle) for f in frames})
        self.next = frames[-1] + 1
        return len(frames)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('--symbols', default='temp_build/build/symbols.json')
    ap.add_argument('--slot', type=int, default=28011, help='PINE slot')
    ap.add_argument('--delay', type=int, default=3, help='frames of input delay (1 frame = 33 ms in this game)')
    ap.add_argument('--control', choices=('p1', 'p2', 'both'), default='p1',
                    help='which player your pad 1 drives (the other stays idle); default p1')
    ap.add_argument('--check', action='store_true')
    ap.add_argument('--diag', action='store_true')
    args = ap.parse_args(argv)

    try:
        pine = Pine(args.slot).connect()
        base, script_base, calls = core_link.resolve_base(pine, args.symbols)
    except (PineError, RuntimeError, KeyError, OSError) as error:
        print(f'cannot reach the game: {error}')
        return 1
    print(f'g_np is at 0x{base:08X} (reported by the running code; NewPadRead ran {calls} times)')
    if script_base is not None and script_base != base:
        print(f'  note: the build script says 0x{script_base:08X}  ->  the code is {script_base - base:+#x} from it. '
              f'The tool uses the code\'s address, so this is harmless here, but your script\'s layout for .bss/.rodata '
              f'does not match armips\'.')
    word, is_j, target, expected = hook_status(pine, args.symbols)
    if is_j:
        ok = expected is None or target == expected
        print(f'0x{PAD_READ:08X}: j 0x{target:08X}'
              + ('  -> NewPadRead: the pad read is replaced' if ok else f'  !! NewPadRead is at 0x{expected:08X}'))
    else:
        print(f'0x{PAD_READ:08X}: 0x{word:08X} is NOT a jump: the replacement is not installed '
              f'(.org 0x001755B0 / j NewPadRead / nop in linker.asm, then rebuild)')
    core = core_link.PineCore(base, pine=pine, serial=None)
    magic = core.get('magic')
    print(f'magic = 0x{magic:08X}  ({"filter armed" if magic == core_link.NP_MAGIC else "not started: normal for now"})')
    if args.diag:
        return diag(pine, base, args.symbols)
    if args.check or not is_j:
        return 0 if is_j else 1

    lb = Loopback(core, args.delay, args.control)
    lb.begin()
    print(f"after start(): magic=0x{core.get('magic'):08X} active={core.get('active')} players={core.get('players')}")
    print(f'loopback running, delay = {args.delay} frames ({args.delay * 33} ms), your pad drives {args.control}. Ctrl+C to stop.')
    t0, f0, last = time.time(), 0, time.time()
    try:
        while True:
            fed = lb.step()
            now = time.time()
            if now - last >= 1.0:
                h = core.health()
                rate = (h['frame'] - f0) / (now - t0)
                hashes = core.hashes(max(0, h['frame'] - 3), 3)
                last_hash = f'{hashes[max(hashes)]:#x}' if hashes else '-'
                print(f"frame={h['frame']:7d}  {rate:5.1f} frames/s  stalls={h['stalls']}  status={h['status']}  "
                      f"abort={h['abort']}  last hash={last_hash}")
                print(f"    pad read: calls={h['dbg_calls']} len={h['dbg_len']} port={h['dbg_port']} skip={h['dbg_skip']}"
                      f"   (skip: 0 filtered, 1 magic, 2 inactive, 3 abort, 4 port, 5 short copy)")
                t0, f0, last = now, h['frame'], now
                if h['abort']:
                    print('the filter aborted (stall timeout): the game is back on the real pads')
                    return 2
            if not fed:
                time.sleep(0.001)
    except KeyboardInterrupt:
        pass
    finally:
        core.stop()                                                   # active = 0: the real pads again
        print('stopped: the game reads the real pads again')
    return 0


if __name__ == '__main__':
    sys.exit(main())