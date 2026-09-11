"""CLI for human sessions and coding-agent tool calls. stdout is always JSON."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import uuid
from . import __version__
from .agent import ask, compose_prompt, edit_prompt, executable
from .context import collect
from .instruments import discover
from .midi import loads, dumps, inspect, piano_roll, sha256
from .render import render
from .score import SCORE_SCHEMA, PATCH_SCHEMA, compile_score, apply_patch, demo_score


def write_json(path, value):
    Path(path).write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def read_json(path):
    if Path(path).stat().st_size > 16 * 1024 * 1024:
        raise ValueError("JSON file exceeds 16 MiB")
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def new_output(value=None):
    path = Path(value) if value else Path("runs") / (datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6])
    path = path.resolve()
    if path.exists():
        raise ValueError(f"Output directory already exists: {path}; choose a new directory")
    path.mkdir(parents=True)
    return path


def report_for(data):
    result = inspect(loads(data))
    result["source_sha256"] = sha256(data)
    return result


def export(data, output, explanation, score=None):
    report = report_for(data)
    (output / "composition.mid").write_bytes(data)
    write_json(output / "inspection.json", report)
    (output / "piano-roll.svg").write_text(piano_roll(report), encoding="utf-8")
    text = "# Composition notes\n\n" + explanation + "\n\n"
    if score:
        write_json(output / "score.json", score)
        text += "Theme: " + score["theme"] + "\n\nMood: " + ", ".join(score["mood"]) + "\n\n"
        text += "## Sections\n\n" + "\n".join(f'- Beat {s["beat"]}: {s["name"]} — {s["purpose"]}' for s in score["sections"]) + "\n\n"
        text += "## Instruments\n\n" + "\n".join("- " + advice for advice in score["instrument_advice"]) + "\n\n"
    text += "Technical inspection is not a listening review. MIDI programs are zero-based.\n"
    (output / "notes.md").write_text(text, encoding="utf-8")
    return report


def audio_options(parser):
    parser.add_argument("--engine", choices=["auto", "builtin", "soundfont"], default="auto")
    parser.add_argument("--soundfont", help="Local SF2/SF3 bank")
    parser.add_argument("--fluidsynth", help="FluidSynth executable")
    parser.add_argument("--ffmpeg", help="FFmpeg executable")
    parser.add_argument("--formats", choices=["both", "wav", "mp3", "none"], default="both")


def agent_options(parser):
    parser.add_argument("--provider", choices=["codex", "claude"], default="codex")
    parser.add_argument("--model", help="Optional model; otherwise use the provider CLI's configured default")
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--response", type=Path, help="Use a local score/patch JSON instead of calling a provider")
    parser.add_argument("--prepare", action="store_true", help="Write prompt and schema only, for an existing agent to answer")


def add_render(args, midi_path, output, score=None):
    if args.formats == "none":
        return None
    result = render(midi_path, output, args.engine, args.soundfont, args.fluidsynth, args.ffmpeg, args.formats, score)
    write_json(output / "render.json", result)
    return result


def response(args, output, prompt, schema, validator):
    (output / "prompt.txt").write_text(prompt, encoding="utf-8")
    write_json(output / "response.schema.json", schema)
    if args.prepare:
        if args.response:
            raise ValueError("--prepare and --response cannot be combined")
        return None
    if args.response:
        value = read_json(args.response)
        validator(value)
        write_json(output / "response.json", value)
        return value
    # A bounded repair pass addresses malformed or musically invalid numeric output.
    for attempt in range(2):
        print(f"Requesting {args.provider} composition data (attempt {attempt + 1})...", file=sys.stderr)
        value = ask(args.provider, prompt, schema, args.model, args.timeout)
        write_json(output / f"response-{attempt + 1}.json", value)
        try:
            validator(value)
            write_json(output / "response.json", value)
            return value
        except (ValueError, KeyError, TypeError) as exc:
            if attempt:
                raise
            prompt += "\nYour previous response was invalid. Fix this error: " + str(exc) + "\nPrevious response:\n" + json.dumps(value)
    raise ValueError("Provider failed to return a valid response")


def build_parser():
    parser = argparse.ArgumentParser(description="Turn ideas and context into editable music using Codex or Claude.")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create", help="Compose from an idea, local repo/files or public webpage")
    create.add_argument("idea", nargs="?", default="")
    create.add_argument("--source", action="append", default=[], help="Local repo, text file or public webpage; repeatable")
    create.add_argument("--bars", type=int, default=32)
    create.add_argument("--instrument-dir", action="append", default=[])
    create.add_argument("--output")
    agent_options(create)
    audio_options(create)
    edit = sub.add_parser("edit", help="Apply natural-language feedback to an existing MIDI")
    edit.add_argument("midi", type=Path)
    edit.add_argument("feedback")
    edit.add_argument("--output")
    agent_options(edit)
    audio_options(edit)
    patch = sub.add_parser("apply", help="Apply an authored hash-bound MIDI patch")
    patch.add_argument("midi", type=Path)
    patch.add_argument("patch", type=Path)
    patch.add_argument("--output")
    audio_options(patch)
    comp = sub.add_parser("compile", help="Validate an authored score JSON and export it")
    comp.add_argument("score", type=Path)
    comp.add_argument("--output")
    audio_options(comp)
    view = sub.add_parser("inspect", help="Read MIDI and write a report and piano-roll SVG")
    view.add_argument("midi", type=Path)
    view.add_argument("--output")
    ren = sub.add_parser("render", help="Render an existing MIDI without editing it")
    ren.add_argument("midi", type=Path)
    ren.add_argument("--output")
    audio_options(ren)
    instruments = sub.add_parser("instruments", help="Find local banks/presets and instrument sourcing advice")
    instruments.add_argument("query", nargs="?", default="")
    instruments.add_argument("--dir", action="append", default=[])
    sub.add_parser("doctor", help="Report installed provider and audio tools")
    schema = sub.add_parser("schema", help="Print the score or patch interchange schema")
    schema.add_argument("kind", choices=["score", "patch"])
    demo = sub.add_parser("demo", help="Export a fixed authored demonstration without a model call")
    demo.add_argument("--output")
    audio_options(demo)
    chat = sub.add_parser("chat", help="Interactive compose/edit session with saved revisions")
    chat.add_argument("--source", action="append", default=[])
    chat.add_argument("--bars", type=int, default=32)
    chat.add_argument("--midi", type=Path, help="Start by revising an existing MIDI")
    chat.add_argument("--output")
    agent_options(chat)
    audio_options(chat)
    return parser


def session(args):
    if args.prepare or args.response:
        raise ValueError("Use create/edit for --prepare or --response")
    root = new_output(args.output)
    current = args.midi.resolve() if args.midi else None
    history = []
    print("Describe a composition, then suggest edits. /new starts a new piece; /quit exits.", file=sys.stderr)
    while True:
        try:
            print("muse> ", end="", file=sys.stderr, flush=True)
            idea = input().strip()
        except (EOFError, KeyboardInterrupt):
            break
        if idea == "/quit":
            break
        if idea == "/new":
            current = None
            continue
        if not idea:
            continue
        command = ["edit", str(current), idea] if current else ["create", idea, "--bars", str(args.bars)]
        command += ["--provider", args.provider, "--timeout", str(args.timeout), "--output", str(root / f"revision-{len(history)+1:03d}-{uuid.uuid4().hex[:4]}"), "--engine", args.engine, "--formats", args.formats]
        if not current:
            for s in args.source:
                command += ["--source", s]
        for name in ("model", "soundfont", "fluidsynth", "ffmpeg"):
            if getattr(args, name):
                command += ["--" + name, getattr(args, name)]
        try:
            result = execute(build_parser().parse_args(command))
            current = Path(result["output"]) / "composition.mid"
            history.append({"feedback": idea, "midi": str(current)})
            write_json(root / "session.json", history)
            print(json.dumps(result), flush=True)
        except (ValueError, OSError) as exc:
            print(f"Error: {exc}", file=sys.stderr)
    return {"output": str(root), "revisions": len(history), "current_midi": str(current) if current else None}


def execute(args):
    command = args.command
    if command == "schema":
        return SCORE_SCHEMA if args.kind == "score" else PATCH_SCHEMA
    if command == "doctor":
        import os
        import importlib.util
        return {"python": sys.version.split()[0], "numpy": bool(importlib.util.find_spec("numpy")),
                "tools": {n: executable(n) for n in ("codex", "claude", "ffmpeg", "fluidsynth")},
                "overrides": {n: os.environ.get(n) for n in ("MUSE_FFMPEG", "MUSE_FLUIDSYNTH", "MUSE_SOUNDFONT")},
                "soundfonts": discover()["banks"], "note": "Tool discovery does not verify provider authentication."}
    if command == "instruments":
        return discover(args.dir, args.query)
    if command == "chat":
        return session(args)
    output = new_output(args.output)
    try:
        if command == "inspect":
            report = report_for(args.midi.read_bytes())
            write_json(output / "inspection.json", report)
            (output / "piano-roll.svg").write_text(piano_roll(report), encoding="utf-8")
            return {"output": str(output), **{k: v for k, v in report.items() if k != "notes"}}
        if command == "render":
            if args.formats == "none":
                raise ValueError("render requires an audio format")
            result = add_render(args, args.midi, output)
            return {"output": str(output), **result}
        if command in ("create", "compile", "demo"):
            if command == "create":
                if not 1 <= args.bars <= 128:
                    raise ValueError("--bars must be between 1 and 128")
                context = collect(args.idea, args.source)
                inventory = discover(args.instrument_dir)
                write_json(output / "context.json", context)
                write_json(output / "instruments.json", inventory)
                score = response(args, output, compose_prompt(context, args.bars, inventory), SCORE_SCHEMA, compile_score)
                if score is None:
                    return {"output": str(output), "status": "prepared", "next": "Answer prompt.txt using response.schema.json; then run compile on your score JSON."}
            else:
                score = demo_score() if command == "demo" else read_json(args.score)
            data = dumps(compile_score(score))
            export(data, output, score["rationale"], score)
        else:
            source = args.midi.read_bytes()
            report = report_for(source)
            # Persist an exact source snapshot for audit and recovery.
            (output / "source.mid").write_bytes(source)
            write_json(output / "source-inspection.json", report)
            if command == "edit":
                if len(json.dumps(report)) > 600000:
                    raise ValueError("MIDI is too large for the model editing context; use inspect and an authored apply patch")
                patch = response(args, output, edit_prompt(args.feedback, report), PATCH_SCHEMA, lambda p: apply_patch(source, p))
                if patch is None:
                    return {"output": str(output), "status": "prepared", "next": "Answer prompt.txt using response.schema.json; then run apply with source.mid and your patch JSON."}
            else:
                patch = read_json(args.patch)
            data = apply_patch(source, patch)
            write_json(output / "patch.json", patch)
            after = export(data, output, patch["explanation"])
            write_json(output / "changes.json", {"before_sha256": sha256(source), "after_sha256": sha256(data),
                       "edited": len(patch["edits"]), "deleted": len(patch["delete_notes"]), "added": len(patch["add_notes"]),
                       "before_duration_seconds": report["duration_seconds"], "after_duration_seconds": after["duration_seconds"],
                       "explanation": patch["explanation"]})
            score = None
        write_json(output / "run.json", {"version": __version__, "command": command,
                   "provider": getattr(args, "provider", None) if not getattr(args, "response", None) else "authored-response",
                   "model": getattr(args, "model", None), "midi_sha256": sha256(data)})
        add_render(args, output / "composition.mid", output, score)
        return {"output": str(output), "midi": str(output / "composition.mid"), "formats": args.formats,
                "notes": str(output / "notes.md"), "piano_roll": str(output / "piano-roll.svg")}
    except Exception as exc:
        write_json(output / "error.json", {"error": str(exc), "note": "Completed intermediate artifacts are retained for recovery."})
        raise


def main(argv=None):
    try:
        result = execute(build_parser().parse_args(argv))
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    except (ValueError, OSError, KeyError, TypeError, ImportError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print('{"error": "Interrupted"}', file=sys.stderr)
        return 130
