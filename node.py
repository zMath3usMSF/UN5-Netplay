"""Star-topology lockstep node: one host (member 1) and any number of guests.

The node talks to the game through a `core` object with three methods (see core_link.py for the PCSX2/PINE one,
FakeCore in test_loopback.py for tests):

    core.captured(first, limit)  -> {frame: (raw8, aux)}   this PC's pad captured by the game, contiguous from `first`
    core.publish(slot, frames)   -> None                   commit {frame: (raw8, aux)} for `slot` into the game
    core.hashes(first, limit)    -> {frame: int}           per-frame state hashes of this PC, contiguous from `first`

The game side (in-game hook) must STALL a frame until all slots' inputs for it were published.
Input delay: a pad captured at game frame f is used at frame f + delay on every PC; frames 0..delay-1 are neutral.

Host-authoritative selection: the host sends the selection it sees in the menus (SELX datagram, same socket and same
path as the game data) and the guest hands it to core.follow_selection(). A core without those methods (FakeCore) just
ignores it. Every SELX carries a sequence number (grows only when the selection changes): the guest drops packets
older than the newest one it applied, so a delayed / reordered datagram can never bring back an old selection.

Tick barrier (--tick-barrier, OFF by default): compares the game counters of both players on the same network frame and
parks the PC that is ahead for a short time. It is experimental; the lockstep is deterministic without it.
"""
import os
import random
import select
import socket
import struct
import time
import zlib

import netlink
import wire

NEUTRAL = (wire.IDLE_PAD, 0)

# State hash of netpad (np_state_hash) when BOTH player objects are missing (np_player() returns 0 for each):
# fnv(fnv(2166136261, 0xFFFFFFFF), 0xFFFFFFFF). A PC reporting it is between scenes (fight over / loading), so a
# mismatch against it is a scene change, not a divergence of the fight state.
NULL_STATE_HASH = 1823345245

SEL_MAGIC = b'SELX'                  # host -> guest: the host's menu selection (NpSel, 14 words)
SEL_FMT = '<4sBII14I'                # magic | member | session crc | seq | 14 words
SEL_LEN = struct.calcsize(SEL_FMT)

TICK_HOLD_MAX = 0.15                 # seconds a tick-barrier hold (hold = 2) may last


FRAME_MS = 1000.0 / 30.0        # Ultimate Ninja 5 runs its fight logic at 30 frames per second (measured)


def delay_for_rtt(rtt_ms, frame_ms=FRAME_MS, margin=int(os.environ.get('UN5_DELAY_MARGIN', '0'))):  # UN5_PATCH_DELAY
    """Input delay in frames that hides half the round trip: one frame costs 33 ms here, so a 100 ms RTT needs 3."""
    import math
    return max(1, int(math.ceil(rtt_ms / 2.0 / frame_ms)) + margin)


def clean_nick(value, limit=15):
    """A nick that is safe to draw with the game's font: printable ASCII, no '<' or '>' (they start font tags)."""
    if isinstance(value, (bytes, bytearray)):
        value = bytes(value).split(b'\0')[0].decode('ascii', 'replace')
    value = str(value or '')
    out = ''.join(ch if 32 <= ord(ch) <= 126 and ch not in '<>' else '?' for ch in value).strip()
    return out[:limit]


def now_ms():
    return int(time.perf_counter() * 1000) & 0xFFFFFFFF


class Peer:
    def __init__(self, member, slot, addr):
        self.member, self.slot, self.addr = member, slot, addr
        self.ack = [0] * wire.SLOTS          # per slot: next frame this peer still needs
        self.hello = False
        self.name = ''                       # the nick it announced in its HELLO
        self.confirmed = False               # we got a DATA from it: it knows we are here
        self.last_t = 0                      # newest clock value received from this peer
        self.last_rx = 0                     # when (our clock) we received it
        self.rtt = None
        self.hash_ack = 0                    # next hash frame this peer wants from us
        self.last_tx = 0
        self.seq = 0
        self.cands = []                      # online: extra addresses to try while punching (LAN addresses)
        self.strict = False                  # online: only accept direct packets from the expected IPs
        self.direct_rx = 0.0                 # when a packet last arrived straight from this peer
        self.relay_rx = 0.0                  # when a packet last arrived through the relay
        self.last_probe = 0.0                # when we last tried the direct path while on the relay
        self.t0 = time.perf_counter()        # when we started trying to reach this peer


class Node:
    def __init__(self, core, member, slot, bind_port, active, delay=3, tag='00' * 8, name='', host=None,
                 expect=None, loss=0.0, bind='0.0.0.0', first_capture=0, sock=None, relay=None, cands=None,
                 punch_s=2.5):
        """member: 1 for the host, 2.. for guests.  slot: this PC's input slot.  active: slots that have a player.
        host: (ip, port) of the host when this PC is a guest, None for the host.
        Online mode (rendezvous server): sock = the UDP socket the server already saw (same NAT mapping), relay =
        ((server ip, port), 8-byte token): packets go straight to the peer and, when nothing comes back for punch_s
        seconds, through the server's relay. cands = extra direct addresses of the host (its LAN addresses)."""
        self.core, self.member, self.slot, self.active = core, member, slot, list(active)
        self.delay, self.tag, self.name, self.loss = delay, tag, name, loss
        self.session = wire.session_id(tag, delay)
        self.is_host = host is None
        if sock is None:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.bind((bind, bind_port))
        self.sock = sock
        self.sock.setblocking(False)
        self.relay = relay
        self.relay_addr = tuple(relay[0]) if relay else None
        self.relay_token = relay[1] if relay else None
        self.punch_s = punch_s
        self.last_keep = -10.0
        self.peers = {} if self.is_host else {1: Peer(1, None, host)}
        if not self.is_host:
            self.peers[1].cands = [tuple(c) for c in (cands or [])]
            self.peers[1].strict = relay is not None
        self.expect = (len(self.active) - 1) if expect is None and self.is_host else (1 if expect is None else expect)
        self.known = {s: {f: NEUTRAL for f in range(delay)} for s in self.active}
        self.need = {s: delay for s in self.active}          # next frame not yet known (contiguous)
        self.published = {s: 0 for s in self.active}
        self.first_capture = first_capture
        self.cap_next = first_capture
        for f in range(first_capture):                     # their captures are ignored (stale after a loaded state):
            if self.slot is not None:                      # those frames enter as the neutral pad
                self.known[self.slot][f + delay] = NEUTRAL
        if self.slot is not None:
            self._advance(self.slot)
        self.started = False
        self.hash_local_next = 0
        self.reports = {}                                    # host: {member: {frame: hash}}
        self.hash_need = {}                                  # host: {member: next hash frame it needs}
        self.checked = 0
        self.desync = None
        self.desync_stop = True                              # False: only log hash mismatches, keep playing
        self.desync_log = []                                 # [(frame, {member: hash})] of every mismatch seen
        self.soft_log = []                                   # mismatches where one PC had no player objects
        self.shift_log = []                                  # [(frame, k)]  k: host(f+k) == guest(f); None = no offset explains it
        self._last_shift = None
        self.tick_next = first_capture + delay               # first net frame whose capture really came from the game
        self.tick_log = []                                   # [(frame, host counter, guest counter, offset)] on every change
        self._tick_off = None
        self._tick_hold = False
        self.tick_barrier = False                            # --tick-barrier turns the (experimental) barrier on
        self._tick_base = None                               # first offset seen: the constant difference is ignored
        self._hold_t0 = 0.0                                  # when the current hold started
        self._hold_cool = 0.0                                # no new hold before this time
        self.watch_log = False                               # True: record every change of the watched aux word
        self.watch_events = []                               # [(frame, host value, guest value)] on every change
        self._watch_last = None
        self.last_hello = 0.0
        self.closed_reason = None
        self.sel_sync = True                                 # host-authoritative selection (--no-force-sel turns it off)
        self.sel_seq = None
        self.sel_words = None
        self.sel_polled = 0.0
        self.sel_last_tx = 0.0
        self.sel_tx_seq = 0                                  # host: sequence number of the SELX packets
        self.sel_rx_seq = None                               # guest: newest sequence number applied
        self._sel_crc = zlib.crc32(repr(self.session).encode()) & 0xFFFFFFFF

    # ---- helpers -------------------------------------------------------------------------------------------------
    def _sendto(self, datagram, addr):
        if self.loss and random.random() < self.loss:
            return
        try:
            self.sock.sendto(datagram, addr)
        except OSError:
            # Windows: an earlier datagram to a port nobody listens on (yet / any more) can make a later send fail with
            # WinError 10054 or "network unreachable". UDP is retransmitted by the protocol, so just drop this one.
            pass

    # ---- online transport: direct first, relay as the fallback ---------------------------------------------------
    def _send_direct(self, p, datagram):
        if p.addr:
            self._sendto(datagram, p.addr)
        if not p.direct_rx:                                  # still punching: also try the other known addresses
            for c in p.cands:
                if c != p.addr:
                    self._sendto(datagram, c)

    def _send_relay(self, datagram):
        self._sendto(netlink.wrap(self.relay_token, datagram), self.relay_addr)

    def _tx(self, p, datagram):
        """Send to a peer. LAN / VPN (no relay): straight to its address, as before. Online: straight while packets keep
        arriving straight; otherwise (after punch_s seconds, or as soon as the peer reached us through the relay)
        through the relay, with a direct probe every second so the match moves back to the direct path by itself."""
        if self.relay is None:
            if p.addr:
                self._sendto(datagram, p.addr)
            return
        now = time.perf_counter()
        if p.direct_rx and now - p.direct_rx < 2.0:
            self._send_direct(p, datagram)
        elif p.relay_rx or now - p.t0 >= self.punch_s:
            self._send_relay(datagram)
            if now - p.last_probe >= 1.0:
                p.last_probe = now
                self._send_direct(p, datagram)
        else:
            self._send_direct(p, datagram)

    def _keepalive(self):
        """Tell the relay where we are (and keep the NAT mapping open) every few seconds."""
        if self.relay is None:
            return
        t = time.perf_counter()
        if t - self.last_keep >= 3.0:
            self.last_keep = t
            self._sendto(netlink.keepalive(self.relay_token), self.relay_addr)

    @staticmethod
    def _known_ip(peer, addr):
        ips = {c[0] for c in peer.cands}
        if peer.addr:
            ips.add(peer.addr[0])
        return addr[0] in ips

    def add_expected_peer(self, member, slot, addr, cands=()):
        """Host, online mode: the server told us who joined and from where. Start talking to it right away (our packets
        open the hole in our own NAT for its packets)."""
        p = self.peers.get(member)
        if p is None:
            p = self.peers[member] = Peer(member, slot, tuple(addr))
            self.hash_need[member] = 0
        p.cands = [tuple(c) for c in cands]
        p.strict = True
        p.t0 = time.perf_counter()
        return p

    def path_summary(self):
        """'direct', 'relay' or 'connecting' (online mode): how the packets of the peers are travelling right now."""
        if self.relay is None:
            return 'direct'
        now = time.perf_counter()
        state = 'direct' if self.peers else 'connecting'
        for p in self.peers.values():
            if p.direct_rx and now - p.direct_rx < 2.0:
                continue
            if p.relay_rx and now - p.relay_rx < 3.0:
                state = 'relay'
            else:
                return 'connecting'
        return state

    def ready(self):
        return len(self.peers) >= self.expect and all(p.hello for p in self.peers.values())

    def info(self):
        """([nick of player 1, nick of player 2], [ping player 1, ping player 2]) for the on-screen display.
        Slot 0 is the host, slot 1 the guest. The ping of the player on THIS PC is 0; the ping of the OTHER player is
        the measured round trip (0xFFFFFFFF = not measured yet). So the host sees (0, rtt) and the guest (rtt, 0):
        the two values are no longer a copy of each other."""
        names = ['', '']
        if self.slot in (0, 1):
            names[self.slot] = clean_nick(self.name)
        for p in self.peers.values():
            if p.slot in (0, 1):
                names[p.slot] = p.name
        rtt = self.rtt_ms()
        value = int(round(rtt)) if rtt is not None else 0xFFFFFFFF
        pings = [0, 0]                                       # 0 = this PC
        if self.slot in (0, 1):
            pings[1 - self.slot] = value                     # the round trip belongs to the OTHER player
        return names, pings

    def rtt_ms(self):
        values = [p.rtt for p in self.peers.values() if p.rtt is not None]
        return max(values) if values else None

    def _advance(self, s):
        k, n = self.known[s], self.need[s]
        while n in k:
            n += 1
        self.need[s] = n

    # ---- receive -------------------------------------------------------------------------------------------------
    def _recv(self):
        while select.select([self.sock], [], [], 0)[0]:
            try:
                data, addr = self.sock.recvfrom(2048)
            except ConnectionResetError:
                # Windows reports an ICMP "port unreachable" (the other side is not listening: closed, restarted or
                # not started yet) as an error on the NEXT recvfrom. It carries no data: ignore it and keep going.
                continue
            except OSError:
                break
            via_relay = False
            if data[:4] == netlink.MAGIC:                    # online: something from the server
                if addr != self.relay_addr or data[4:5] != b'D':
                    continue                                 # (an old WHOAMI answer, a stranger ...)
                via_relay, data = True, data[5:]
            if data[:4] == SEL_MAGIC:                        # the host's menu selection (not a wire.py message)
                self._on_sel(data, addr, via_relay)
                continue
            try:
                m = wire.parse(data)
            except wire.ProtocolError:
                continue
            if m['session'] != self.session:
                continue
            peer = self.peers.get(m['member'])
            if m['kind'] == wire.HELLO:
                slot = m['slot']
                if peer is None and self.is_host and self.relay is None:   # online: peers come from the server
                    taken = {p.slot for p in self.peers.values()} | {self.slot}
                    if slot is None or slot not in self.active or slot in taken or m['member'] == 1:
                        continue
                    peer = self.peers[m['member']] = Peer(m['member'], slot, addr)
                    self.hash_need[m['member']] = 0
                if peer is None:
                    continue
                peer.slot, peer.hello = slot, True
                peer.name = clean_nick(m.get('name'))
            elif peer is None or not peer.hello:
                continue
            now_t = time.perf_counter()
            if via_relay:
                peer.relay_rx = now_t
            elif peer.strict and not self._known_ip(peer, addr):
                continue                                     # online: not from the player we expect
            else:
                peer.addr, peer.direct_rx = addr, now_t      # follows the peer if its NAT port changed
            self._clock(peer, m)
            if m['kind'] == wire.DATA:
                self._on_data(peer, m)
            elif m['kind'] == wire.BYE:
                self.closed_reason = m['reason']

    def _clock(self, peer, m):
        now = now_ms()
        peer.last_t, peer.last_rx = m['t_send'], now
        if m['echo']:
            rtt = (now - m['echo'] - m['hold']) & 0xFFFFFFFF
            if rtt < 5000:
                peer.rtt = rtt if peer.rtt is None else 0.8 * peer.rtt + 0.2 * rtt

    def _on_data(self, peer, m):
        peer.confirmed = True
        peer.ack = [max(a, b) for a, b in zip(peer.ack, m['acks'])]
        for slot, first, items in m['blocks']:
            if slot == self.slot or slot not in self.known:
                continue
            k = self.known[slot]
            for i, item in enumerate(items):
                k.setdefault(first + i, item)               # first copy wins; duplicates are identical
            self._advance(slot)
        if self.is_host:
            reports = self.reports.setdefault(peer.member, {})
            for f, h in m['hashes']:
                reports.setdefault(f, h)
            n = self.hash_need.get(peer.member, 0)
            while n in reports:
                n += 1
            self.hash_need[peer.member] = n
        else:
            peer.hash_ack = m['hash_ack']

    # ---- host-authoritative selection ----------------------------------------------------------------------------
    def _sel_send(self):
        """HOST: send the selection this PC sees in the menus to the guests. One PINE read (sel_seq) every 20 ms; the
        words are re-read only when it changes, and re-sent every 250 ms because UDP can lose a packet. The sequence
        number of the packet grows only when the selection changes (repeats carry the same number)."""
        if not (self.is_host and self.sel_sync) or not hasattr(self.core, 'selection_words'):
            return
        t = time.perf_counter()
        if t - self.sel_polled < 0.02:
            return
        self.sel_polled = t
        seq = self.core.sync_get('sel_seq')
        changed = seq != self.sel_seq
        if changed:
            self.sel_seq = seq
            self.sel_words = self.core.selection_words('cur')
            self.sel_tx_seq = (self.sel_tx_seq + 1) & 0xFFFFFFFF
        if not self.sel_words or not self.sel_words[0]:
            return                                           # nothing selected yet
        if changed or t - self.sel_last_tx >= 0.25:
            self.sel_last_tx = t
            pkt = struct.pack(SEL_FMT, SEL_MAGIC, self.member, self._sel_crc, self.sel_tx_seq, *self.sel_words)
            for p in self.peers.values():
                if p.hello:
                    self._tx(p, pkt)

    def _on_sel(self, data, addr, via_relay):
        """GUEST: the host's selection arrived. Packets older than the newest one applied are dropped (reordering);
        follow_selection() ignores repeats; the in-game np_sel_poll writes it only on the right screen (state 7 / 9)
        and only after the local selection was stable."""
        if self.is_host or not self.sel_sync or len(data) != SEL_LEN:
            return
        magic, member, crc, seq, *words = struct.unpack(SEL_FMT, data)
        if crc != self._sel_crc:
            return
        peer = self.peers.get(member)
        if peer is None or not peer.hello:
            return
        if not via_relay and peer.strict and not self._known_ip(peer, addr):
            return
        if self.sel_rx_seq is not None:
            diff = (seq - self.sel_rx_seq) & 0xFFFFFFFF
            if diff >= 0x80000000:                           # older than what we already applied
                return
        self.sel_rx_seq = seq
        if hasattr(self.core, 'follow_selection'):
            try:
                self.core.follow_selection(words)
            except Exception:                                # noqa: BLE001 - never stop a match for it
                pass

    # ---- core <-> node -------------------------------------------------------------------------------------------
    def _capture(self):
        if self.slot is None:
            return
        for f, item in sorted(self.core.captured(self.cap_next, 64).items()):
            self.known[self.slot][f + self.delay] = item
            self.cap_next = f + 1
        self._advance(self.slot)

    def _publish(self):
        for s in self.active:
            if self.published[s] < self.need[s]:
                frames = {f: self.known[s][f] for f in range(self.published[s], self.need[s])}
                self.core.publish(s, frames)
                self.published[s] = self.need[s]

    def _detect_shift(self, f):
        """k such that the host's hash at f+k equals the guest's hash at f (k > 0: the host is k logic ticks BEHIND).
        Returns None when no offset in -16..+16 explains the mismatch (a real divergence)."""
        mine = self.reports.get(self.member, {})
        for m, r in self.reports.items():
            if m == self.member or f not in r:
                continue
            for k in [sign * n for n in range(1, 17) for sign in (1, -1)]:
                if mine.get(f + k) == r[f]:
                    return k
        return None

    def _tick_step(self):
        """Compare the game's own tick counter (the aux word of each captured pad) of the two players on the SAME net
        frame. A constant offset of 0 is a healthy match; a change of the offset marks the moment the two games got
        out of step (for example when a round restarts)."""
        if len(self.active) < 2:
            return
        a, b = self.active[0], self.active[1]
        ka, kb = self.known[a], self.known[b]
        while self.tick_next in ka and self.tick_next in kb:
            f = self.tick_next
            self.tick_next = f + 1
            ta, tb = ka[f][1], kb[f][1]
            if self.watch_log and (ta, tb) != self._watch_last:     # --watch-log: every change, on both PCs
                self._watch_last = (ta, tb)
                if len(self.watch_events) < 2000:
                    self.watch_events.append((f, ta, tb))
            if 0xFFFFFFFF in (ta, tb):
                continue
            off = (tb - ta) & 0xFFFFFFFF
            if off >= 0x80000000:
                off -= 1 << 32
            if off != self._tick_off:
                self._tick_off = off
                if len(self.tick_log) < 60:
                    self.tick_log.append((f, ta, tb, off))

    def _release_hold(self):
        if self._tick_hold:
            self.core.put('hold', 0)
            self._tick_hold = False

    def _tick_barrier(self):
        """Optional (--tick-barrier, off by default). Uses the offset measured by _tick_step, i.e. the game counters
        of the two players on the SAME network frame, relative to the first offset seen, so the constant difference
        between the two PCs does not matter. Only the PC that is ahead parks, for at most TICK_HOLD_MAX seconds; after a
        timeout the current offset becomes the new baseline and the barrier rests for a second. The in-game filter also
        drops a hold == 2 by itself (NP_HOLD2_LIMIT), so the game can never freeze forever because of it."""
        if (not self.tick_barrier or not self.started or len(self.active) < 2
                or self.slot not in self.active or self._tick_off is None):
            self._release_hold()
            return
        now = time.perf_counter()
        off = self._tick_off                                 # guest - host, same net frame
        if self._tick_base is None:
            self._tick_base = off
        dev = off - self._tick_base
        local_ahead = (dev < 0) if self.member == 1 else (dev > 0)
        if self._tick_hold:
            timed_out = now - self._hold_t0 >= TICK_HOLD_MAX
            if timed_out or not local_ahead:
                self._release_hold()
                self._hold_cool = now + 1.0
                if timed_out:
                    self._tick_base = off
            return
        if local_ahead and now >= self._hold_cool:
            self.core.put('hold', 2)
            self._tick_hold = True
            self._hold_t0 = now

    def _hash_step(self):
        if not self.is_host:
            return
        local = self.core.hashes(self.hash_local_next, 64)
        mine = self.reports.setdefault(self.member, {})
        for f, h in sorted(local.items()):
            mine[f] = h
            self.hash_local_next = f + 1
        if len(self.peers) < self.expect:
            return
        top = min([self.hash_local_next] + [self.hash_need.get(m, 0) for m in self.peers])
        while self.checked < top:
            vals = {m: self.reports[m].get(self.checked) for m in [self.member, *self.peers]}
            if len(set(vals.values())) > 1:
                if NULL_STATE_HASH in vals.values():         # a scene change (no player objects on one PC): not a desync
                    self.soft_log.append((self.checked, vals))
                else:
                    self.desync_log.append((self.checked, vals))
                    k = self._detect_shift(self.checked)
                    if k != self._last_shift or not self.shift_log:
                        self._last_shift = k
                        self.shift_log.append((self.checked, k))
                    if self.desync_stop and self.desync is None:
                        self.desync = self.checked
            self.checked += 1
            if self.desync is not None:
                break
        for r in self.reports.values():                      # keep memory bounded
            for f in [f for f in r if f < self.checked - 64]:
                del r[f]

    # ---- send ----------------------------------------------------------------------------------------------------
    def _send_hello(self):
        t = time.perf_counter()
        if t - self.last_hello < 0.1:
            return
        self.last_hello = t
        for p in self.peers.values():
            if p.confirmed:
                continue
            pkt = wire.hello(self.member, self.session, p.seq, now_ms(), delay=self.delay, tag=self.tag,
                             name=self.name, slot=self.slot)
            p.seq += 1
            self._tx(p, pkt)

    def _send_data(self):
        now = now_ms()
        for p in self.peers.values():
            blocks = []
            for s in self.active:
                if s == p.slot:
                    continue                                  # a peer never needs its own inputs back
                if p.ack[s] < self.need[s]:
                    first = p.ack[s]
                    blocks.append((s, first, [self.known[s][f] for f in range(first, self.need[s])
                                              if f in self.known[s]]))
            hashes = []
            if not self.is_host:
                got = self.core.hashes(p.hash_ack, wire.MAX_HASHES)
                hashes = sorted(got.items())
            if not blocks and not hashes and (now - p.last_tx) & 0xFFFFFFFF < 30:
                continue
            pkt = wire.data(self.member, self.session, p.seq, now, frame=self.cap_next,
                            hash_ack=self.hash_need.get(p.member, 0), acks=[self.need.get(s, 0) for s in range(wire.SLOTS)],
                            blocks=blocks, hashes=hashes, echo=p.last_t, hold=(now - p.last_rx) & 0xFFFFFFFF)
            p.seq += 1
            p.last_tx = now
            self._tx(p, pkt)

    def _prune(self):
        for s in self.active:
            floor = self.published[s]
            for p in self.peers.values():
                if p.slot != s:
                    floor = min(floor, p.ack[s])
            for f in [f for f in self.known[s] if f < floor - 60]:
                del self.known[s][f]

    # ---- main entry ----------------------------------------------------------------------------------------------
    def pump(self):
        """Call in a loop (every 1-2 ms)."""
        self._keepalive()
        self._recv()
        self._send_hello()                                  # until every peer answered with DATA
        if not self.ready():
            return
        if not self.started:                                 # keep telling the peers we are here ...
            self._send_data()
            if all(p.confirmed for p in self.peers.values()):
                self.started = True                          # ... and go once every peer answered with DATA
            else:
                return
        self._capture()
        self._publish()
        self._tick_step()
        self._tick_barrier()
        self._hash_step()
        self._send_data()
        self._sel_send()
        self._prune()

    def close(self, reason=wire.BYE_DONE):
        self._release_hold()
        for p in self.peers.values():
            self._tx(p, wire.bye(self.member, self.session, p.seq, now_ms(), reason))
        self.sock.close()