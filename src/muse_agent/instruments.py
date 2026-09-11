"""Discover local SoundFonts and read their real preset names without loading samples."""
import os
from pathlib import Path
import struct
import urllib.parse


def presets(path):
    result = []
    with Path(path).open("rb") as f:
        header = f.read(12)
        if len(header) != 12 or header[:4] != b"RIFF" or header[8:] != b"sfbk":
            raise ValueError("Not a RIFF SoundFont")
        size = Path(path).stat().st_size
        end = 8 + struct.unpack("<I", header[4:8])[0]
        if end > size:
            raise ValueError("Truncated SoundFont")
        while f.tell() + 8 <= end:
            kind, length = struct.unpack("<4sI", f.read(8))
            start = f.tell()
            if start + length > end:
                raise ValueError("Invalid SoundFont chunk length")
            if kind == b"LIST" and length >= 4 and f.read(4) == b"pdta":
                while f.tell() + 8 <= start + length:
                    sub, n = struct.unpack("<4sI", f.read(8))
                    substart = f.tell()
                    if substart + n > start + length:
                        raise ValueError("Invalid preset chunk length")
                    if sub == b"phdr":
                        if n % 38 or n > 38 * 65536:
                            raise ValueError("Invalid SoundFont preset table")
                        rows = f.read(n)
                        for offset in range(0, n - 38, 38):
                            name, program, bank = struct.unpack("<20sHH", rows[offset:offset + 24])
                            result.append({"name": name.split(b"\0")[0].decode("utf-8", "replace"), "program": program, "bank": bank})
                    f.seek(substart + n + (n % 2))
            f.seek(start + length + (length % 2))
    return result


def default_roots():
    roots = []
    if os.environ.get("MUSE_SOUNDFONT"):
        roots.append(Path(os.environ["MUSE_SOUNDFONT"]))
    if os.name == "nt":
        roots += [Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "MuseScore 4" / "sound"]
    else:
        roots += [Path("/usr/share/sounds/sf2"), Path("/usr/share/soundfonts"), Path("/usr/local/share/soundfonts")]
    return roots


def discover(roots=(), query=""):
    candidates, seen = [], set()
    for root in [*default_roots(), *(Path(p).expanduser() for p in roots)]:
        if root.is_file():
            candidates.append(root)
        elif root.is_dir():
            for count, (directory, dirs, files) in enumerate(os.walk(root, followlinks=False)):
                dirs[:] = sorted(d for d in dirs if not d.startswith(".") and not (Path(directory) / d).is_symlink())
                candidates += [Path(directory) / f for f in sorted(files) if Path(f).suffix.lower() in (".sf2", ".sf3") and not (Path(directory) / f).is_symlink()]
                if count >= 500:
                    break
    banks = []
    for path in candidates[:100]:
        path = path.resolve()
        if path in seen:
            continue
        seen.add(path)
        try:
            programs = presets(path)
            matches = [p for p in programs if query.lower() in p["name"].lower()]
            banks.append({"path": str(path), "preset_count": len(programs), "presets": matches,
                          "license": "Consult the bank's accompanying license; discovery does not establish redistribution rights."})
        except (OSError, ValueError, struct.error) as exc:
            banks.append({"path": str(path), "error": str(exc)})
    return {"banks": banks, "query": query,
            "advice": ["MIDI contains notes and program selections, not instrument audio.",
                       "For automatic sampled rendering use an SF2/SF3 bank and FluidSynth. GM banks map programs predictably.",
                       "SFZ, Kontakt and VST instruments require a compatible sampler/DAW; this CLI does not host them.",
                       "Audition the same MIDI phrase, check playable range and articulations, then compare at matched loudness.",
                       "Record the download source, version and license alongside any bank you acquire."],
            "search_queries": [f'{query or "General MIDI"} SoundFont SF2 license samples', f'{query or "acoustic instrument"} SFZ multisamples license'],
            "search_links": ["https://www.google.com/search?q=" + urllib.parse.quote_plus(f'{query or "General MIDI"} SoundFont SF2 license')],
            "synthesis": ["sine", "flute", "pluck", "strings", "brass", "bell", "drums"]}
