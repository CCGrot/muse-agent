"""Lossless event payloads for standard MIDI type 0/1 with PPQ timing.

Edits re-encode delta times and running status, preserving unrelated event data.
Event identifiers refer to the specific source file and are never inferred by an LLM.
"""
from bisect import bisect_right
from collections import defaultdict, deque
from dataclasses import dataclass
import hashlib
import html
import struct


@dataclass
class Event:
    tick: int
    data: bytes


@dataclass
class Midi:
    format: int
    ppq: int
    tracks: list[list[Event]]


def vlq(value):
    if not 0 <= value <= 0x0FFFFFFF:
        raise ValueError("MIDI delta time exceeds the supported range")
    result = [value & 127]
    while value >> 7:
        value >>= 7
        result.insert(0, 128 | (value & 127))
    return bytes(result)


class Reader:
    def __init__(self, data):
        self.data, self.pos = data, 0

    def take(self, n):
        if n < 0 or self.pos + n > len(self.data):
            raise ValueError("Truncated MIDI")
        data = self.data[self.pos:self.pos + n]
        self.pos += n
        return data

    def byte(self):
        return self.take(1)[0]

    def vlq(self):
        value = 0
        for _ in range(4):
            b = self.byte()
            value = (value << 7) | (b & 127)
            if b < 128:
                return value
        raise ValueError("Invalid MIDI variable-length quantity")


def meta(kind, payload):
    return b"\xff" + bytes([kind]) + vlq(len(payload)) + payload


def payload(event):
    reader = Reader(event.data[2:])
    return reader.take(reader.vlq())


def loads(data):
    if len(data) > 32 * 1024 * 1024:
        raise ValueError("MIDI exceeds 32 MiB limit")
    r = Reader(data)
    if r.take(4) != b"MThd" or r.take(4) != b"\0\0\0\x06":
        raise ValueError("Expected a standard MIDI header")
    fmt, count, ppq = struct.unpack(">HHH", r.take(6))
    if fmt not in (0, 1) or not ppq or ppq & 0x8000 or not count or (fmt == 0 and count != 1):
        raise ValueError("Only MIDI type 0/1 with PPQ timing is supported")
    tracks = []
    for _ in range(count):
        if r.take(4) != b"MTrk":
            raise ValueError("Expected MIDI track chunk")
        tr = Reader(r.take(int.from_bytes(r.take(4), "big")))
        events, tick, running, ended = [], 0, None, False
        while tr.pos < len(tr.data):
            if ended:
                raise ValueError("Data after end-of-track")
            tick += tr.vlq()
            status = tr.byte()
            if status < 128:
                if running is None:
                    raise ValueError("Running status without previous channel event")
                tr.pos -= 1
                status = running
            if 128 <= status <= 239:
                running = status
                raw = tr.take(1 if status >> 4 in (12, 13) else 2)
                if any(x > 127 for x in raw):
                    raise ValueError("Invalid channel event data")
                event = Event(tick, bytes([status]) + raw)
            elif status == 255:
                kind = tr.byte()
                raw = tr.take(tr.vlq())
                if kind == 47:
                    if raw:
                        raise ValueError("Invalid end-of-track")
                    ended = True
                if kind == 81 and (len(raw) != 3 or not int.from_bytes(raw, "big")):
                    raise ValueError("Invalid MIDI tempo")
                event = Event(tick, meta(kind, raw))
            elif status in (240, 247):
                running = None
                raw = tr.take(tr.vlq())
                event = Event(tick, bytes([status]) + vlq(len(raw)) + raw)
            else:
                raise ValueError(f"Unsupported system status {status:#x}")
            events.append(event)
        if not ended:
            raise ValueError("Track is missing end-of-track")
        tracks.append(events)
    if r.pos != len(data):
        raise ValueError("Unexpected data after tracks")
    return Midi(fmt, ppq, tracks)


def dumps(midi):
    chunks = []
    for track in midi.tracks:
        end = max((e.tick for e in track), default=0)
        events = [e for e in track if e.data[:2] != b"\xff\x2f"]
        events = sorted(events, key=lambda e: e.tick) + [Event(end, meta(47, b""))]
        raw, previous = bytearray(), 0
        for event in events:
            raw.extend(vlq(event.tick - previous) + event.data)
            previous = event.tick
        chunks.append(b"MTrk" + struct.pack(">I", len(raw)) + raw)
    return b"MThd\0\0\0\x06" + struct.pack(">HHH", midi.format, len(chunks), midi.ppq) + b"".join(chunks)


def timeline(midi):
    tempos = {0: 500000}
    for tr in midi.tracks:
        for e in tr:
            if e.data[:2] == b"\xff\x51":
                tempos[e.tick] = int.from_bytes(payload(e), "big")
    ticks = sorted(tempos)
    times = [0.0]
    for a, b in zip(ticks, ticks[1:]):
        times.append(times[-1] + (b - a) * tempos[a] / midi.ppq / 1e6)

    def seconds(tick):
        i = bisect_right(ticks, tick) - 1
        return times[i] + (tick - ticks[i]) * tempos[ticks[i]] / midi.ppq / 1e6

    return seconds, [{"tick": t, "seconds": seconds(t), "bpm": 60000000 / tempos[t]} for t in ticks]


def inspect(midi):
    seconds, tempos = timeline(midi)
    notes, tracks, markers, warnings = [], [], [], set()
    active = defaultdict(deque)
    programs = defaultdict(int)
    volume = defaultdict(lambda: 100)
    expression = defaultdict(lambda: 127)
    pan = defaultdict(lambda: 64)
    merged = []
    for ti, track in enumerate(midi.tracks):
        name, port = f"Track {ti}", 0
        for ei, event in enumerate(track):
            if event.data[:2] == b"\xff\x03":
                name = payload(event).decode("utf-8", "replace")
            if event.data[:2] == b"\xff\x21":
                raw = payload(event)
                if len(raw) != 1:
                    raise ValueError("Invalid MIDI port")
                port = raw[0]
            merged.append((event.tick, ti, ei, port, event))
        tracks.append({"index": ti, "name": name})
    for tick, ti, ei, port, event in sorted(merged):
        d = event.data
        kind, channel = d[0] >> 4, d[0] & 15
        if d[:2] == b"\xff\x06":
            markers.append({"tick": tick, "beat": tick / midi.ppq, "seconds": seconds(tick), "text": payload(event).decode("utf-8", "replace")})
        if d[0] >= 240:
            if d[0] in (240, 247):
                warnings.add("SysEx retained; sound bank/device configuration may be required.")
            continue
        address = (port, channel)
        if kind == 12:
            programs[address] = d[1]
        elif kind == 9 and d[2]:
            note = {"id": f"t{ti}:e{ei}", "track": ti, "event": ei, "port": port, "channel": channel,
                    "program": programs[address], "pitch": d[1], "velocity": d[2], "start_tick": tick,
                    "start_beat": tick / midi.ppq, "start_seconds": seconds(tick),
                    "gain_at_onset": volume[address] * expression[address] / 127 ** 2,
                    "pan_at_onset": pan[address] / 63.5 - 1}
            active[(port, channel, d[1])].append(note)
            notes.append(note)
        elif kind == 8 or (kind == 9 and d[2] == 0):
            queue = active[(port, channel, d[1])]
            if not queue:
                warnings.add("Unmatched note-off retained.")
                continue
            note = queue.popleft()
            if tick <= note["start_tick"]:
                raise ValueError("Zero or negative length MIDI note")
            note.update(end_tick=tick, end_track=ti, end_event=ei,
                        duration_beats=(tick - note["start_tick"]) / midi.ppq,
                        duration_seconds=seconds(tick) - note["start_seconds"])
        elif kind in (10, 11, 13, 14):
            if kind == 11 and d[1] in (7, 10, 11):
                {7: volume, 10: pan, 11: expression}[d[1]][address] = d[2]
            else:
                warnings.add("Controllers, pressure and pitch bends are retained; built-in synthesis only applies volume, expression and pan at note onset.")
    if any(e.data[0] >> 4 == 11 and e.data[1] in (7, 10, 11) and e.tick > 0 for tr in midi.tracks for e in tr):
        warnings.add("Continuous volume, expression and pan are sampled at note onset by built-in synthesis; use SoundFont rendering for automation.")
    if any(active.values()):
        raise ValueError("Unclosed MIDI notes; repair note-offs before editing or rendering")
    duration = max((e.tick for tr in midi.tracks for e in tr), default=0)
    for track in tracks:
        ns = [n for n in notes if n["track"] == track["index"]]
        track.update(note_count=len(ns), channels=sorted({n["channel"] for n in ns}),
                     programs=sorted({n["program"] for n in ns}),
                     pitch_range=[min(n["pitch"] for n in ns), max(n["pitch"] for n in ns)] if ns else [])
    return {"format": midi.format, "ppq": midi.ppq, "duration_seconds": seconds(duration),
            "duration_beats": duration / midi.ppq, "tempo_map": tempos, "tracks": tracks,
            "markers": markers, "notes": notes, "warnings": sorted(warnings)}


def piano_roll(report):
    width, row = 1200, 150
    tracks = [t for t in report["tracks"] if t["note_count"]]
    height = max(100, 45 + row * len(tracks))
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
           f'<rect width="{width}" height="{height}" fill="#101820"/>',
           '<text x="15" y="23" fill="white" font-family="sans-serif">MIDI piano roll — horizontal axis: seconds; vertical axis: MIDI pitch</text>']
    duration = max(1, report["duration_seconds"])
    for index, track in enumerate(tracks):
        y = 45 + index * row
        out.append(f'<text x="15" y="{y+14}" fill="white" font-family="sans-serif">{track["index"]}: {html.escape(track["name"])}</text>')
        low, high = track["pitch_range"]
        for n in report["notes"]:
            if n["track"] != track["index"]:
                continue
            x = 180 + n["start_seconds"] / duration * 990
            ny = y + 25 + (high - n["pitch"]) / max(12, high - low + 1) * 100
            w = max(1, n["duration_seconds"] / duration * 990)
            title = html.escape(f'{n["id"]}: pitch {n["pitch"]}, {n["start_seconds"]:.2f}s, velocity {n["velocity"]}')
            out.append(f'<rect x="{x:.2f}" y="{ny:.2f}" width="{w:.2f}" height="5" fill="#72d6ad" opacity="{n["velocity"]/127:.2f}"><title>{title}</title></rect>')
    for i in range(11):
        out.append(f'<text x="{180+i*99}" y="{height-5}" fill="white" font-size="10">{duration*i/10:.1f}</text>')
    return "\n".join(out + ["</svg>"])


def sha256(data):
    return hashlib.sha256(data).hexdigest()
