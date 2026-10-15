"""Delay-based lockstep input exchange over UDP (2..N players, full mesh).

Idea: the input you press at local tick T is applied at tick T + delay on EVERY machine.
A tick may only run when the inputs of all players for that tick are known; otherwise you stall.
Because every PC feeds the same inputs to the same deterministic game, the simulations stay identical.

Packet (little endian):
  u8  sender id
  i32 ack          highest contiguous frame the sender has received FROM YOU (-1 = none)
  u32 first_frame  frame number of inputs[0]
  u8  count
  u64 * count      inputs for first_frame .. first_frame+count-1  (redundant: resent until acked)
UDP can lose packets, so every packet repeats all inputs the peer has not acknowledged yet.
"""
import select
import socket
import struct
import time

HDR = struct.Struct('<BiIB')
MAX_REDUNDANCY = 64


class Lockstep:
    def __init__(self, my_id, peers, bind_port, delay=3):
        """peers: {player_id: (ip, port)} for every OTHER player."""
        self.me = my_id
        self.peers = dict(peers)
        self.delay = delay
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(('0.0.0.0', bind_port))
        self.sock.setblocking(False)
        ids = [my_id] + list(self.peers)
        self.inputs = {pid: {} for pid in ids}            # pid -> {frame: value}
        self.recv_contig = {pid: delay - 1 for pid in self.peers}   # contiguous frames received from pid
        self.peer_ack = {pid: delay - 1 for pid in self.peers}      # contiguous frames pid received from me
        self.frame = 0                                    # next tick to run
        self.local_next = delay                           # next frame my local input is assigned to
        for pid in ids:                                   # frames 0..delay-1 are neutral input for everyone
            for f in range(delay):
                self.inputs[pid][f] = 0

    # ---- local input -----------------------------------------------------------------------
    def submit_local(self, value):
        """Call once per tick with this PC's pad state (packed into a u64)."""
        self.inputs[self.me][self.local_next] = value & 0xFFFFFFFFFFFFFFFF
        self.local_next += 1

    # ---- network ---------------------------------------------------------------------------
    def pump(self):
        """Receive everything pending and (re)send what peers are missing. Call often."""
        while select.select([self.sock], [], [], 0)[0]:
            data, _ = self.sock.recvfrom(2048)
            self._on_packet(data)
        for pid, addr in self.peers.items():
            first = self.peer_ack[pid] + 1
            last = self.local_next - 1
            if last < first:
                continue
            first = max(first, last - MAX_REDUNDANCY + 1)
            vals = [self.inputs[self.me][f] for f in range(first, last + 1)]
            pkt = HDR.pack(self.me, self.recv_contig[pid], first, len(vals)) + struct.pack(f'<{len(vals)}Q', *vals)
            self.sock.sendto(pkt, addr)

    def _on_packet(self, data):
        if len(data) < HDR.size:
            return
        pid, ack, first, count = HDR.unpack_from(data)
        if pid not in self.peers or len(data) != HDR.size + 8 * count:
            return
        vals = struct.unpack_from(f'<{count}Q', data, HDR.size)
        for i, v in enumerate(vals):
            self.inputs[pid].setdefault(first + i, v)      # first value wins (duplicates are identical)
        while (self.recv_contig[pid] + 1) in self.inputs[pid]:
            self.recv_contig[pid] += 1
        self.peer_ack[pid] = max(self.peer_ack[pid], ack)

    # ---- tick control ----------------------------------------------------------------------
    def ready(self):
        """True when every player's input for the current tick is known."""
        return all(self.frame in self.inputs[pid] for pid in self.inputs)

    def current_inputs(self):
        """{player_id: value} for the tick about to run. Only valid when ready()."""
        return {pid: self.inputs[pid][self.frame] for pid in self.inputs}

    def advance(self):
        """Call after the tick ran. Frees old frames."""
        old = self.frame - 120
        if old >= 0:
            for pid in self.inputs:
                self.inputs[pid].pop(old, None)
        self.frame += 1


def pick_delay(rtt_ms, tick_ms=16.7, margin=1):
    """Frames of delay needed to hide half the round trip (plus a safety margin)."""
    return max(1, int(rtt_ms / 2 / tick_ms) + margin + 1)
