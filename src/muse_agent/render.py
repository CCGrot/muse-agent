"""Offline SoundFont rendering or deterministic additive/subtractive sketch synthesis."""
import hashlib
import math
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import wave
from .midi import loads, inspect, sha256


def run(args):
    try:
        result = subprocess.run([str(a) for a in args], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600)
    except subprocess.TimeoutExpired as exc:
        raise ValueError("Audio renderer timed out") from exc
    if result.returncode:
        raise ValueError(f"{Path(str(args[0])).name} failed: {result.stderr[-2000:]}")
    return result


def file_hash(path):
    with Path(path).open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def synth_name(program, channel):
    if channel == 9:
        return "drums"
    if 72 <= program <= 79:
        return "flute"
    if 24 <= program <= 39:
        return "pluck"
    if 40 <= program <= 55:
        return "strings"
    if 56 <= program <= 71:
        return "brass"
    if 8 <= program <= 15:
        return "bell"
    return "sine"


def synthesize(report, path, score=None, rate=44100):
    import numpy as np
    duration = report["duration_seconds"] + 2
    if not 0 < duration <= 602:
        raise ValueError("Built-in synthesis supports up to ten minutes per render")
    mix = np.zeros((math.ceil(duration * rate), 2), dtype=np.float32)
    settings = {i + 1: t for i, t in enumerate(score["tracks"])} if score else {}
    rng = np.random.default_rng(0)
    for n in report["notes"]:
        config = settings.get(n["track"], {})
        kind = config.get("synth", synth_name(n["program"], n["channel"]))
        gain = config.get("gain", n["gain_at_onset"])
        pan = config.get("pan", n["pan_at_onset"])
        held = n["duration_seconds"]
        release = 0.65 if kind in ("strings", "bell") else 0.15
        length = min(len(mix) - round(n["start_seconds"] * rate), math.ceil((held + release) * rate))
        t = np.arange(length, dtype=np.float64) / rate
        hz = 440 * 2 ** ((n["pitch"] - 69) / 12)
        phase = 2 * np.pi * hz * t
        attack = 0.08 if kind == "strings" else 0.015
        envelope = np.minimum(1, t / attack) * np.clip((held + release - t) / release, 0, 1)
        signal = np.sin(phase) if hz < rate * 0.45 else np.zeros_like(t)
        if kind in ("flute", "strings", "brass", "pluck"):
            weights = {"flute": [0.22, 0.07], "strings": [0.45, 0.3, 0.18, 0.1], "brass": [0.65, 0.4, 0.24], "pluck": [0.5, 0.3, 0.14]}[kind]
            for harmonic, weight in enumerate(weights, 2):
                if hz * harmonic < rate * 0.45:
                    signal += weight * np.sin(phase * harmonic)
            if kind == "pluck":
                envelope *= np.exp(-t * 2.3)
        elif kind == "bell":
            for ratio, weight in [(2.76, 0.4), (5.4, 0.17)]:
                if hz * ratio < rate * 0.45:
                    signal += weight * np.sin(phase * ratio) * np.exp(-t * 3)
            envelope *= np.exp(-t * 1.8)
        elif kind == "drums":
            noise = rng.uniform(-1, 1, len(t))
            if n["pitch"] in (35, 36):
                signal = np.sin(2 * np.pi * (50 * t + 8 * (1 - np.exp(-t * 25)))) * np.exp(-t * 12)
            elif n["pitch"] in (42, 44, 46):
                signal = (noise - np.roll(noise, 1)) * np.exp(-t * 35) * 0.4
            else:
                signal = (noise * 0.7 + np.sin(2 * np.pi * 170 * t) * 0.3) * np.exp(-t * 18)
        signal = (signal * envelope * (n["velocity"] / 127) ** 1.5 * gain * 0.2).astype(np.float32)
        start = round(n["start_seconds"] * rate)
        angle = (pan + 1) * np.pi / 4
        mix[start:start + length, 0] += signal * np.cos(angle)
        mix[start:start + length, 1] += signal * np.sin(angle)
    peak = float(np.max(np.abs(mix)))
    if peak <= 1e-8:
        raise ValueError("Synthesis produced silence")
    attenuation = min(1, 10 ** (-3 / 20) / peak)
    mix *= attenuation
    pcm = (np.clip(mix, -1, 1) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(2)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(pcm.tobytes())
    return {"engine": "builtin", "synthesis": "deterministic oscillator/noise proxies", "linear_attenuation": attenuation,
            "limitations": "Volume, expression and pan are sampled at note onset. Sustain, continuous automation, pitch bends, bank selects and pressure are not performed. Use a compatible SoundFont renderer for those events."}


def wav_stats(path):
    import numpy as np
    with wave.open(str(path), "rb") as wav:
        if wav.getsampwidth() != 2 or wav.getnchannels() != 2 or wav.getframerate() != 44100:
            raise ValueError("Expected stereo 44.1 kHz 16-bit PCM WAV")
        frames, rate = wav.getnframes(), wav.getframerate()
        samples = np.frombuffer(wav.readframes(frames), dtype="<i2").astype(np.float64) / 32768
    peak = float(np.max(np.abs(samples))) if samples.size else 0
    if not 0 < peak < 1:
        raise ValueError("Rendered audio is silent or clipped")
    return {"duration_seconds": frames / rate, "sample_rate": rate, "channels": 2,
            "sample_peak_dbfs": 20 * math.log10(peak), "rms_dbfs": 20 * math.log10(float(np.sqrt(np.mean(samples ** 2))))}


def render(midi_path, output, engine="auto", soundfont=None, fluidsynth=None, ffmpeg=None, formats="both", score=None):
    midi_path, output = Path(midi_path).resolve(), Path(output).resolve()
    source = midi_path.read_bytes()
    report = inspect(loads(source))
    if not report["notes"]:
        raise ValueError("MIDI contains no playable notes")
    if len(report["notes"]) > 30000:
        raise ValueError("Rendering is limited to 30000 notes")
    if report["duration_seconds"] > 600:
        raise ValueError("Rendering is limited to ten minutes per file")
    if any(n["port"] != 0 for n in report["notes"]):
        raise ValueError("Multi-port MIDI needs separate synth instances; render in a port-aware DAW")
    bank = soundfont or os.environ.get("MUSE_SOUNDFONT")
    fluid = fluidsynth or os.environ.get("MUSE_FLUIDSYNTH") or shutil.which("fluidsynth")
    encoder = ffmpeg or os.environ.get("MUSE_FFMPEG") or shutil.which("ffmpeg")
    if engine == "auto":
        from .instruments import discover
        if not bank:
            bank = next((b["path"] for b in discover()["banks"] if "error" not in b), None)
        engine = "soundfont" if bank and fluid else "builtin"
    if engine == "soundfont" and (not bank or not Path(bank).is_file() or not fluid):
        raise ValueError("SoundFont rendering requires --soundfont BANK.sf2 and --fluidsynth EXECUTABLE (or MUSE_* environment variables)")
    if (formats in ("both", "mp3") or engine == "soundfont") and not encoder:
        raise ValueError("FFmpeg is required for MP3 and SoundFont normalization. Install it or use --engine builtin --formats wav.")
    if any((output / name).exists() for name in ("audio.wav", "audio.mp3", "render.json")):
        raise ValueError("Render output already exists; choose a fresh directory")
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".render-", dir=output) as temp:
        root = Path(temp)
        wav = root / "audio.wav"
        if engine == "builtin":
            details = synthesize(report, wav, score)
        else:
            raw = root / "raw.wav"
            config = root / "empty.cfg"
            config.write_text("", encoding="utf-8")
            result = run([fluid, "-ni", "-f", config, "-R", "1", "-C", "0", "-r", "44100", "-g", "0.4", "-T", "wav", "-O", "float", "-o", "synth.cpu-cores=1", "-F", raw, Path(bank).resolve(), midi_path])
            messages = result.stderr + result.stdout
            if "warning" in messages.lower() or "error" in messages.lower():
                raise ValueError(f"FluidSynth reported a rendering problem: {messages[-2000:]}")
            # Normalize peak downward only, then pad/trim to a predictable release tail.
            measured = run([encoder, "-hide_banner", "-i", raw, "-af", "astats=metadata=0:reset=0", "-f", "null", "-"])
            import re
            found = re.findall(r"Peak level dB:\s*([-+\w.]+)", measured.stderr)
            if not found or not math.isfinite(float(found[-1])):
                raise ValueError("Could not measure SoundFont render")
            durations = re.findall(r"time=(\d+):(\d+):(\d+\.\d+)", measured.stderr)
            raw_duration = max((int(h) * 3600 + int(m) * 60 + float(s) for h, m, s in durations), default=0)
            if raw_duration < report["duration_seconds"] - 0.1:
                raise ValueError("SoundFont render ended before the MIDI score")
            gain = min(0, -3 - float(found[-1]))
            run([encoder, "-v", "error", "-i", raw, "-af", f"volume={gain}dB,apad", "-t", str(report["duration_seconds"] + 2), "-ar", "44100", "-ac", "2", "-c:a", "pcm_s16le", wav])
            details = {"engine": "soundfont", "soundfont": str(Path(bank).resolve()), "soundfont_sha256": file_hash(bank),
                       "fluidsynth": str(fluid), "version": run([fluid, "--version"]).stdout[:500], "gain_db": gain,
                       "raw_peak_dbfs": float(found[-1]), "raw_duration_seconds": raw_duration,
                       "license": "Use the license distributed with this local SoundFont; no bank is bundled."}
        stats = wav_stats(wav)
        if abs(stats["duration_seconds"] - (report["duration_seconds"] + 2)) > 0.1:
            raise ValueError("Unexpected WAV duration")
        artifacts = {}
        if formats in ("both", "mp3"):
            mp3 = root / "audio.mp3"
            run([encoder, "-v", "error", "-i", wav, "-c:a", "libmp3lame", "-q:a", "2", mp3])
            decoded = root / "decoded.wav"
            run([encoder, "-v", "error", "-i", mp3, "-c:a", "pcm_s16le", decoded])
            details["decoded_mp3"] = wav_stats(decoded)
            if abs(details["decoded_mp3"]["duration_seconds"] - stats["duration_seconds"]) > 0.15:
                raise ValueError("Unexpected decoded MP3 duration")
            artifacts["audio.mp3"] = file_hash(mp3)
            details["ffmpeg_version"] = run([encoder, "-version"]).stdout.splitlines()[0]
        if formats in ("both", "wav"):
            artifacts["audio.wav"] = file_hash(wav)
        for name in artifacts:
            (root / name).replace(output / name)
    return {"source_sha256": sha256(source), "render": details, "wav_measurements": stats, "artifacts": artifacts,
            "midi_warnings": report["warnings"], "auditory_review": "Not performed; technical validation is not a listening judgment."}
