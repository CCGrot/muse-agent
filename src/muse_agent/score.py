"""Small, explicit composition interchange and MIDI patch contracts."""
import copy
import math
from .midi import Midi, Event, meta, inspect, dumps, sha256


def obj(properties):
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


def array(items):
    return {"type": "array", "items": items}


STRING = {"type": "string"}
NUMBER = {"type": "number"}
INTEGER = {"type": "integer"}
NOTE = obj({"beat": NUMBER, "duration": NUMBER, "pitch": INTEGER, "velocity": INTEGER})
TRACK = obj({"name": STRING, "role": STRING, "program": INTEGER, "channel": INTEGER,
             "synth": {"type": "string", "enum": ["sine", "flute", "pluck", "strings", "brass", "bell", "drums"]},
             "pan": NUMBER, "gain": NUMBER, "notes": array(NOTE)})
SCORE_SCHEMA = obj({"title": STRING, "theme": STRING, "mood": array(STRING), "rationale": STRING,
                    "instrument_advice": array(STRING), "tempo_bpm": NUMBER,
                    "beats_per_bar": INTEGER, "length_beats": NUMBER,
                    "sections": array(obj({"name": STRING, "beat": NUMBER, "purpose": STRING})),
                    "tracks": array(TRACK)})
PATCH_SCHEMA = obj({"source_sha256": STRING, "explanation": STRING,
                    "edits": array(obj({"note_id": STRING, "pitch": INTEGER, "velocity": INTEGER,
                                        "start_beat": NUMBER, "duration_beats": NUMBER})),
                    "delete_notes": array(STRING),
                    "add_notes": array(obj({"track": INTEGER, "channel": INTEGER, "pitch": INTEGER,
                                            "velocity": INTEGER, "start_beat": NUMBER, "duration_beats": NUMBER})),
                    "program_changes": array(obj({"track": INTEGER, "channel": INTEGER, "program": INTEGER})),
                    "tempo_scale": NUMBER})


def validate_shape(value, schema, path="response"):
    kind = schema["type"]
    valid = {"object": isinstance(value, dict), "array": isinstance(value, list),
             "string": isinstance(value, str), "integer": type(value) is int,
             "number": type(value) in (int, float) and math.isfinite(value)}[kind]
    if not valid:
        raise ValueError(f"{path}: expected {kind}")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"{path}: expected one of {schema['enum']}")
    if kind == "object":
        if set(value) != set(schema["properties"]):
            raise ValueError(f"{path}: fields must be {list(schema['properties'])}")
        for name, child in schema["properties"].items():
            validate_shape(value[name], child, f"{path}.{name}")
    elif kind == "array":
        for i, item in enumerate(value):
            validate_shape(item, schema["items"], f"{path}[{i}]")


def bounded(value, low, high, name):
    if not low <= value <= high:
        raise ValueError(f"{name} must be between {low} and {high}")


def note_values(n, start="beat", duration="duration"):
    bounded(n["pitch"], 0, 127, "pitch")
    bounded(n["velocity"], 1, 127, "velocity")
    bounded(n[start], 0, 100000, start)
    bounded(n[duration], 1 / 480, 100000, duration)


def compile_score(score):
    validate_shape(score, SCORE_SCHEMA)
    bounded(score["tempo_bpm"], 20, 300, "tempo_bpm")
    bounded(score["beats_per_bar"], 1, 16, "beats_per_bar")
    bounded(score["length_beats"], 1, 3600, "length_beats")
    bounded(len(score["tracks"]), 1, 16, "track count")
    if sum(len(t["notes"]) for t in score["tracks"]) > 30000:
        raise ValueError("Score exceeds 30000 notes")
    if not any(t["notes"] for t in score["tracks"]):
        raise ValueError("Score has no notes")
    end = round(score["length_beats"] * 480)
    conductor = [Event(0, meta(3, score["title"].encode())),
                 Event(0, meta(81, round(60000000 / score["tempo_bpm"]).to_bytes(3, "big"))),
                 Event(0, meta(88, bytes([score["beats_per_bar"], 2, 24, 8])))]
    for section in score["sections"]:
        bounded(section["beat"], 0, score["length_beats"], "section beat")
        conductor.append(Event(round(section["beat"] * 480), meta(6, section["name"].encode())))
    conductor.append(Event(end, meta(47, b"")))
    tracks, channels = [conductor], set()
    for track in score["tracks"]:
        ch = track["channel"]
        bounded(ch, 0, 15, "channel (zero based)")
        bounded(track["program"], 0, 127, "program (zero based)")
        bounded(track["pan"], -1, 1, "pan")
        bounded(track["gain"], 0, 1, "gain")
        if ch in channels:
            raise ValueError("Each composed track must have its own MIDI channel")
        channels.add(ch)
        if (ch == 9) != (track["synth"] == "drums"):
            raise ValueError("Use channel 9 exclusively for drums")
        events = [Event(0, meta(3, track["name"].encode())), Event(0, bytes([192 | ch, track["program"]])),
                  Event(0, bytes([176 | ch, 10, round((track["pan"] + 1) * 63.5)])),
                  Event(0, bytes([176 | ch, 7, round(track["gain"] * 127)]))]
        for note in track["notes"]:
            note_values(note)
            if note["beat"] + note["duration"] > score["length_beats"] + 1e-7:
                raise ValueError("Note extends beyond length_beats")
            start, stop = round(note["beat"] * 480), round((note["beat"] + note["duration"]) * 480)
            if stop <= start:
                raise ValueError("Note rounds to zero MIDI ticks")
            events += [Event(start, bytes([144 | ch, note["pitch"], note["velocity"]])),
                       Event(stop, bytes([128 | ch, note["pitch"], 0]))]
        events.sort(key=lambda e: (e.tick, 0 if e.data[0] >> 4 == 8 else 1))
        events.append(Event(end, meta(47, b"")))
        tracks.append(events)
    midi = Midi(1, 480, tracks)
    # IDs are always assigned after canonical serialization.
    inspect(midi)
    return midi


def apply_patch(source, patch):
    from .midi import loads, payload
    validate_shape(patch, PATCH_SCHEMA)
    if patch["source_sha256"] != sha256(source):
        raise ValueError("Patch source hash does not match this MIDI; inspect the current version again")
    midi = loads(source)
    report = inspect(midi)
    notes = {n["id"]: n for n in report["notes"]}
    result = copy.deepcopy(midi)
    removed, changed = set(), set()
    for identity in patch["delete_notes"]:
        if identity not in notes or identity in changed:
            raise ValueError(f"Unknown or duplicate note id: {identity}")
        changed.add(identity)
        n = notes[identity]
        removed.update([(n["track"], n["event"]), (n["end_track"], n["end_event"])])
    for edit in patch["edits"]:
        identity = edit["note_id"]
        if identity not in notes or identity in changed:
            raise ValueError(f"Unknown or duplicate note id: {identity}")
        changed.add(identity)
        note_values(edit, "start_beat", "duration_beats")
        n = notes[identity]
        on, off = result.tracks[n["track"]][n["event"]], result.tracks[n["end_track"]][n["end_event"]]
        on.tick = round(edit["start_beat"] * midi.ppq)
        off.tick = round((edit["start_beat"] + edit["duration_beats"]) * midi.ppq)
        if off.tick <= on.tick:
            raise ValueError("Edited note rounds to zero ticks")
        on.data = bytes([on.data[0], edit["pitch"], edit["velocity"]])
        off.data = bytes([off.data[0], edit["pitch"], off.data[2]])
    for ti, track in enumerate(result.tracks):
        result.tracks[ti] = [e for ei, e in enumerate(track) if (ti, ei) not in removed]
    for note in patch["add_notes"]:
        note_values(note, "start_beat", "duration_beats")
        bounded(note["track"], 0, len(result.tracks) - 1, "track")
        bounded(note["channel"], 0, 15, "channel")
        ch = note["channel"]
        start = round(note["start_beat"] * midi.ppq)
        end = round((note["start_beat"] + note["duration_beats"]) * midi.ppq)
        if end <= start:
            raise ValueError("Added note rounds to zero ticks")
        result.tracks[note["track"]] += [Event(start, bytes([144 | ch, note["pitch"], note["velocity"]])),
                                        Event(end, bytes([128 | ch, note["pitch"], 0]))]
    for change in patch["program_changes"]:
        bounded(change["track"], 0, len(result.tracks) - 1, "track")
        bounded(change["channel"], 0, 15, "channel")
        bounded(change["program"], 0, 127, "program")
        # Program state is channel-wide; ambiguous shared channels need a DAW.
        users = {n["track"] for n in report["notes"] if n["channel"] == change["channel"]}
        if users - {change["track"]}:
            raise ValueError("Program edit would affect another track sharing the MIDI channel")
        track = result.tracks[change["track"]]
        status = 192 | change["channel"]
        existing = [e for e in track if e.data[0] == status]
        for event in existing:
            event.data = bytes([status, change["program"]])
        if not existing:
            track.insert(0, Event(0, bytes([status, change["program"]])))
    bounded(patch["tempo_scale"], 0.25, 4, "tempo_scale")
    if patch["tempo_scale"] != 1:
        tempo_events = [e for tr in result.tracks for e in tr if e.data[:2] == b"\xff\x51"]
        if not any(e.tick == 0 for e in tempo_events):
            event = Event(0, meta(81, (500000).to_bytes(3, "big")))
            result.tracks[0].insert(0, event)
            tempo_events.append(event)
        for e in tempo_events:
            tempo = round(int.from_bytes(payload(e), "big") / patch["tempo_scale"])
            bounded(tempo, 1, 0xFFFFFF, "MIDI tempo")
            e.data = meta(81, tempo.to_bytes(3, "big"))
    encoded = dumps(result)
    inspect(loads(encoded))
    return encoded


def demo_score():
    """Authored short fixture: no claim that offline mode understands a prompt."""
    motif = [62, 65, 69, 67, 65, 64, 62]
    lead = []
    for phrase in range(4):
        for i, pitch in enumerate(motif):
            lead.append({"beat": phrase * 8 + i, "duration": 0.8 if i < 6 else 1.6,
                         "pitch": pitch + (12 if phrase == 2 else 0), "velocity": 68 + phrase * 5})
    return {"title": "Embers Before Dawn", "theme": "An authored demonstration of motif, departure and return",
            "mood": ["reflective", "hopeful"], "rationale": "A seven-note idea returns over a changing bass; the third phrase rises an octave.",
            "instrument_advice": ["The built-in flute and pluck are synthesized proxies. Try a flute and nylon guitar in a GM SoundFont."],
            "tempo_bpm": 88, "beats_per_bar": 4, "length_beats": 32,
            "sections": [{"name": name, "beat": beat, "purpose": purpose} for name, beat, purpose in
                         [("Embers", 0, "State the idea"), ("First light", 16, "Open the register"), ("Return", 24, "Resolve")]],
            "tracks": [{"name": "Reed melody", "role": "lead", "program": 73, "channel": 0, "synth": "flute", "pan": -0.2, "gain": 0.8, "notes": lead},
                       {"name": "Plucked ground", "role": "bass", "program": 24, "channel": 1, "synth": "pluck", "pan": 0.2, "gain": 0.65,
                        "notes": [{"beat": i * 4, "duration": 3.5, "pitch": p, "velocity": 62} for i, p in enumerate([38, 45, 41, 43, 46, 45, 43, 38])]}]}
