# Validation record

Verified on Windows with Python 3.13.5, NumPy, FFmpeg 8.0.1 and FluidSynth 2.6.0
during initial development on 2026-09-10. Check the local run reports for exact
audio hashes, measurements and provider responses. Generated runs are ignored by Git.

- 22 automated tests pass, including note/event preservation, variable tempo,
  malformed input, stale patches, context filtering, Windows provider launch,
  timeout reporting, SoundFont preset tables and offline MIDI/edit/WAV export.
- Editable installation in the project virtual environment succeeds;
  `muse-agent --version` reports `0.1.0`.
- Built-in synthesis exports the fixed demo to WAV and MP3. MP3 decode validation
  and WAV duration/silence/sample-peak checks pass.
- The original opening-theme reference renders through the installed MS Basic
  bank. The final floating-point SoundFont pipeline was separately verified on
  the demo, including downward normalization and raw-duration checks.
- The existing performed “Under the First Sky” imports with all seven tempo
  segments, eight markers and six note-bearing tracks. Controller warnings are
  reported rather than treating an oscillator preview as faithful performance.
- Local SoundFont discovery finds 309 presets in MS Basic, including flute and
  pan-flute variants with actual bank/program numbers.
- A real Codex call authors a four-bar lighthouse cue, yielding 18 MIDI notes,
  notes/section/instrument advice, SVG, WAV and MP3 in `runs/codex-live`.
- A real Claude call edits that MIDI in `runs/claude-live-edit`, changing exactly
  the 13 flute velocities. All pitches, starts, ends, programs and bass notes
  remain unchanged, and the source snapshot matches the original bytes. The
  revision exports WAV and MP3 successfully.
- Repository context preparation against the original project succeeds and
  records the selected text and truncation information.

The provider calls required network-capable execution outside the development
sandbox. Sandboxed attempts timed out; the implementation now reports timeout
and process-cleanup failures explicitly. Authentication is owned by the provider
CLIs and is not exercised by the automated test suite.

No auditory quality judgment was made. No live webpage integration test,
macOS/Linux execution test, long-form composition evaluation, or compatibility
claim for arbitrary MIDI devices/sample banks is included in this record.
