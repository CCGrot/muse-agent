import copy
import json
import os
from pathlib import Path
import struct
import sys
import subprocess
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from muse_agent.midi import Midi, Event, loads, dumps, meta, inspect, sha256, piano_roll
from muse_agent.score import compile_score, demo_score, apply_patch, SCORE_SCHEMA
from muse_agent.context import source, visible, validate_url
from muse_agent.agent import command, decode_response, ask, launcher
from muse_agent.cli import main
from muse_agent.instruments import presets
from muse_agent.render import render, wav_stats


def fixture():
    return dumps(Midi(1, 480, [
        [Event(0, meta(3, b"Conductor")), Event(0, meta(81, (500000).to_bytes(3, "big"))),
         Event(480, meta(81, (1000000).to_bytes(3, "big"))), Event(960, meta(47, b""))],
        [Event(0, meta(3, b"Lead")), Event(0, bytes([192, 73])), Event(0, bytes([176, 64, 127])),
         Event(0, bytes([144, 60, 90])), Event(120, bytes([224, 1, 65])),
         Event(240, bytes([128, 60, 12])), Event(300, b"\xf0\x03\x7d\x01\xf7"),
         Event(480, bytes([144, 64, 75])), Event(720, bytes([144, 64, 0])),
         Event(800, bytes([176, 64, 0])), Event(960, meta(47, b""))]]))


def empty_patch(data):
    return {"source_sha256": sha256(data), "explanation": "Test", "edits": [], "delete_notes": [],
            "add_notes": [], "program_changes": [], "tempo_scale": 1}


class MidiTests(unittest.TestCase):
    def test_tempo_map_and_opaque_event_roundtrip(self):
        data = fixture()
        self.assertEqual(dumps(loads(data)), data)
        report = inspect(loads(data))
        self.assertAlmostEqual(report["duration_seconds"], 1.5)
        self.assertEqual(report["notes"][0]["duration_seconds"], 0.25)
        self.assertEqual(report["notes"][1]["duration_seconds"], 0.5)

    def test_edit_preserves_unrelated_events(self):
        data = fixture()
        before = loads(data)
        note = inspect(before)["notes"][0]
        p = empty_patch(data)
        p["edits"] = [{"note_id": note["id"], "pitch": 62, "velocity": 65, "start_beat": 0, "duration_beats": 0.5}]
        after = loads(apply_patch(data, p))
        self.assertEqual(before.tracks[0], after.tracks[0])
        for i, e in enumerate(before.tracks[1]):
            if i not in (note["event"], note["end_event"]):
                self.assertEqual(e, after.tracks[1][i])
        self.assertEqual(inspect(after)["notes"][0]["pitch"], 62)
        self.assertEqual(after.tracks[1][note["end_event"]].data[2], 12)

    def test_stale_patch_rejected(self):
        p = empty_patch(fixture())
        p["source_sha256"] = "bad"
        with self.assertRaisesRegex(ValueError, "hash"):
            apply_patch(fixture(), p)

    def test_tempo_scales_all_segments(self):
        p = empty_patch(fixture())
        p["tempo_scale"] = 2
        result = inspect(loads(apply_patch(fixture(), p)))
        self.assertEqual(result["duration_seconds"], 0.75)
        self.assertEqual([t["bpm"] for t in result["tempo_map"]], [240, 120])

    def test_tempo_scale_inserts_default_at_zero(self):
        midi = loads(fixture())
        midi.tracks[0] = [e for e in midi.tracks[0] if not (e.tick == 0 and e.data[:2] == b"\xff\x51")]
        data = dumps(midi)
        p = empty_patch(data)
        p["tempo_scale"] = 2
        self.assertEqual(inspect(loads(apply_patch(data, p)))["duration_seconds"], 0.75)

    def test_add_delete_and_extend(self):
        data = fixture()
        p = empty_patch(data)
        p["delete_notes"] = [inspect(loads(data))["notes"][0]["id"]]
        p["add_notes"] = [{"track": 1, "channel": 0, "pitch": 67, "velocity": 80, "start_beat": 3, "duration_beats": 1}]
        report = inspect(loads(apply_patch(data, p)))
        self.assertEqual([n["pitch"] for n in report["notes"]], [64, 67])
        self.assertEqual(report["duration_beats"], 4)

    def test_duplicate_edit_and_invalid_note_rejected(self):
        data = fixture()
        p = empty_patch(data)
        identity = inspect(loads(data))["notes"][0]["id"]
        p["delete_notes"] = [identity, identity]
        with self.assertRaisesRegex(ValueError, "duplicate"):
            apply_patch(data, p)

    def test_running_status_and_zero_velocity_off(self):
        raw = b"\x00\x90\x3c\x40\x78\x3c\x00\x00\xff\x2f\x00"
        data = b"MThd\0\0\0\x06" + struct.pack(">HHH", 0, 1, 480) + b"MTrk" + struct.pack(">I", len(raw)) + raw
        self.assertEqual(inspect(loads(data))["notes"][0]["duration_beats"], 0.25)

    def test_truncated_smpte_and_unclosed_notes_rejected(self):
        for data in [fixture()[:-2], fixture()[:12] + b"\xe7\x28" + fixture()[14:]]:
            with self.assertRaises(ValueError):
                loads(data)
        midi = loads(fixture())
        midi.tracks[1] = [e for e in midi.tracks[1] if e.data[0] != 128]
        with self.assertRaisesRegex(ValueError, "Unclosed"):
            inspect(midi)

    def test_svg_escapes_names(self):
        report = inspect(loads(fixture()))
        report["tracks"][1]["name"] = "<script>bad</script>"
        svg = piano_roll(report)
        self.assertIn("&lt;script&gt;", svg)
        self.assertNotIn("<script>", svg)


class ScoreTests(unittest.TestCase):
    def test_demo_deterministic_and_sections_preserved(self):
        data = dumps(compile_score(demo_score()))
        self.assertEqual(data, dumps(compile_score(demo_score())))
        report = inspect(loads(data))
        self.assertEqual(len(report["notes"]), 36)
        self.assertEqual(len(report["markers"]), 3)

    def test_invalid_scores(self):
        for mutate in [lambda s: s.update(tempo_bpm=float("nan")),
                       lambda s: s["tracks"][0].update(channel=9),
                       lambda s: s["tracks"][0]["notes"][0].update(pitch=128),
                       lambda s: s["tracks"][0]["notes"][0].update(duration=999),
                       lambda s: s["tracks"][1].update(channel=0)]:
            score = demo_score()
            mutate(score)
            with self.assertRaises(ValueError):
                compile_score(score)


class ContextTests(unittest.TestCase):
    def test_html_removes_script_and_styles(self):
        self.assertEqual(visible("<h1>River</h1><script>secret()</script><p>Fog &amp; bells</p>"), "River\nFog & bells")

    def test_repo_does_not_collect_secrets_or_generated_files(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "README.md").write_text("A windswept mountain game")
            (root / ".env").write_text("SECRET=123")
            (root / "credentials.json").write_text("PRIVATE")
            (root / "node_modules").mkdir()
            (root / "node_modules" / "README.md").write_text("IGNORE")
            result = source(str(root))
            self.assertIn("windswept", result["text"])
            self.assertNotIn("SECRET", result["text"])
            self.assertNotIn("PRIVATE", result["text"])
            self.assertNotIn("IGNORE", result["text"])

    def test_private_url_rejected(self):
        with patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", ("127.0.0.1", 80))]):
            with self.assertRaisesRegex(ValueError, "public"):
                validate_url("http://example.test/")


class ProviderTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows npm layout")
    def test_windows_launcher_avoids_batch_shell(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            native = root / "node_modules/@anthropic-ai/claude-code/bin/claude.exe"
            native.parent.mkdir(parents=True)
            native.touch()
            with patch("muse_agent.agent.executable", return_value=str(root / "claude.cmd")):
                self.assertEqual(launcher("claude"), [str(native)])

    def test_timeout_reports_original_failure_when_cleanup_denied(self):
        with patch("muse_agent.agent.command", return_value=["fake"]), patch("muse_agent.agent.subprocess.Popen") as popen, patch("muse_agent.agent.subprocess.run") as run:
            # Time is imported locally by ask; patch the stdlib function itself.
            with patch("time.monotonic", side_effect=[0, 0, 1, 2]), patch("muse_agent.agent.os.killpg", create=True, side_effect=PermissionError("denied")):
                process = popen.return_value
                process.communicate.side_effect = subprocess.TimeoutExpired("fake", 1)
                process.poll.return_value = None
                process.kill.side_effect = PermissionError("denied")
                run.return_value.returncode = 1
                with self.assertRaisesRegex(ValueError, "timed out.*Could not fully stop"):
                    ask("claude", "test", SCORE_SCHEMA, timeout=1)

    def test_provider_commands_and_envelopes(self):
        with tempfile.TemporaryDirectory() as temp, patch("muse_agent.agent.executable", return_value="provider"):
            root = Path(temp)
            schema, output = root / "schema.json", root / "out.json"
            schema.write_text(json.dumps(SCORE_SCHEMA))
            codex = command("codex", schema, output)
            self.assertIn("read-only", codex)
            self.assertEqual(codex[-1], "-")
            claude = command("claude", schema, output)
            self.assertEqual(claude[claude.index("--tools") + 1], "")
            output.write_text('{"ok": true}')
            self.assertEqual(decode_response("codex", "", output), {"ok": True})
            self.assertEqual(decode_response("claude", '{"structured_output":{"ok":true}}', output), {"ok": True})
            with self.assertRaises(ValueError):
                decode_response("claude", '{"is_error":true,"result":"failed"}', output)


class WorkflowTests(unittest.TestCase):
    def test_builtin_import_keeps_static_midi_mix(self):
        score = demo_score()
        report = inspect(loads(dumps(compile_score(score))))
        lead = report["notes"][0]
        self.assertAlmostEqual(lead["gain_at_onset"], round(score["tracks"][0]["gain"] * 127) / 127)
        self.assertAlmostEqual(lead["pan_at_onset"], round((score["tracks"][0]["pan"] + 1) * 63.5) / 63.5 - 1)

    def test_cli_demo_inspect_edit_render(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            demo, edited = root / "demo", root / "edited"
            self.assertEqual(main(["demo", "--output", str(demo), "--formats", "none"]), 0)
            source_path = demo / "composition.mid"
            before = source_path.read_bytes()
            p = empty_patch(before)
            p["tempo_scale"] = 1.25
            patchfile = root / "patch.json"
            patchfile.write_text(json.dumps(p))
            self.assertEqual(main(["apply", str(source_path), str(patchfile), "--output", str(edited), "--formats", "wav", "--engine", "builtin"]), 0)
            self.assertEqual(source_path.read_bytes(), before)
            self.assertGreater(wav_stats(edited / "audio.wav")["duration_seconds"], 2)
            self.assertTrue((edited / "changes.json").exists())
            self.assertEqual(main(["demo", "--output", str(demo)]), 1)

    def test_prepare_never_calls_provider(self):
        with tempfile.TemporaryDirectory() as temp, patch("muse_agent.cli.ask") as ask:
            root = Path(temp) / "prepared"
            self.assertEqual(main(["create", "Rain on a lake", "--prepare", "--output", str(root)]), 0)
            ask.assert_not_called()
            self.assertTrue((root / "prompt.txt").exists())
            self.assertFalse((root / "composition.mid").exists())

    def test_soundfont_preset_reader(self):
        def chunk(tag, data):
            return tag + struct.pack("<I", len(data)) + data + (b"\0" if len(data) % 2 else b"")
        row = struct.pack("<20sHHHIII", b"Bone flute", 73, 0, 0, 0, 0, 0)
        terminal = struct.pack("<20sHHHIII", b"EOP", 0, 0, 0, 0, 0, 0)
        data = chunk(b"RIFF", b"sfbk" + chunk(b"LIST", b"pdta" + chunk(b"phdr", row + terminal)))
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "test.sf2"
            path.write_bytes(data)
            self.assertEqual(presets(path), [{"name": "Bone flute", "program": 73, "bank": 0}])


if __name__ == "__main__":
    unittest.main()
