"""Wire format of a star-topology lockstep session (own implementation of the same design as TTM Online Kit's
protocol v4, simplified: no RLE, no scheduled writes). UDP, little endian, one datagram per message.

Star: every playing PC sends its own inputs to the host; the host forwards every slot's inputs to everyone.
Only the host compares state hashes (every PC reports its per-frame hashes to the host).

Header : magic, version, kind, member id (host = 1), session id, seq, sender clock (ms), echo of the newest clock
         received from the other side, hold time (ms) -> RTT = now - echo - hold.
HELLO  : input delay, 8 bytes of the savestate/build tag, 16-byte name, input slot.
DATA   : sender's game frame, hash_ack (next hash frame the sender needs), acks[SLOTS] (per slot: next frame the
         sender still needs, everything below has arrived), input BLOCKS (slot, count, first, count x 12 bytes),
         hash reports (frame, hash).
         Redundancy: every DATA repeats ALL frames the receiver has not acknowledged, so a lost datagram costs only
         the next one.
BYE    : a reason code.
"""
import hashlib
import struct

MAGIC, VERSION = b'NPG1', 1
HELLO, DATA, BYE = 1, 2, 3
SLOTS = 4                       # input slots of the game (players)
RAW = 24                        # rdata[0..23] of one pad (status, mode, buttons, sticks, pressures)
NO_SLOT = 0xFF
# An idle DualShock 2 read: status 0, mode 0x79, no button (active LOW: FF), sticks centred, no pressure.
# NEVER use zeros as 'neutral': that is every button pressed and both sticks in a corner.
IDLE_PAD = bytes([0x00, 0x79, 0xFF, 0xFF, 0x7F, 0x7F, 0x7F, 0x7F]) + bytes(RAW - 8)
MAX_DATA = 1400                 # below every common path MTU: no fragmentation
MAX_HASHES = 32

HEADER = struct.Struct('<4sBBBIIIIH')
HELLO_BODY = struct.Struct('<H8s16sB')
DATA_BODY = struct.Struct(f'<II{SLOTS}IBB')     # frame, hash_ack, acks[SLOTS], nblocks, nhash
BLOCK = struct.Struct('<BBI')                   # slot, count, first frame
ENTRY = struct.Struct(f'<{RAW}sI')              # raw pad, aux word
HASH = struct.Struct('<II')                     # frame, hash
BYE_DONE, BYE_ABORTED, BYE_DESYNC, BYE_ERROR = 1, 2, 3, 4


class ProtocolError(ValueError):
    pass


def header(kind, member, session, seq, t_send, echo=0, hold=0):
    return HEADER.pack(MAGIC, VERSION, kind, member & 0xFF, session & 0xFFFFFFFF, seq & 0xFFFFFFFF,
                       t_send & 0xFFFFFFFF, echo & 0xFFFFFFFF, min(max(int(hold), 0), 0xFFFF))


def hello(member, session, seq, t_send, *, delay, tag, name='', slot=None, echo=0, hold=0):
    tag = bytes.fromhex(tag[:16]) if isinstance(tag, str) else bytes(tag[:8])
    return header(HELLO, member, session, seq, t_send, echo, hold) + HELLO_BODY.pack(
        delay, tag.ljust(8, b'\0'), name.encode('utf-8')[:16].ljust(16, b'\0'), NO_SLOT if slot is None else slot)


def data(member, session, seq, t_send, *, frame, hash_ack, acks, blocks=(), hashes=(), echo=0, hold=0):
    """blocks: [(slot, first_frame, [(raw8, aux), ...])]; hashes: [(frame, hash), ...]."""
    hashes = list(hashes)[:MAX_HASHES]
    blocks = [(s, f, list(items)) for s, f, items in blocks if items]
    room = (MAX_DATA - HEADER.size - DATA_BODY.size - len(blocks) * BLOCK.size - len(hashes) * HASH.size) // ENTRY.size
    share = room // len(blocks) if blocks else 0
    acks = (list(acks) + [0] * SLOTS)[:SLOTS]
    out = [header(DATA, member, session, seq, t_send, echo, hold),
           DATA_BODY.pack(frame & 0xFFFFFFFF, hash_ack & 0xFFFFFFFF, *(a & 0xFFFFFFFF for a in acks),
                          len(blocks), len(hashes))]
    for s, first, items in blocks:
        items = items[:min(share, 255)]
        out.append(BLOCK.pack(s, len(items), first & 0xFFFFFFFF))
        out += [ENTRY.pack(bytes(raw), aux & 0xFFFFFFFF) for raw, aux in items]
    out += [HASH.pack(f & 0xFFFFFFFF, h & 0xFFFFFFFF) for f, h in hashes]
    datagram = b''.join(out)
    assert len(datagram) <= MAX_DATA
    return datagram


def bye(member, session, seq, t_send, reason):
    return header(BYE, member, session, seq, t_send) + struct.pack('<H', reason)


def parse(datagram):
    if len(datagram) < HEADER.size:
        raise ProtocolError('short datagram')
    magic, version, kind, member, session, seq, t_send, echo, hold = HEADER.unpack_from(datagram)
    if magic != MAGIC or version != VERSION:
        raise ProtocolError('not a datagram of this protocol')
    out = dict(kind=kind, member=member, session=session, seq=seq, t_send=t_send, echo=echo, hold=hold)
    body = datagram[HEADER.size:]
    if kind == HELLO:
        if len(body) != HELLO_BODY.size:
            raise ProtocolError('bad HELLO')
        delay, tag, name, slot = HELLO_BODY.unpack(body)
        out.update(delay=delay, tag=tag.hex(), name=name.rstrip(b'\0').decode('utf-8', 'replace'),
                   slot=None if slot == NO_SLOT else slot)
    elif kind == DATA:
        if len(body) < DATA_BODY.size:
            raise ProtocolError('short DATA')
        f = DATA_BODY.unpack_from(body)
        frame, hash_ack, acks, nblocks, nhash = f[0], f[1], list(f[2:2 + SLOTS]), f[2 + SLOTS], f[3 + SLOTS]
        if nblocks > SLOTS or nhash > MAX_HASHES:
            raise ProtocolError('bad counts')
        at, blocks = DATA_BODY.size, []
        for _ in range(nblocks):
            if len(body) < at + BLOCK.size:
                raise ProtocolError('short block')
            slot, count, first = BLOCK.unpack_from(body, at)
            at += BLOCK.size
            if slot >= SLOTS or len(body) < at + count * ENTRY.size:
                raise ProtocolError('bad block')
            blocks.append((slot, first, [ENTRY.unpack_from(body, at + i * ENTRY.size) for i in range(count)]))
            at += count * ENTRY.size
        if len(body) != at + nhash * HASH.size:
            raise ProtocolError('bad DATA size')
        hashes = [HASH.unpack_from(body, at + i * HASH.size) for i in range(nhash)]
        out.update(frame=frame, hash_ack=hash_ack, acks=acks, blocks=blocks, hashes=hashes)
    elif kind == BYE:
        if len(body) != 2:
            raise ProtocolError('bad BYE')
        out['reason'] = struct.unpack('<H', body)[0]
    else:
        raise ProtocolError(f'unknown kind {kind}')
    return out


def session_id(tag, delay, epoch=0):
    """Same id on every PC iff they agree on build/savestate tag, input delay and epoch (a rematch = next epoch):
    PCs that disagree never talk to each other."""
    digest = hashlib.sha256(f'v1:{tag}:{delay}:{epoch}'.encode()).digest()
    return struct.unpack('<I', digest[:4])[0] or 1