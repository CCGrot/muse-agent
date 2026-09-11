# Muse Agent

A CLI composition and MIDI revision workbench for Codex and Claude. Give it an
idea, a local repository, text files or a public webpage. The model interprets
theme, mood and dramatic arc, authors a structured score, and the local engine
validates it and exports MIDI, WAV and MP3. Continue with natural-language edits.

The musical starting point is Flint Chorus: deliberate sections, recurring motifs,
performer roles, expressive note values, and a separate instrument/rendering pass.
This is a standalone project with no imports from the original repository and no
prehistoric style restriction. It creates no HTML viewer. A static SVG piano roll
and JSON inspection report make existing MIDI inspectable by a person or coding agent.

## Quick start

Python 3.11+ and NumPy are required for audio synthesis. FFmpeg with libmp3lame is
required for MP3. A configured and authenticated Codex CLI or Claude Code CLI is
required for natural-language composition/editing. Their normal account usage
and charges apply; Muse Agent does not manage credentials or select a model for you.

```powershell
cd muse-agent
python -m venv .venv
.venv\Scripts\python -m pip install -e .
.venv\Scripts\muse-agent doctor
.venv\Scripts\muse-agent chat --provider codex
```

On macOS/Linux, use `.venv/bin/python` and `.venv/bin/muse-agent`. From a checkout
with NumPy already installed, `python muse.py` works without package installation.
`python -m muse_agent` works after installation.

Try the authored demonstration without a provider call:

```powershell
python muse.py demo --engine builtin --output runs/demo
```

This is one fixed demonstration, not a prompt-aware offline composer. If FFmpeg
is unavailable, add `--formats wav`. Every output directory must be new.

## Compose from context

```powershell
python muse.py create "A lonely coastal settlement, cautious hope, a melody that returns changed" --provider codex --bars 32
python muse.py create "Main menu and exploration theme" --source ../my-game --provider claude
python muse.py create "Music inspired by this place" --source https://example.com --source notes.md
python muse.py chat --provider claude --source ../my-game
```

In chat, describe the first piece, then type feedback such as “Make the second
section quieter and let the flute answer the bass.” `/new` begins another piece;
`/quit` exits. Each successful turn becomes a separate revision. `--midi file.mid`
starts chat from existing music. `session.json` records successful revisions;
restart with the current MIDI to continue later.

`--source` is repeatable (up to eight sources). Local repositories are sampled:
READMEs and other Markdown first, then selected source files; at most 30 files and
48,000 characters per source. Hidden files, common generated directories, named
credential files and symlinks are skipped. An explicit text-file source reads that
file directly. Context snapshots show exactly what was supplied and whether it
was truncated. Selected text is sent to your chosen provider. Review `--prepare`
output first if you need to inspect that text before submission. Automatic file
filtering is not a guarantee that a repository contains no secrets.

Web sources fetch bounded, visible HTML/text over public HTTP(S). No JavaScript,
login, recursive crawling, PDFs, audio transcription or remote Git cloning is
implemented. Clone a repository locally before passing it. Web/repo content is
labeled as reference data in the composition prompt. Providers run from a temporary
directory, not from the source repository. Your provider CLI's own account/global
configuration still applies. Run untrusted-source workflows in a suitable local
sandbox if stronger isolation is needed.

The model authors all notes explicitly. `--bars` requests a length; exact timing
is recorded in `score.json`. Local validation checks types, finite values, channel
allocation, pitch/velocity limits and score boundaries, with one provider repair
attempt for invalid scores. It does not certify musical quality. `--model NAME`
overrides the CLI's configured default; `--timeout SECONDS` controls each call.

## Inspect and revise existing MIDI

```powershell
python muse.py inspect song.mid --output runs/inspection
python muse.py edit song.mid "At 0:12–0:20, thin the accompaniment; preserve the melody" --provider claude
python muse.py chat --midi song.mid --provider codex
```

Inspection exports `inspection.json` and `piano-roll.svg`. The report contains
track indices, names, pitch ranges, note IDs, beat positions, second timestamps,
tempo changes, markers and MIDI warnings. The SVG can be opened in an image viewer
or supplied to an agent with image support. Editing providers receive the full
structured report; they do not automatically listen to audio or inspect the SVG.

Edits are hash-bound JSON patches: change/move/resize notes, add/delete notes,
swap a track/channel's program, or scale all tempo segments. Unrelated event
payloads and absolute ticks are retained, including CC, pressure, pitch bend,
SysEx and metadata. Delta times/running status are re-encoded; byte-for-byte
identity is not promised. Each revision includes an exact `source.mid` snapshot,
the patch, before/after hashes and an explanation. Input MIDI is never overwritten.
Use the new inspection report for the next patch because event IDs can change.

Supported import: standard MIDI types 0/1 with PPQ timing, polyphony, running
status and multiple tempo segments. Type 2, SMPTE timing, malformed/unclosed notes
and large files are rejected explicitly. Overlapping same-pitch notes are paired
FIFO. Large edit contexts (over 600 KB of report JSON) need an authored patch or
editing in a DAW. Program changes on channels shared by multiple tracks are
rejected. Patches cannot author controller automation, alter meter or create new
tracks yet; use a DAW for those operations.

## Instruments and audio

```powershell
python muse.py instruments "flute" --dir "D:/SoundFonts"
python muse.py create "A warm chamber miniature" --instrument-dir "D:/SoundFonts"
python muse.py render song.mid --engine builtin
python muse.py render song.mid --engine soundfont --soundfont "D:/SoundFonts/GeneralUser.sf2" --fluidsynth "D:/tools/fluidsynth.exe"
```

Discovery reads actual preset names, bank numbers and zero-based programs from
local SF2/SF3 files. It also searches standard system locations, including
MuseScore 4's sound directory on Windows. The composition agent receives the
inventory and gives instrument-specific advice. `instruments` provides search
queries/links for manual sourcing; it does not claim to have searched the web or
verified downloads. No sound banks are downloaded or redistributed.

Rendering supports:

- **SoundFont:** FluidSynth performs MIDI through a supplied/discovered bank.
  Supply FFmpeg for PCM conversion, normalization and MP3 encoding.
- **Built-in:** deterministic sine, flute, pluck, strings, brass, bell and drum
  proxies synthesized locally. Good for auditioning composition and exploring
  timbre; these are not realistic acoustic sample libraries.
- **Auto:** choose SoundFont when both a bank and FluidSynth are available;
  otherwise use built-in synthesis. The selected engine is recorded in `render.json`.

Set `MUSE_SOUNDFONT`, `MUSE_FLUIDSYNTH`, and `MUSE_FFMPEG`, or use their explicit
CLI flags. For example, on the development machine the existing renderer is at
`../.tools/fluidsynth-2.6.0/fluidsynth-v2.6.0-win10-x64-cpp11/bin/fluidsynth.exe`
and the installed bank is `C:/Program Files/MuseScore 4/sound/MS Basic.sf3`.
These paths are examples, not runtime dependencies on the original project.

MIDI stores instrument selections, not audio. GM program numbers assume a compatible
GM bank; arbitrary custom banks may map differently. Nonzero banks require MIDI
bank-select events authored separately. SFZ/Kontakt/VST hosting is outside this CLI.
Check a bank's own source, license, range, articulations and loop quality before use.

Built-in rendering samples volume, expression and pan at each note's onset; it
does not perform sustain, pitch bends, continuous automation or SysEx. Use
SoundFont/DAW rendering for those.
New score files additionally specify synthesis family, gain and pan for the first
render. A MIDI-only rerender uses program-based proxies, so custom score synthesis
choices may differ. Use `compile score.json` to reproduce those settings.

Exports use stereo 44.1 kHz 16-bit PCM WAV and FFmpeg/libmp3lame VBR quality 2 MP3,
with two seconds of release tail and downward-only peak normalization. Reports
record input/output hashes, sound source, duration and sample peak/RMS measurements.
MP3 is decoded and checked as well. This is technical validation, not a listening
review or true-peak mastering. Exports are not seamless loops. Rendering is limited
to ten minutes, one MIDI port and 30,000 notes per file.

`--formats both` is the default; choose `wav`, `mp3` or `none`. Errors return a
nonzero status and write `error.json` in an allocated run folder. Already-completed
MIDI/context artifacts are retained, so a failed audio export can be retried with
`render` into another directory.

## Use from an existing Codex or Claude session

The installed `muse-agent` executable is an ordinary CLI tool: no server or plugin
is required. The repository includes `AGENTS.md` and `CLAUDE.md` for agent guidance.
stdout is JSON; progress/errors go to stderr.

```powershell
python muse.py create "A mountain village after winter" --source ../my-game --prepare --output runs/brief
python muse.py schema score
# Read runs/brief/prompt.txt and response.schema.json; author a score JSON.
python muse.py compile my-score.json --output runs/composition

python muse.py edit song.mid "Soften the opening flute" --prepare --output runs/edit-brief
python muse.py schema patch
# Author a patch using the source hash and note IDs in the prepared report.
python muse.py apply runs/edit-brief/source.mid my-patch.json --output runs/revision
```

`create/edit --response FILE` accepts a score/patch supplied by the caller instead
of making a provider call. `compile` and `apply` are simpler when you already have
that data. See [examples/demo-score.json](examples/demo-score.json) for a complete
score and [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the design.

Provider integration follows [Codex non-interactive mode](https://developers.openai.com/codex/noninteractive)
and [Claude Code programmatic use](https://code.claude.com/docs/en/headless).
Codex runs with a read-only sandbox and a schema-constrained last-message file;
Claude uses JSON-schema output with built-in tools disabled and external MCP
configuration excluded. Prompts are supplied through stdin. Neither adapter uses
an approval-bypass flag. See the [Claude CLI reference](https://code.claude.com/docs/en/cli-reference)
for provider-specific behavior. Users remain responsible for their CLI configuration.

## Development

```powershell
python -m unittest discover -s tests -v
python muse.py demo --engine builtin --output runs/check
```

Tests cover MIDI event preservation, variable tempo, note pairing, patch hash
validation, context filtering, provider argument/response contracts, score bounds,
SoundFont preset parsing and an offline compose/edit/WAV workflow. No tests require
paid provider calls. Audio determinism requires the same tool/bank environment.

This repository is an initial implementation. Remaining work includes finer
controller editing, persistent musical memory across projects, richer synthesis
presets, bank-select authoring, optional sample-provider search integrations,
long-form section-by-section generation and automated audio listening/critique.
