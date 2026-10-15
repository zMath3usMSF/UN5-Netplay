"""UN5 online, two PCs: delay-based lockstep over UDP (host = player 1, guest = player 2).

How a match starts (the game is frozen by the in-game filter, so nobody runs ahead):
  1. Creator, in a versus fight with both pads connected, at the moment the match should start:
         python netplay.py prepare --save-slot 1
     The filter is armed in HOLD: the game stalls at frame 0. The state saved NOW is a frozen, armed match.
     (If --save-slot does not work with your PCSX2, save it by hand with the save-state key while it is frozen.)
     Send the .p2s file of that slot to the other player (identical bytes on both PCs).
  2. Guest loads that state in PCSX2 (it freezes at frame 0 too). This tool can do it for you:
         python netplay.py load --state match.p2s --state-dir "C:\\...\\PCSX2\\sstates" --load-slot 9
     (it copies the match file over that PCSX2 slot and asks PCSX2, over PINE, to load the slot; the game must be
     running. Slot 9 gets OVERWRITTEN: use a slot you do not care about.)
  3. Both run, with the same --state file (the input delay is automatic, see below):
         host:   python netplay.py host  --state match.p2s --port 47400
         guest:  python netplay.py guest --state match.p2s --connect HOST_IP:47400
     Add  --state-dir DIR --load-slot 9  to either one and step 2 is done automatically before connecting.
     When both are connected the freeze is released and the fight starts on both screens.
  The host also SHARES its state file over TCP (same port number, 47400) and its symbols.json (TCP port + 2, 47402):
  a guest started with --fetch-state downloads them when its own files differ, so nobody has to send the .p2s or the
  symbols.json by hand (forward TCP 47400 and 47402 too). The match state carries the HOST's build of the game, so the
  guest must use the host's symbols.json: with --fetch-state the guest does not even need its own build.
  Over the internet the host needs UDP port 47400 reachable (port forward) - or use a VPN (Radmin / ZeroTier /
  Tailscale) and connect to the VPN address. Ctrl+C gives the real pads back.

  python netplay.py stop      gives the real pads back (also unfreezes a game that is stuck in the stall).

  INPUT DELAY (--delay N, default 0 = automatic):
     The delay is part of the session id, so both PCs must agree on it BEFORE they talk. Automatic mode:
       - host, online (--server): measures the ping to the server (best of 3) and picks the delay that hides the
         estimated host<->guest round trip (2 x that ping), between 2 and 10 frames. It is published in the room.
       - guest, online: always uses the delay of the room it joins.
       - LAN / VPN (--connect, no server): nothing to measure beforehand, so 3 frames.
     Give --delay N (N >= 1) on the host to force a value; on a LAN / VPN the guest must give the same N.

  RNG + selection sync (same on both PCs; the defaults are what you want):
     --rng-mode N       0 off, 1 reseed the game's RNG at the first live frame of every scene, 2 (default) = 1 + at
                        every frame. The seed comes from the match state tag, so both PCs use the same one.
     --rng-site N       1 (default): one private rand stream per call site, reseeded every frame: a cosmetic caller
                        (particles, camera ...) that is called a different number of times on each PC only disturbs
                        itself, never the logic. 0 = the old single shared stream. The SAME on both PCs.
     --sync-seed N      override that seed (the SAME on both PCs)
     --sync-hash WHAT   none | sel (default) | seq | both: extra terms mixed into the state hash. sel = the selection
                        (characters, skins, colours, map) each PC saw in the menus, so a different selection shows up
                        as a hash mismatch at once; seq = the sequence of "logical" rand calls (diagnosis: use it with
                        --ignore-desync on the host).
     --cos-range A:B    (repeatable, up to 8) return-address range of a function whose rand calls are purely
                        cosmetic: they use a private state of their own, so they cannot shift the shared one.
                        Example: --cos-range 0x743720:0x743900
     --rng-addr2 ...    (repeatable) extra 64-bit LCG state to reseed (only when it has the same format as 0x412B90)
     --no-force-sel     the guest does NOT follow the host's selection. By default the host sends the characters /
                        skins / colours / map it sees in the menus and the guest overwrites its own with them (only on
                        the character / stage selection screen, once its own selection has been stable for a moment).
                        A "SEL FORCED" line is printed on the guest every time that actually changes something.
     Every PC prints a "SEL ..." line when the fight starts (what it thinks was selected) and, when it stops, the
     table of rand call sites with their call counts: a call site that counts differently on the two PCs is a
     cosmetic caller (put its function in --cos-range).

  --tick-barrier       EXPERIMENTAL, off by default. The PC whose game tick is ahead parks for at most 150 ms (the game
                       drops the park by itself after ~3 s too). The lockstep is deterministic without it; it only
                       tries to keep the two game clocks close. Use the same setting on both PCs.

  Debugging a desync: add --ignore-desync to the HOST. Hash mismatches are printed but the match keeps going, so you can
  see whether the two screens really diverge or only the state hash differs. Diagnostic knobs (the SAME on both PCs):
     --hash-mask N      1 position, 2 0x68..0x80, 4 life/chakra, 8 timers, 16 RNG word, 32 extra RAM range
     --rng-addr 0x...   address of the game's RNG word (hashed with mask bit 16)
     --rng-resync       old NetPad.c flag: at the first live frame of every scene both PCs write the same value into the RNG
     --hash-range A:B   hash the RAM words in [A, B) too (at most 64 KB)
     --watch 0x...      the value of that RAM word travels with every frame: the TICK lines show it for both PCs
     --watch 10         (code, not an address) where each PC is in the battle flow: flow state / action / argument /
                        players present. --watch 9: action / argument / timer.
     --watch-log        print a WATCH line every time the watched value changes (host and guest side by side, decoded
                        for --watch 9 and 10). Use --watch 10 --watch-log on both PCs to see which screen each scene
                        transition belongs to.

  ONLINE mode, no Radmin / VPN / port forwarding: run relay_server.py on any machine with a public address (a free
  VM works) and give both PCs the same server:
         host:   python netplay.py host  --state match.p2s --server SERVER[:47500]       (publishes a room)
         guest:  python netplay.py guest --state match.p2s --server SERVER --room ID --fetch-state
  The server lists the rooms, hosts the DEFAULT match state and symbols.json published by the owner (host and guest download them), tells each PC the other's
  public address (UDP hole punching) and, when punching fails (CGNAT, symmetric NAT), relays the packets itself.
  The console prints "connection: direct" or "connection: relay".

Needs: the build with NetPad.o (symbols.json) and PCSX2 with PINE enabled.
"""
import argparse
import hashlib
import json
import os
import shutil
import socket
import sys
import threading
import time

import core_link
import netlink
import node as node_mod
import statesync
from pine import Pine, PineError

try:
    import rooms                                       # optional: without rooms.py the room list is simply not announced
except ImportError:
    rooms = None

GAME_SERIAL = 'SLES-55605'

SYNC_HASH_BITS = {'none': 0, 'sel': 4, 'seq': 8, 'both': 12}   # bits 2-3 of NpSync.rng_mode (see netpad_core.h)
RNG_SITE_BIT = 16                                              # bit 4 of NpSync.rng_mode: one rand stream per call site

LAN_DELAY = 3                                          # automatic delay when there is no server to measure against
AUTO_DELAY_MIN, AUTO_DELAY_MAX = 3, 10                 # clamp of the automatic delay (frames)


def describe_watch(value, code):
    """Readable form of the aux word for --watch 9 / 10 (see NetPad.c np_aux_value); any other watch: hex."""
    v = int(value) & 0xFFFFFFFF
    if code == 9:
        if v == 0xFFFFFFFF:
            return 'no battle'
        return f'action={v >> 24} arg={(v >> 16) & 0xFF} timer={v & 0xFFFF}'
    if code == 10:
        flow = v >> 24
        return (f"flow={'none' if flow == 0xFF else format(flow, '#x')} action={(v >> 16) & 0xFF} "
                f"arg={(v >> 8) & 0xFF} players={v & 3}")
    return f'0x{v:08X}'


GAME_GP = 0x617EF0
FLOW_PHASES = {0x7: 'select', 0x9: 'select', 0xF: 'fight'}   # 0xB..0xE = loading: keeps the previous phase


class GameFlow:
    """Tells the launcher where the game is by printing "phase: select" / "phase: fight" when it changes.
    Same flow state the game's GetFreeBattleStatus reads: *(*(gp-0x1D0)) = 7 character selection, 9 stage selection,
    0xB..0xE loading, 0xF in battle. The launcher forwards it to the server (room list). Never raises."""
    def __init__(self, pine):
        self.pine, self.last, self.ok = pine, None, True

    def _u32(self, addr):
        return self.pine.read(addr, 4) & 0xFFFFFFFF

    def state(self):
        fb = self._u32(GAME_GP - 0x1D0)
        if not 0x100000 <= fb < 0x2000000 or fb & 3:
            return None
        return self._u32(fb) & 0xFF

    def report(self):
        if not self.ok:
            return
        try:
            phase = FLOW_PHASES.get(self.state())
        except AttributeError as error:
            self.ok = False
            print(f'note: phase reporting is off ({error}); the room list will show "in fight" only.')
            return
        except Exception:                              # noqa: BLE001 - a failed read must never stop a match
            return
        if phase and phase != self.last:
            self.last = phase
            print(f'phase: {phase}', flush=True)


class NetplaySession:
    """One PC's side of the match: the Node (network) + the PineCore (game). step() is non-blocking."""

    def __init__(self, core, node):
        self.core, self.node, self.released = core, node, False

    def step(self):
        self.node.pump()
        if self.node.started and not self.released:
            self.core.release()                       # the stall timeout counts again, the match is running
            self.released = True
        return self.node.desync, self.node.closed_reason


def state_tag(path):
    """First 16 hex digits of the sha256 of the match state: both PCs must hold byte-identical files, otherwise the
    session ids differ and the PCs never talk to each other."""
    digest = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            digest.update(chunk)
    return digest.hexdigest()[:16]


def newest_state(directory, since):
    """The newest *.p2s of `directory` written after `since` (epoch seconds), or None."""
    import glob
    import os
    files = [f for f in glob.glob(os.path.join(directory, '*.p2s')) if os.path.getmtime(f) >= since]
    return max(files, key=os.path.getmtime) if files else None


def connect(args):
    try:
        pine = Pine(args.pine_slot).connect()
        base, script_base, calls = core_link.resolve_base(pine, args.symbols)
    except (PineError, RuntimeError, KeyError, OSError) as error:
        raise SystemExit(f'cannot reach the game: {error}')
    return pine, core_link.PineCore(base, pine=pine, serial=None)

def stop_requested(args):
    path = getattr(args, 'stop_file', None)
    return bool(path and os.path.isfile(path))

def _fine_timer(on):
    """Windows: sleep(0.001) can really sleep ~15 ms. Ask for a 1 ms timer while a session runs (inputs arrive sooner)."""
    if os.name != 'nt':
        return
    try:
        import ctypes
        (ctypes.windll.winmm.timeBeginPeriod if on else ctypes.windll.winmm.timeEndPeriod)(1)
    except Exception:                                  # noqa: BLE001 - purely an optimisation
        pass


def auto_delay(sock, server_addr, samples=3):
    """Input delay (frames) chosen from the ping to the server. The host<->guest ping is estimated as 2x the host's
    ping to the server (the guest is assumed to be about as far from the server as the host)."""
    best = None
    for _ in range(samples):
        t = time.perf_counter()
        try:
            netlink.whoami(sock, server_addr)
        except Exception:                              # noqa: BLE001 - a lost answer just costs one sample
            continue
        ms = (time.perf_counter() - t) * 1000.0
        best = ms if best is None else min(best, ms)
    if best is None:
        return LAN_DELAY
    return max(AUTO_DELAY_MIN, min(AUTO_DELAY_MAX, node_mod.delay_for_rtt(best * 2)))


# ---- loading the match state into PCSX2 ------------------------------------------------------------------------
def _wait_stable(path, timeout=10.0):
    """PCSX2 writes states in the background: wait until the file stops changing."""
    end, last, still = time.time() + timeout, None, 0.0
    while time.time() < end:
        try:
            st = os.stat(path)
        except OSError:
            return False
        sig = (st.st_size, st.st_mtime_ns)
        if sig == last:
            if time.time() - still >= 0.6:
                return True
        else:
            last, still = sig, time.time()
        time.sleep(0.1)
    return False


def pine_load_state(pine, state_slot):
    """PINE MsgLoadState (pine.py: Pine.load_state). Raises PineError when PCSX2 refuses."""
    pine.load_state(state_slot)


def load_match_state(args):
    """Put args.state into PCSX2: copy it over the file of save slot args.load_slot, ask PCSX2 to load that slot over
    PINE, then wait until the game sits frozen at frame 0 of the armed match. True on success."""
    state = getattr(args, 'state', None)
    sdir = getattr(args, 'state_dir', None)
    slot = getattr(args, 'load_slot', None)
    if slot is None:
        slot = 9
    if not state or not os.path.isfile(state):
        print('cannot load: the match state file (.p2s) was not found.')
        return False
    if not sdir or not os.path.isdir(sdir):
        print("cannot load: PCSX2's save-state folder is not set (Settings > Folders in PCSX2; launcher: Advanced).")
        return False
    try:
        pine = Pine(args.pine_slot).connect()
    except (PineError, OSError) as error:
        print(f'cannot reach the game: {error}')
        return False
    try:
        print(f'asking PCSX2 to save slot {slot} once, to learn which file that slot uses ...')
        t0 = time.time()
        try:
            pine.save_state(slot)
        except Exception as error:                     # noqa: BLE001 - depends on the PCSX2 build
            print(f'failed: PCSX2 could not save slot {slot} ({error}). Is the game running?')
            return False
        target = None
        for _ in range(80):                            # up to ~8 s for the file to show up
            target = newest_state(sdir, t0 - 1)
            if target:
                break
            time.sleep(0.1)
        if target is None:
            print(f'failed: no new .p2s appeared in {sdir} after saving slot {slot}. Is this the folder set in '
                  f'PCSX2 (Settings > Folders)?')
            return False
        _wait_stable(target)
        try:
            shutil.copyfile(state, target)
        except OSError as error:
            print(f'failed: could not write {target}: {error}')
            return False
        print(f'loading {os.path.basename(state)} into PCSX2 (slot {slot}) ...')
        try:
            pine_load_state(pine, slot)
        except Exception as error:                     # noqa: BLE001
            print(f'failed: PCSX2 did not load slot {slot} ({error}). Load it by hand (PCSX2 hotkey, slot {slot}).')
            return False
    finally:
        try:
            pine.close()
        except Exception:                              # noqa: BLE001
            pass
    time.sleep(1.5)                                    # PCSX2 loads on its own thread: do not read the OLD state
    deadline = time.time() + 25
    while time.time() < deadline:
        time.sleep(0.5)
        if stop_requested(args):
            return False
        try:
            p2 = Pine(args.pine_slot).connect()
            try:
                base, _, _ = core_link.resolve_base(p2, args.symbols)
                if core_link.PineCore(base, pine=p2, serial=None).frozen():
                    print('FROZEN at frame 0 (match state loaded).')
                    return True
            finally:
                p2.close()
        except (PineError, RuntimeError, KeyError, OSError):
            pass
    print('failed: the state was loaded but the game is not frozen at frame 0. Is this the file made by PREPARE?')
    return False


def fetch_host_state(args):
    """Guest: make sure args.state is the host's match state (download it when the tag differs). Falls back to the
    local file, with a warning, when the host's state server cannot be reached and a local file exists."""
    ip, _, port = args.connect.rpartition(':')
    port = int(port or 47400)
    if not args.state:
        print('cannot download: no path for the match state.')
        return False
    have = state_tag(args.state) if os.path.isfile(args.state) else None
    print(f'checking the host match state at {ip}:{port} (TCP) ...')
    try:
        result, tag = statesync.fetch(ip, port, args.state, have, say=print)
    except statesync.StateError as error:
        if have:
            print(f'warning: could not check the host state ({error}). Using your file {have}; if it differs from '
                  f'the host\'s, the PCs will not connect.')
            return True
        print(f'failed: {error}')
        return False
    if result == 'same':
        print(f'your match state is the same as the host\'s ({tag}).')
    else:
        print(f'match state downloaded from the host ({tag}) -> {args.state}')
    return True


# ---- sharing symbols.json (host serves it, guest downloads it) -------------------------------------------------
# The match state carries the HOST's patched game code in RAM, so the addresses in symbols.json (where g_np lives,
# ...) must be the host's too. Protocol, one short TCP connection per check, on SYMBOLS_PORT_OFFSET + the room port
# (the state server already uses the room port itself):
#     client -> "SYM1 <tag or ->\n"          tag = first 16 hex digits of the sha256 of the guest's file
#     server -> "SAME <tag>\n"               identical files
#               "DATA <size> <tag>\n" + bytes  the host's file
#               "NONE\n"                      the host has no readable symbols.json
SYMBOLS_PORT_OFFSET = 2
SYMBOLS_MAX_SIZE = 8 * 1024 * 1024
SYMBOLS_TIMEOUT = 8.0


class SymbolsError(Exception):
    pass


def symbols_port(port):
    """TCP port of the symbols server for a room port. Both sides use this same function."""
    port = int(port)
    return port + SYMBOLS_PORT_OFFSET if port + SYMBOLS_PORT_OFFSET <= 65535 else port - SYMBOLS_PORT_OFFSET


def _bytes_tag(data):
    return hashlib.sha256(data).hexdigest()[:16]


def symbols_tag(path):
    with open(path, 'rb') as f:
        return _bytes_tag(f.read())


def _sym_read_line(sock, limit=128):
    """One line, byte by byte (never reads past the newline: the payload follows it)."""
    out = bytearray()
    while len(out) < limit:
        b = sock.recv(1)
        if not b:
            break
        if b == b'\n':
            return out.decode('ascii', 'replace').strip()
        out += b
    raise SymbolsError('no valid answer from the other side')


def _sym_read_exact(sock, size):
    chunks, left = [], size
    while left > 0:
        chunk = sock.recv(min(65536, left))
        if not chunk:
            raise SymbolsError('the connection closed before the file was complete')
        chunks.append(chunk)
        left -= len(chunk)
    return b''.join(chunks)


class SymbolsServer:
    """Host side: answers guests with the CURRENT content of `path` (read at every request)."""

    def __init__(self, path, port):
        self.path = path
        self.port = symbols_port(port)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.sock.bind(('', self.port))
            self.sock.listen(4)
        except OSError:
            self.sock.close()
            raise
        self.sock.settimeout(0.5)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self):
        while not self._stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn):
        try:
            conn.settimeout(SYMBOLS_TIMEOUT)
            parts = _sym_read_line(conn).split()
            if not parts or parts[0] != 'SYM1':
                return
            have = parts[1] if len(parts) > 1 else '-'
            try:
                with open(self.path, 'rb') as f:
                    data = f.read(SYMBOLS_MAX_SIZE + 1)
            except OSError:
                data = b''
            if not data or len(data) > SYMBOLS_MAX_SIZE:
                conn.sendall(b'NONE\n')
                return
            tag = _bytes_tag(data)
            if have == tag:
                conn.sendall(f'SAME {tag}\n'.encode('ascii'))
            else:
                conn.sendall(f'DATA {len(data)} {tag}\n'.encode('ascii') + data)
        except (OSError, SymbolsError):
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
        self._thread.join(timeout=2)


def fetch_symbols_file(ip, port, path):
    """Guest side: make `path` the host's symbols.json. Returns ('same' | 'downloaded', tag).
    Raises SymbolsError when the host cannot be reached or sends something unusable (the old file stays untouched).
    The download is checked (size limit, tag, valid JSON) and written atomically; the old file is kept as <path>.bak."""
    have = symbols_tag(path) if os.path.isfile(path) else '-'
    try:
        with socket.create_connection((ip, symbols_port(port)), timeout=SYMBOLS_TIMEOUT) as s:
            s.settimeout(SYMBOLS_TIMEOUT)
            s.sendall(f'SYM1 {have}\n'.encode('ascii'))
            header = _sym_read_line(s).split()
            if not header:
                raise SymbolsError('empty answer')
            if header[0] == 'SAME' and len(header) == 2:
                return 'same', header[1]
            if header[0] == 'NONE':
                raise SymbolsError('the host has no symbols.json to share')
            if header[0] != 'DATA' or len(header) != 3:
                raise SymbolsError('unexpected answer from the host')
            size, tag = int(header[1]), header[2]
            if not 0 < size <= SYMBOLS_MAX_SIZE:
                raise SymbolsError('the host file has an invalid size')
            data = _sym_read_exact(s, size)
    except (OSError, ValueError) as error:
        raise SymbolsError(str(error)) from None
    if _bytes_tag(data) != tag:
        raise SymbolsError('the download is corrupted (tag mismatch)')
    try:
        json.loads(data.decode('utf-8'))
    except ValueError:
        raise SymbolsError('the downloaded file is not valid JSON') from None
    path = os.path.abspath(path)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if os.path.isfile(path):
            shutil.copyfile(path, path + '.bak')
        tmp = path + '.tmp'
        with open(tmp, 'wb') as f:
            f.write(data)
        os.replace(tmp, path)
    except OSError as error:
        raise SymbolsError(f'could not write {path}: {error}') from None
    return 'downloaded', tag


def fetch_host_symbols(args):
    """Guest: make args.symbols the host's symbols.json. Falls back to the local file, with a warning, when the host
    cannot be reached and a local file exists."""
    ip, _, port = args.connect.rpartition(':')
    port = int(port or 47400)
    path = args.symbols
    have = symbols_tag(path) if os.path.isfile(path) else None
    print(f'checking the host symbols.json at {ip}:{symbols_port(port)} (TCP) ...')
    try:
        result, tag = fetch_symbols_file(ip, port, path)
    except SymbolsError as error:
        if have:
            print(f'warning: could not check the host symbols.json ({error}). Using your file {have}; if it comes '
                  f'from another build the game will not be found or will not sync.')
            return True
        print(f'failed: {error}')
        return False
    if result == 'same':
        print(f'your symbols.json is the same as the host\'s ({tag}).')
    else:
        print(f'symbols.json downloaded from the host ({tag}) -> {path} (the old one is kept as .bak)')
    return True


def cmd_load(args):
    return 0 if load_match_state(args) else 1


def cmd_stop(args):
    """Give the real pads back (also unfreezes a game that waits in the first stall)."""
    pine, core = connect(args)
    core.put('hold', 0)
    core.stop()
    print('stopped: the game reads the real pads again')
    return 0


# ---- diagnostic knobs -> in-game config ----------------------------------------------------------------------------
def _parse_range(text):
    a, _, b = str(text).partition(':')
    lo, hi = int(a, 0), int(b, 0)
    if not 0 < lo < hi:
        raise ValueError(text)
    return lo, hi


def apply_config(args, core, tag='00' * 8):
    num = lambda v: int(v, 0) if v else 0
    rng, watch = num(args.rng_addr), num(args.watch)
    start = end = 0
    if args.hash_range:
        a, _, b = args.hash_range.partition(':')
        start, end = num(a), num(b)
    mask = num(args.hash_mask)
    if not mask and (rng or end):
        mask = 0x0F | (16 if rng else 0) | (32 if end else 0)
    flags = 1 if args.rng_resync else 0
    if mask or rng or end or flags or watch:
        core.set_config(mask, rng, start, end, flags, watch)
        print(f'config: mask=0x{mask:X} rng=0x{rng:X} range=0x{start:X}:0x{end:X} resync={flags} watch=0x{watch:X}'
              f'  (must be the SAME on both PCs)')
    # RNG + selection sync (NpSync). The seed comes from the match-state tag unless given: both PCs hold the same file.
    mode = int(getattr(args, 'rng_mode', '2') or 0) & 3
    hash_bits = SYNC_HASH_BITS[getattr(args, 'sync_hash', 'sel')]
    site_bit = RNG_SITE_BIT if getattr(args, 'rng_site', '1') == '1' else 0
    seed = num(getattr(args, 'sync_seed', '')) or (int(tag[:8], 16) or 1)
    try:
        cos = [_parse_range(r) for r in (getattr(args, 'cos_range', None) or [])]
        extra = [int(a, 0) for a in (getattr(args, 'rng_addr2', None) or [])]
    except ValueError as error:
        raise SystemExit(f'bad --cos-range / --rng-addr2 value: {error}')
    core.set_sync(seed, mode | hash_bits | site_bit, rng_addrs=[0] + extra[:3], cos_mode=1 if cos else 0,
                  cos_ranges=cos)
    print(f'sync: seed=0x{seed:08X} rng-mode={mode} per-site={bool(site_bit)} '
          f'sync-hash={getattr(args, "sync_hash", "sel")} '
          f'cosmetic ranges={[(hex(a), hex(b)) for a, b in cos] or "none"}  (must be the SAME on both PCs)')


def print_rng_diag(core):
    """Rand call sites seen during the session and how often each was called (compare the two PCs)."""
    try:
        rows = core.rng_diag()
    except Exception:                                  # noqa: BLE001 - the game may already be gone
        return
    if not rows:
        print('rand call sites: none recorded (np_rand is not installed, or rand was never called)')
        return
    print('rand call sites (return address: calls). A site that counts differently on the two PCs is cosmetic:')
    for ra, n in sorted(rows, key=lambda r: -r[1]):
        print(f'    0x{ra:08X}: {n}')


def cmd_prepare(args):
    pine, core = connect(args)
    if core.frozen():
        # Arming again would zero the frame counter while the game waits inside its stall (its own copy of the frame
        # number is already 0): the saved state would be inconsistent. Only save.
        print('the game is ALREADY frozen at frame 0: not arming again, only saving.')
    else:
        print('arming the filter in HOLD (the game will freeze at frame 0) ...')
        core.arm()
        end = time.time() + 5
        while time.time() < end and not core.frozen():
            time.sleep(0.05)
        if not core.frozen():
            print('the game did not freeze: is it in a fight (not paused) with both pads connected?')
            core.stop()
            return 1
    print('FROZEN at frame 0. Save the state now.')
    t0 = time.time()
    if args.save_slot is not None:
        try:
            pine.save_state(args.save_slot)
            print(f'asked PCSX2 to save state slot {args.save_slot}.')
        except Exception as error:                     # noqa: BLE001 - depends on the PCSX2 build
            print(f'PINE could not save the state ({error}).')
    if args.state_dir:
        found = None
        for _ in range(60):                            # up to ~6 s for the file to show up
            found = newest_state(args.state_dir, t0 - 1)
            if found:
                break
            time.sleep(0.1)
        if found:
            print(f'state saved: {found}')
        else:
            print(f'NO new .p2s appeared in {args.state_dir}: PINE saving did not work on this PCSX2 build (or the '
                  f'folder is another one). Press the save-state hotkey (F1) in the PCSX2 window now - the game '
                  f'stays frozen - and look for the file there.')
    else:
        print('Check that a new .p2s exists (pass --state-dir FOLDER to let this tool verify it). If it does not, press '
              'the save-state hotkey (F1) in the PCSX2 window now: the game stays frozen.')
    print('The game stays frozen. Next: run "host" on this PC after the guest has loaded the same state.')
    return 0


def cmd_solo(args):
    """One PC, no network: leave the frozen match state like a real match would, feeding the game from your own pad."""
    pine, core = connect(args)
    if not core.frozen():
        print('the game is not frozen at frame 0 of an armed match. Load the match state first (F3).')
        return 1
    from selftest_netpad import Loopback
    lb = Loopback(core, args.delay, args.control, resume=True)
    lb.begin()
    print(f'match released from the frozen state (solo, delay {args.delay}, your pad 1 drives {args.control}). '
          f'Ctrl+C to stop.')
    last, last_frame = time.time(), core.get('frame')
    try:
        while not stop_requested(args):
            fed = lb.step()
            now = time.time()
            if now - last >= 1.0:
                h = core.health()
                print(f"frame={h['frame']:7d}  {(h['frame'] - last_frame) / (now - last):4.1f}/s  stalls={h['stalls']}  "
                      f"status={h['status']}  abort={h['abort']}")
                last, last_frame = now, h['frame']
                if h['abort']:
                    print('the game gave up waiting for inputs (stall timeout).')
                    return 2
            if not fed:
                time.sleep(0.001)
    except KeyboardInterrupt:
        pass
    finally:
        core.stop()
        print('stopped: the game reads the real pads again')
    return 0


# ---- online mode: rooms, state and symbols.json through the UN5 server (relay_server.py), UDP hole punching ---------
# --server HOST[:PORT] on the host publishes a room; --server HOST[:PORT] --room ID on the guest joins it. No port
# forwarding and no VPN: both PCs punch through their NATs, and when that fails the server relays the UDP packets.
def online_guest_fetch(args):
    """Guest, online: read the room, take its input delay and download the host's match state and symbols.json from the
    server when they differ from yours (--fetch-state)."""
    try:
        api = netlink.Api(getattr(args, 'api_url', '') or args.server)
        room = api.get_room(args.room)
    except netlink.ServerError as error:
        print(f'failed: {error}')
        return False
    if room['status'] != 'waiting':
        print(f"failed: {room['nick']}'s room is already in a match.")
        return False
    if args.delay > 0 and args.delay != room['delay']:
        print(f"the room uses input delay {room['delay']}: using it (you had {args.delay}).")
    args.delay = room['delay']
    if not getattr(args, 'fetch_state', False):
        return True
    for label, tag, path in (('match state', room['tag'], args.state), ('symbols.json', room['sym'], args.symbols)):
        if not path:
            print(f'failed: no path for the {label}.')
            return False
        have = state_tag(path) if os.path.isfile(path) else None
        if have == tag:
            print(f"your {label} is the same as the host's ({tag}).")
            continue
        print(f"downloading the host {label} from the server ({tag}) ...")
        try:
            api.blob_get(tag, path, say=print, backup=(label == 'symbols.json'), json_check=(label == 'symbols.json'))
        except netlink.ServerError as error:
            if have:
                print(f"warning: could not download the host {label} ({error}). Using your file {have}; if it differs "
                      f"from the host's, the PCs will not connect.")
                continue
            print(f'failed: {error}')
            return False
        print(f'{label} downloaded ({tag}) -> {path}')
    return True


def online_open(args, core, is_host, tag, nick):
    """Open the UDP socket, learn our public address from the server, publish / join the room and build the Node.
    Returns (node, poller) or (None, None) after printing why."""
    api = room = sock = None
    try:
        api = netlink.Api(getattr(args, 'api_url', '') or args.server)
        server_addr = netlink.resolve(args.server)
        sock = netlink.open_socket(args.port if is_host else args.bind_port)
        pub = netlink.whoami(sock, server_addr)
        lan = netlink.lan_candidates(sock.getsockname()[1])
        print(f'online: the server sees this PC as {pub[0]}:{pub[1]} (UDP)')
        if is_host and args.delay <= 0:
            args.delay = auto_delay(sock, server_addr)
            print(f'input delay: {args.delay} frames (automatic, from your ping to the server)')
        if is_host:
            sym_tag = symbols_tag(args.symbols)
            room = api.create_room(nick, args.delay, tag, sym_tag, pub, lan)
            print(f"the server accepted your match state ({tag}): it is the server's default.")
            poller = netlink.RoomPoller(api, room['id'], room['token'])
            poller.host_ip = pub[0]
            relay = (server_addr, bytes.fromhex(room['token']))
            node = node_mod.Node(core, 1, 0, 0, [0, 1], delay=args.delay, tag=tag, name=nick, first_capture=1,
                                 sock=sock, relay=relay)
            print(f"host: room {room['id']} is open on the server (delay {args.delay} frames, state tag {tag}). "
                  f"Waiting for the guest ...")
            return node, poller
        room = api.join_room(args.room, nick, pub, lan)
        if room['tag'] != tag:
            print(f"failed: your match state ({tag}) is not the host's ({room['tag']}). Use --fetch-state, or the "
                  f"host's UN5-partida.p2s.")
            sock.close()
            return None, None
        host_addr = tuple(room['host']['addr'])
        cands = [tuple(c) for c in room['host'].get('lan', [])] if host_addr[0] == pub[0] else []
        relay = (server_addr, bytes.fromhex(room['token']))
        node = node_mod.Node(core, 2, 1, 0, [0, 1], delay=room['delay'], tag=tag, name=nick, host=host_addr,
                             first_capture=1, sock=sock, relay=relay, cands=cands)
        print(f"guest: joined room {args.room} ({room['host']['nick']}), connecting (delay {room['delay']} frames, "
              f"state tag {tag}) ...")
        return node, None
    except (netlink.ServerError, OSError) as error:
        print(f'failed: {error}')
        if is_host and room is not None and api is not None:
            try:
                api.close_room(room['id'], room['token'])
            except netlink.ServerError:
                pass
        if sock is not None:
            sock.close()
        return None, None


def cmd_play(args, is_host):
    server_mode = bool(getattr(args, 'server', ''))
    if args.delay <= 0 and not server_mode:
        args.delay = LAN_DELAY                         # LAN / VPN: nothing to measure before connecting
    if not is_host and not server_mode and not args.connect:
        print('guest: give --connect HOST_IP:PORT, or --server SERVER --room ID.')
        return 1
    if not is_host and server_mode:
        if not args.room:
            print('guest: --server needs --room ID (the room id shown in the room list).')
            return 1
        if not online_guest_fetch(args):
            return 1
    elif not is_host and getattr(args, 'fetch_state', False):
        if not fetch_host_state(args):
            return 1
        if not fetch_host_symbols(args):               # the state carries the host's build: use the host's symbols.json
            return 1
    if getattr(args, 'load_slot', None) is not None and args.state:
        if not load_match_state(args):                 # loads the match state into PCSX2 first (frozen at frame 0)
            return 1
    pine, core = connect(args)
    if not core.frozen():
        print('the game is not frozen at frame 0 of an armed match. Load the match state (made with "prepare") first.')
        return 1
    tag = state_tag(args.state) if args.state else '00' * 8
    apply_config(args, core, tag)
    server = None
    sym_server = None
    if is_host and args.state and not getattr(args, 'no_share_state', False) and not server_mode:
        try:
            server = statesync.StateServer(args.state, args.port)
            print(f'sharing the match state on TCP {args.port}: guests can download it when theirs differs.')
        except OSError as error:
            print(f'state sharing is off (TCP {args.port}: {error}); send the .p2s to the guest by hand.')
    if is_host and not getattr(args, 'no_share_state', False) and not server_mode:
        try:
            sym_server = SymbolsServer(args.symbols, args.port)
            print(f'sharing symbols.json on TCP {symbols_port(args.port)}: guests download it when theirs differs.')
        except OSError as error:
            print(f'symbols sharing is off (TCP {symbols_port(args.port)}: {error}); the guest needs your symbols.json.')
    nick = node_mod.clean_nick(getattr(args, 'nick', '')) or ('Player1' if is_host else 'Player2')
    print(f'your nick: {nick}')
    poller = None
    if server_mode:
        node, poller = online_open(args, core, is_host, tag, nick)
        if node is None:
            return 1
    elif is_host:
        node = node_mod.Node(core, 1, 0, args.port, [0, 1], delay=args.delay, tag=tag, name=nick, first_capture=1)
        print(f'host: waiting for the guest on UDP {args.port} (delay {args.delay} frames, state tag {tag}) ...')
    else:
        ip, _, port = args.connect.rpartition(':')
        node = node_mod.Node(core, 2, 1, args.bind_port, [0, 1], delay=args.delay, tag=tag, name=nick,
                             host=(ip, int(port or 47400)), first_capture=1)
        print(f'guest: connecting to {args.connect} (delay {args.delay} frames, state tag {tag}) ...')
    if is_host and getattr(args, 'ignore_desync', False):
        node.desync_stop = False
        print('--ignore-desync: hash mismatches are only printed, the match keeps going.')
    node.watch_log = bool(getattr(args, 'watch_log', False))
    node.tick_barrier = bool(getattr(args, 'tick_barrier', False))
    if node.tick_barrier:
        print('--tick-barrier: EXPERIMENTAL, the PC whose game tick is ahead parks for up to 150 ms (use it on both PCs).')
    node.sel_sync = not getattr(args, 'no_force_sel', False)
    if not node.sel_sync:
        print('--no-force-sel: the guest keeps its own selection (the host selection is not sent / applied).')
    force_seen = 0
    try:
        watch_code = int(args.watch, 0) if args.watch else 0
    except ValueError:
        watch_code = 0
    if node.watch_log and watch_code not in (9, 10):
        print('note: --watch-log works best with --watch 10 (or 9); with a RAM address every change is printed.')
    announcer = None
    if is_host and rooms is not None and not getattr(args, 'no_announce', False):
        announcer = rooms.RoomAnnouncer(nick, args.port, args.delay, tag)
        if announcer.routes:
            print(f"room announced on {', '.join(announcer.ips)} (UDP {rooms.DISCOVERY_PORT}): others see it in the room list")
        else:
            print('room not announced: no usable network interface')
            announcer = None
    session = NetplaySession(core, node)
    flow = GameFlow(pine)
    _fine_timer(True)
    last, last_frame, t0 = time.time(), 0, time.time()
    seen = 0
    soft_seen = 0
    shift_seen = 0
    tick_seen = 0
    watch_seen = 0
    info_at = 0.0
    last_path = None
    lost_said = False
    warned_need = None
    sel_seen = 0
    try:
        while not stop_requested(args):
            desync, closed = session.step()
            if server_mode:
                path = node.path_summary()                  # direct (hole punched) or relay (through the server)
                if path != last_path:
                    print(f'connection: {path}')
                    last_path = path
            if poller is not None:
                poller.status = 'playing' if node.started else 'waiting'
                guest = poller.take_guest()
                if guest:
                    node.add_expected_peer(2, 1, tuple(guest['addr']), poller.lan_cands(guest))
                    print(f"{guest.get('nick') or 'a guest'} joined the room: connecting ...")
                if poller.lost and not lost_said:
                    lost_said = True
                    print('warning: the server forgot this room (restart?): new guests cannot join it.')
            if announcer is not None:
                announcer.tick('playing' if node.started else 'waiting')
            if time.time() - info_at >= 0.5:                # nicks + ping for the on-screen display (Online.c)
                info_at = time.time()
                flow.report()
                try:
                    core.set_info(*node.info())
                except Exception:                           # noqa: BLE001 - purely cosmetic, never stop a match for it
                    pass
                try:                                        # the selection this PC locked when the fight started
                    sel_tag = core.sync_get('sel_final_tag')
                    if sel_tag != sel_seen:
                        sel_seen = sel_tag
                        print(f"SEL (frame {sel_tag - 1}): "
                              f"{core_link.format_selection(core.selection('final'))}")
                except Exception:                           # noqa: BLE001 - diagnostics only
                    pass
                if not is_host and node.sel_sync:           # the guest overwrote its selection with the host's
                    try:
                        fw = core.force_info()['writes']
                        if fw != force_seen:
                            print(f"SEL FORCED: the guest overwrote {fw - force_seen} slot field(s) with the "
                                  f"host's selection")
                            force_seen = fw
                    except Exception:                       # noqa: BLE001 - diagnostics only
                        pass
            while seen < len(node.desync_log):             # hash mismatches (host only; also printed with --ignore-desync)
                f, vals = node.desync_log[seen]
                seen += 1
                print(f"hash differs at frame {f}: {vals}  stalls={core.get('stalls')}")
            while soft_seen < len(node.soft_log):          # a PC without player objects (scene change): not a desync
                f, vals = node.soft_log[soft_seen]
                soft_seen += 1
                print(f"scene change at frame {f}: one PC has no fighters at this moment {vals} (keeps going)")
            while tick_seen < len(node.tick_log):          # the game's own tick counter, host vs guest, same net frame
                f, ta, tb, off = node.tick_log[tick_seen]
                tick_seen += 1
                print(f"TICK frame {f}: game counter host={ta} guest={tb} -> offset guest-host = {off:+d}")
            while watch_seen < len(node.watch_events):     # --watch-log: the watched value changed on one of the PCs
                f, ta, tb = node.watch_events[watch_seen]
                watch_seen += 1
                mark = '' if ta == tb else '   <-- DIFFERENT'
                print(f"WATCH frame {f}: host[{describe_watch(ta, watch_code)}] "
                      f"guest[{describe_watch(tb, watch_code)}]{mark}")
            while shift_seen < len(node.shift_log):        # is the mismatch a pure offset between the two PCs?
                f, k = node.shift_log[shift_seen]
                shift_seen += 1
                if k is None:
                    print(f"SHIFT? frame {f}: no offset of -16..+16 explains the mismatch (real divergence)")
                else:
                    print(f"SHIFT frame {f}: host(f{k:+d}) == guest(f) -> host {'BEHIND' if k > 0 else 'AHEAD'} "
                          f"by {abs(k)} tick(s)")
            if desync is not None:
                print(f'DESYNC at frame {desync}: the two games diverged. Stopping.')
                try:
                    print(f"SEL on this PC: {core_link.format_selection(core.selection('final'))}  "
                          f"(compare with the other PC's SEL line: a different selection explains a desync at the "
                          f"first frames)")
                except Exception:                           # noqa: BLE001
                    pass
                return 3
            if closed:
                print(f'the other side left (reason {closed}).')
                return 0
            now = time.time()
            if now - last >= 1.0:
                if session.node.started:
                    h = core.health()
                    rtt = session.node.rtt_ms()
                    fps = (h['frame'] - last_frame) / (now - last)
                    # idle = 1: no fighters right now, the frame counter is paused (scene barrier at work);
                    # seen = 1: the filter has seen the fighters alive, so the scene barrier is armed (new code runs).
                    print(f"frame={h['frame']:7d}  {fps:4.1f}/s  rtt={rtt if rtt is None else round(rtt)}ms  "
                          f"stalls={h['stalls']}  status={h['status']}  abort={h['abort']}  "
                          f"idle={h['idle']} seen={h['live_state'] & 1}")
                    if rtt is not None and node_mod.delay_for_rtt(rtt) > args.delay:
                        need = node_mod.delay_for_rtt(rtt)
                        if need != warned_need:
                            warned_need = need
                            print(f'    (the connection would need delay {need}; you use {args.delay})')
                    last_frame = h['frame']
                    if h['abort']:
                        print('the game gave up waiting for inputs (stall timeout).')
                        return 2
                else:
                    print('waiting for the other PC ...')
                last = now
            time.sleep(0.001)
    except KeyboardInterrupt:
        pass
    finally:
        if server is not None:
            server.close()
        if sym_server is not None:
            sym_server.close()
        if announcer is not None:
            announcer.close()
        if poller is not None:
            poller.close()                             # deletes the room on the server
        try:
            node.close()
        finally:
            print_rng_diag(core)                       # before the filter is switched off (the block stays readable)
            core.stop()                                # active = 0: the real pads again
            _fine_timer(False)
            print('stopped: the game reads the real pads again')
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--symbols', default='temp_build/build/symbols.json')
    ap.add_argument('--pine-slot', type=int, default=28011)
    ap.add_argument('--stop-file', default=None, help='file signal to stop the netplay session')
    sub = ap.add_subparsers(dest='cmd', required=True)
    p = sub.add_parser('prepare', help='arm the filter and freeze the game at frame 0 (to save the match state)')
    p.add_argument('--save-slot', type=int, help='ask PCSX2 (PINE) to save the state in this slot')
    p.add_argument('--state-dir', help="PCSX2's save-state folder (Settings > Folders): the tool then checks the file appeared")
    sub.add_parser('stop', help='give the real pads back (unfreezes the game)')
    ld = sub.add_parser('load', help='load the match state (.p2s) into PCSX2 over PINE and wait for the freeze')
    ld.add_argument('--state', required=True, help='the match state file')
    ld.add_argument('--state-dir', required=True, help="PCSX2's save-state folder")
    ld.add_argument('--load-slot', type=int, default=9, help='PCSX2 slot that is OVERWRITTEN with the match state')
    so = sub.add_parser('solo', help='no network: leave the frozen state feeding the game from your own pad (a self test)')
    so.add_argument('--delay', type=int, default=3)
    so.add_argument('--control', choices=('p1', 'p2', 'both'), default='p1')
    env = os.environ.get
    for name in ('host', 'guest'):
        q = sub.add_parser(name)
        q.add_argument('--state', help='the match state file (both PCs: identical bytes); makes the session id')
        q.add_argument('--delay', type=int, default=0,
                       help='frames of input delay (33 ms each). 0 = automatic (default): the host picks it from its '
                            'ping to the server, the online guest uses the room\'s, LAN / VPN uses 3')
        q.add_argument('--nick', default='', help='your name on the screen (up to 15 characters, ASCII)')
        q.add_argument('--state-dir', default=None, help="PCSX2's save-state folder (needed with --load-slot)")
        q.add_argument('--load-slot', type=int, default=None,
                       help='load --state into this PCSX2 slot (OVERWRITTEN) and start from it, automatically')
        q.add_argument('--hash-mask', default=env('NP_HASH_MASK', ''),
                       help='1 position, 2 0x68..0x80, 4 life/chakra, 8 timers, 16 RNG, 32 extra range')
        q.add_argument('--rng-addr', default=env('NP_RNG_ADDR', ''), help='RAM address of the RNG word (0x...)')
        q.add_argument('--rng-resync', action='store_true', default=bool(env('NP_RNG_RESYNC')),
                       help='old flag: both PCs write the same value into the RNG at every scene start')
        q.add_argument('--rng-mode', choices=('0', '1', '2'), default=env('NP_RNG_MODE', '2'),
                       help='RNG sync: 0 off, 1 reseed at scene entry, 2 (default) = 1 + at every frame')
        q.add_argument('--rng-site', choices=('0', '1'), default=env('NP_RNG_SITE', '1'),
                       help='1 (default): one private rand stream per call site (cosmetic callers cannot shift the '
                            'logic); 0: the old single shared stream. The SAME on both PCs')
        q.add_argument('--no-force-sel', action='store_true',
                       help='do not make the guest follow the host selection (characters / skins / colours / map)')
        q.add_argument('--sync-seed', default=env('NP_SYNC_SEED', ''),
                       help='seed of the RNG sync (default: derived from the match state tag; the SAME on both PCs)')
        q.add_argument('--sync-hash', choices=tuple(SYNC_HASH_BITS), default=env('NP_SYNC_HASH', 'sel'),
                       help='extra state-hash terms: sel = menu selection (default), seq = rand call sequence, both')
        q.add_argument('--cos-range', action='append', default=None, metavar='A:B',
                       help='return-address range of a function whose rand calls are cosmetic (repeatable, up to 8)')
        q.add_argument('--rng-addr2', action='append', default=None, metavar='ADDR',
                       help='extra 64-bit LCG state to reseed (repeatable, up to 3; same format as 0x412B90)')
        q.add_argument('--hash-range', default=env('NP_HASH_RANGE', ''), help='START:END of extra RAM to hash (<= 64 KB)')
        q.add_argument('--watch', default=env('NP_WATCH', ''),
                       help='RAM word shown in the TICK lines (both PCs); or a code: 9 = action/arg/timer, '
                            '10 = flow state/action/arg/players')
        q.add_argument('--watch-log', action='store_true', default=bool(env('NP_WATCH_LOG')),
                       help='print a WATCH line every time the watched value changes (use with --watch 10)')
        q.add_argument('--tick-barrier', action='store_true', default=bool(env('NP_TICK_BARRIER')),
                       help='experimental: park the PC whose game tick is ahead, up to 150 ms (off by default)')
        q.add_argument('--server', default=env('NP_SERVER', ''), metavar='HOST[:PORT]',
                       help='online mode: UN5 server (relay_server.py) for rooms, files and relay; no VPN / port forward')
        q.add_argument('--api-url', default=env('NP_API_URL', ''), metavar='URL',
                       help='HTTP(S) address of the server API (e.g. https://un5netplay.duckdns.org); '
                            'default: http://--server. UDP (relay) always uses --server')
        if name == 'host':
            q.add_argument('--no-share-state', action='store_true',
                           help='do not serve the match state file and symbols.json to guests (TCP)')
            q.add_argument('--port', type=int, default=47400, help='UDP port to listen on')
            q.add_argument('--no-announce', action='store_true',
                           help='do not show this room in the room list of other players')
            q.add_argument('--ignore-desync', action='store_true',
                           help='debug: print hash mismatches but keep the match running')
        else:
            q.add_argument('--connect', default='', metavar='HOST_IP:PORT', help='LAN / VPN: the host address')
            q.add_argument('--room', default='', help='online: id of the room to join (with --server)')
            q.add_argument('--fetch-state', action='store_true',
                           help='download the host match state and symbols.json when they differ from yours')
            q.add_argument('--bind-port', type=int, default=0, help='local UDP port (0 = any)')
    args = ap.parse_args(argv)
    if args.cmd == 'prepare':
        return cmd_prepare(args)
    if args.cmd == 'solo':
        return cmd_solo(args)
    if args.cmd == 'stop':
        return cmd_stop(args)
    if args.cmd == 'load':
        return cmd_load(args)
    return cmd_play(args, args.cmd == 'host')


if __name__ == '__main__':
    sys.exit(main())