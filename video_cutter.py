#!/usr/bin/env python3
"""Cut unwanted parts out of a video and join what is left back together.

Say what you want in one of two ways:

    --cut  0:10-0:25      drop this range, keep everything else   (repeatable)
    --keep 1:00-2:30      keep only this range, drop the rest     (repeatable)

Every kept range is extracted with ffmpeg and the pieces are concatenated back into
a single file. The input is never modified.

Batch several videos with --config edits.json (see README.md for the schema).
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# faster-whisper downloads its model from Hugging Face through "Xet", a newer,
# faster transfer backend. On some networks (proxies, firewalls, flaky links) Xet's
# CAS servers are unreachable and the download fails with a CAS/reqwest error after
# a few retries. Falling back to the plain HTTP downloader avoids that; it only has
# to be set before huggingface_hub is imported, which happens inside transcribe_audio().
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")

# Containers that can hold the H.264 + AAC streams produced by --mode reencode.
# Anything else (.webm, .avi, ...) is written as .mp4 instead.
REENCODE_CONTAINERS = {".mp4", ".mkv", ".mov", ".m4v"}

# Ranges shorter than this are almost always a typo, and ffmpeg may produce an
# empty file for them.
MIN_SEGMENT_SECONDS = 0.05


# --------------------------------------------------------------------------- time

def parse_time(text, duration=None):
    """Parse 90, 1:30, 0:01:30, 1:30.5 or 'end' into seconds."""
    raw = str(text).strip().lower()
    if raw in ("", "start"):
        return 0.0
    if raw in ("end", "eof"):
        if duration is None:
            raise ValueError("'end' needs the video duration, which could not be read")
        return duration
    if raw.endswith("s"):
        raw = raw[:-1]

    parts = raw.split(":")
    if len(parts) > 3:
        raise ValueError(f"'{text}' has too many ':' parts - use HH:MM:SS")

    seconds = 0.0
    for part in parts:
        if not part:
            raise ValueError(f"'{text}' is not a time - try 1:30 or 90 or 0:01:30")
        try:
            value = float(part)
        except ValueError:
            raise ValueError(f"'{text}' is not a time - try 1:30 or 90 or 0:01:30")
        if value < 0:
            raise ValueError(f"'{text}' is negative")
        seconds = seconds * 60 + value
    return seconds


def parse_range(text, duration):
    """Parse 'START-END' (or 'START..END') into a (start, end) pair of seconds."""
    raw = str(text).strip()
    separator = ".." if ".." in raw else "-"
    if separator not in raw:
        raise ValueError(f"'{raw}' is not a range - use START-END, e.g. 0:10-0:25")

    left, right = raw.split(separator, 1)
    start = parse_time(left, duration)
    end = parse_time(right, duration)

    if duration is not None:
        start = min(start, duration)
        end = min(end, duration)
    if end <= start:
        raise ValueError(f"'{raw}' ends at or before it starts")
    return start, end


def format_time(seconds):
    """Format seconds as H:MM:SS.mmm, the way the ranges are written."""
    total_ms = int(round(seconds * 1000))
    hours, rest = divmod(total_ms, 3_600_000)
    minutes, rest = divmod(rest, 60_000)
    secs, millis = divmod(rest, 1000)
    return f"{hours}:{minutes:02d}:{secs:02d}.{millis:03d}"


def merge_ranges(ranges):
    """Sort ranges and fuse the ones that overlap or touch."""
    merged = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(start, end) for start, end in merged]


def invert_ranges(cuts, duration):
    """Turn 'the parts to drop' into 'the parts to keep'."""
    keeps = []
    position = 0.0
    for start, end in merge_ranges(cuts):
        if start - position >= MIN_SEGMENT_SECONDS:
            keeps.append((position, start))
        position = max(position, end)
    if duration - position >= MIN_SEGMENT_SECONDS:
        keeps.append((position, duration))
    return keeps


# ------------------------------------------------------------------------- ffmpeg

def require_tools():
    """Fail early and clearly when ffmpeg is not on PATH."""
    missing = [name for name in ("ffmpeg", "ffprobe") if shutil.which(name) is None]
    if missing:
        raise RuntimeError(
            f"{' and '.join(missing)} not found on PATH. Install ffmpeg "
            "(https://ffmpeg.org/download.html) and reopen the terminal."
        )


def run_ffmpeg(args, verbose=False):
    """Run ffmpeg, showing its output only when it matters."""
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-y"]
    cmd += ["-loglevel", "info" if verbose else "error"]
    cmd += args
    res = subprocess.run(cmd, capture_output=not verbose, text=True,
                         encoding="utf-8", errors="replace")
    if res.returncode != 0:
        detail = (res.stderr or res.stdout or "").strip() if not verbose else ""
        raise RuntimeError(f"ffmpeg failed (exit {res.returncode})\n{detail}")


def require_asr():
    """Fail early and clearly when the speech-recognition package is missing."""
    try:
        import faster_whisper  # noqa: F401
    except ImportError as ex:
        raise RuntimeError(
            "faster-whisper is not installed. Run: pip install faster-whisper"
        ) from ex


def require_groq(api_key):
    """Fail early and clearly when the groq package or API key is missing."""
    try:
        import groq  # noqa: F401
    except ImportError as ex:
        raise RuntimeError(
            "the groq package is not installed. Run: pip install groq"
        ) from ex
    if not api_key:
        raise RuntimeError(
            "no Groq API key. Set the GROQ_API_KEY environment variable, or pass "
            "one in, from a free key at https://console.groq.com/keys"
        )


def probe_duration(path):
    """Read the duration of a video in seconds."""
    res = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nokey=1:noprint_wrappers=1", str(path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if res.returncode != 0:
        raise RuntimeError(f"ffprobe could not read {path}:\n{res.stderr.strip()}")
    try:
        return float(res.stdout.strip())
    except ValueError:
        raise RuntimeError(f"{path} reports no duration - is it a video file?")


def print_info(path):
    """Print duration and streams, so cut points can be chosen from real numbers."""
    res = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "stream=index,codec_type,codec_name,width,height,r_frame_rate",
         "-of", "json", str(path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    duration = probe_duration(path)
    print(f"{path}")
    print(f"  duration : {format_time(duration)}  ({duration:.3f}s)")
    try:
        streams = json.loads(res.stdout or "{}").get("streams", [])
    except json.JSONDecodeError:
        streams = []
    for stream in streams:
        bits = [stream.get("codec_type", "?"), stream.get("codec_name", "?")]
        if stream.get("width"):
            bits.append(f"{stream['width']}x{stream['height']}")
        rate = stream.get("r_frame_rate")
        if rate and rate not in ("0/0",):
            bits.append(f"{rate} fps")
        print(f"  stream {stream.get('index', '?')} : " + "  ".join(bits))


# --------------------------------------------------------------- speech to text

def extract_audio(source, target, verbose=False):
    """Pull out mono 16kHz audio, the format the recognizer expects."""
    run_ffmpeg(["-i", str(source), "-vn", "-ac", "1", "-ar", "16000", "-f", "wav",
                str(target)], verbose)


def transcribe_audio(source, model_size="small", language=None, verbose=False,
                      progress=None):
    """Transcribe the speech in a video/audio file.

    Runs fully offline once the chosen model has been downloaded the first time
    (faster-whisper fetches it from Hugging Face on first use, then caches it).
    Returns a list of {"start", "end", "text"} segments, in order.

    `progress`, if given, is called as progress(start, end, text) for every
    segment as soon as it is recognized, so a caller can show live results
    instead of waiting for the whole file.
    """
    require_asr()
    from faster_whisper import WhisperModel

    source = Path(source)
    if not source.is_file():
        raise RuntimeError(f"no such file: {source}")

    workdir = Path(tempfile.mkdtemp(prefix="video_cutter_asr_"))
    try:
        wav = workdir / "audio.wav"
        print("  extracting audio ...")
        extract_audio(source, wav, verbose)

        print(f"  loading model '{model_size}' (first run downloads it) ...")
        model = WhisperModel(model_size, device="cpu", compute_type="int8")

        print("  transcribing ...")
        segment_iter, info = model.transcribe(str(wav), language=language,
                                              vad_filter=True)
        detected = getattr(info, "language", None)
        if detected and not language:
            print(f"  detected language: {detected}")

        segments = []
        for seg in segment_iter:
            text = seg.text.strip()
            if not text:
                continue
            segments.append({"start": seg.start, "end": seg.end, "text": text})
            if progress:
                progress(seg.start, seg.end, text)
            else:
                print(f"  [{format_time(seg.start)} -> {format_time(seg.end)}] {text}")
        return segments
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# Groq's API caps each request at 25MB, so long audio is sent in chunks; 15 minutes
# of mono 64kbps mp3 is a little over 7MB, comfortably inside that limit.
GROQ_CHUNK_SECONDS = 15 * 60


def extract_audio_chunk(source, start, duration, target, verbose=False):
    """Extract one small, compressed slice of audio for a single Groq request."""
    args = ["-ss", f"{start:.3f}", "-t", f"{duration:.3f}", "-i", str(source),
            "-vn", "-ac", "1", "-ar", "16000", "-c:a", "libmp3lame", "-b:a", "64k",
            str(target)]
    run_ffmpeg(args, verbose)


def transcribe_audio_groq(source, language=None, api_key=None, verbose=False,
                          progress=None, chunk_seconds=GROQ_CHUNK_SECONDS):
    """Transcribe speech using Groq's hosted Whisper API instead of a local model.

    No model download, so this is the easy way out on networks that block
    Hugging Face but allow api.groq.com. Needs `pip install groq` and an API key
    from https://console.groq.com/keys (env var GROQ_API_KEY, or pass api_key=).
    Audio is split into a few-minute chunks to stay under Groq's per-request size
    limit; the chunk timestamps are offset back onto the full timeline.
    Returns a list of {"start", "end", "text"} segments, in order.
    """
    api_key = api_key or os.environ.get("GROQ_API_KEY")
    require_groq(api_key)
    from groq import Groq

    source = Path(source)
    if not source.is_file():
        raise RuntimeError(f"no such file: {source}")

    duration = probe_duration(source)
    client = Groq(api_key=api_key)

    workdir = Path(tempfile.mkdtemp(prefix="video_cutter_groq_"))
    segments = []
    try:
        offset = 0.0
        chunk_index = 0
        while offset < duration:
            chunk_index += 1
            length = min(chunk_seconds, duration - offset)
            chunk_path = workdir / f"chunk{chunk_index:03d}.mp3"
            print(f"  chunk {chunk_index}: extracting {format_time(offset)}"
                  f" + {length:.0f}s ...")
            extract_audio_chunk(source, offset, length, chunk_path, verbose)

            print(f"  chunk {chunk_index}: sending to Groq ...")
            with open(chunk_path, "rb") as fh:
                result = client.audio.transcriptions.create(
                    file=(chunk_path.name, fh.read()),
                    model="whisper-large-v3",
                    language=language,
                    response_format="verbose_json",
                )
            chunk_path.unlink(missing_ok=True)

            for seg in getattr(result, "segments", None) or []:
                get = seg.get if isinstance(seg, dict) else (
                    lambda k, s=seg: getattr(s, k, None))
                text = (get("text") or "").strip()
                if not text:
                    continue
                start = offset + get("start")
                end = offset + get("end")
                segments.append({"start": start, "end": end, "text": text})
                if progress:
                    progress(start, end, text)
                else:
                    print(f"  [{format_time(start)} -> {format_time(end)}] {text}")

            offset += length
        return segments
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def write_srt(segments, target):
    """Write segments out as an .srt subtitle file."""
    def srt_time(t):
        ms = int(round(t * 1000))
        h, ms = divmod(ms, 3_600_000)
        m, ms = divmod(ms, 60_000)
        s, ms = divmod(ms, 1000)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

    lines = []
    for i, seg in enumerate(segments, 1):
        lines.append(str(i))
        lines.append(f"{srt_time(seg['start'])} --> {srt_time(seg['end'])}")
        lines.append(seg["text"])
        lines.append("")
    Path(target).write_text("\n".join(lines), encoding="utf-8")


def write_txt(segments, target):
    """Write segments out as a plain timestamped transcript."""
    lines = [f"[{format_time(seg['start'])}] {seg['text']}" for seg in segments]
    Path(target).write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------- the work

def extract_segment(source, start, end, target, mode, crf, preset, verbose):
    """Write one kept range to its own file."""
    # -ss before -i seeks fast; with a re-encode ffmpeg still starts exactly at -ss.
    # -t (a duration) rather than -to, which is ambiguous once -ss is in play.
    args = ["-ss", f"{start:.3f}", "-t", f"{end - start:.3f}", "-i", str(source),
            "-map", "0:v:0", "-map", "0:a?", "-sn", "-dn"]
    if mode == "copy":
        args += ["-c", "copy", "-avoid_negative_ts", "make_zero"]
    else:
        args += ["-c:v", "libx264", "-preset", preset, "-crf", str(crf),
                 "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k"]
    args.append(str(target))
    run_ffmpeg(args, verbose)


def concat_segments(segments, target, verbose):
    """Join the extracted pieces without touching the streams again."""
    list_file = target.parent / f".{target.stem}_concat.txt"
    lines = []
    for segment in segments:
        # The concat demuxer quotes with ', so an ' inside a name must be escaped.
        escaped = str(segment.resolve()).replace("'", "'\\''")
        lines.append(f"file '{escaped}'")
    list_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        run_ffmpeg(["-f", "concat", "-safe", "0", "-i", str(list_file),
                    "-c", "copy", str(target)], verbose)
    finally:
        list_file.unlink(missing_ok=True)


def resolve_output(source, output, mode, separate):
    """Pick the output path, and the container the segments have to use."""
    suffix = source.suffix.lower() or ".mp4"
    if mode == "reencode" and suffix not in REENCODE_CONTAINERS:
        print(f"  note: {suffix} cannot hold H.264/AAC, writing .mp4 instead")
        suffix = ".mp4"

    if separate:
        return None, suffix
    if output:
        target = Path(output)
        if target.is_dir():
            target = target / f"{source.stem}_cut{suffix}"
    else:
        target = source.with_name(f"{source.stem}_cut{suffix}")
    if target.resolve() == source.resolve():
        raise RuntimeError("the output would overwrite the input - pass -o")
    return target, suffix


def cut_video(source, cuts=(), keeps=(), output=None, mode="reencode", separate=False,
              crf=20, preset="veryfast", dry_run=False, verbose=False):
    """Remove the cut ranges from one video. Returns the list of files written."""
    source = Path(source)
    if not source.is_file():
        raise RuntimeError(f"no such file: {source}")
    if cuts and keeps:
        raise RuntimeError("use --cut or --keep, not both")
    if not cuts and not keeps:
        raise RuntimeError("nothing to do - pass at least one --cut or --keep range")

    duration = probe_duration(source)
    print(f"\n{source.name}  ({format_time(duration)})")

    if keeps:
        kept = merge_ranges([parse_range(text, duration) for text in keeps])
    else:
        dropped = merge_ranges([parse_range(text, duration) for text in cuts])
        for start, end in dropped:
            print(f"  cut  {format_time(start)} -> {format_time(end)}"
                  f"   (-{end - start:.3f}s)")
        kept = invert_ranges(dropped, duration)

    kept = [(start, end) for start, end in kept if end - start >= MIN_SEGMENT_SECONDS]
    if not kept:
        raise RuntimeError("every second was cut - nothing would be left")

    total = sum(end - start for start, end in kept)
    for index, (start, end) in enumerate(kept, 1):
        print(f"  keep {index}/{len(kept)}  {format_time(start)} -> {format_time(end)}"
              f"   ({end - start:.3f}s)")
    print(f"  result: {format_time(total)} of {format_time(duration)}"
          f"   ({duration - total:.3f}s removed)")
    if mode == "copy":
        print("  mode copy: cuts snap to the nearest keyframe, so they can be off by "
              "a second or two")

    target, suffix = resolve_output(source, output, mode, separate)
    if dry_run:
        print(f"  dry run: would write {target if target else 'one file per kept range'}")
        return []

    if separate:
        written = []
        for index, (start, end) in enumerate(kept, 1):
            piece = source.with_name(f"{source.stem}_part{index:02d}{suffix}")
            extract_segment(source, start, end, piece, mode, crf, preset, verbose)
            print(f"  wrote {piece.name}")
            written.append(piece)
        return written

    target.parent.mkdir(parents=True, exist_ok=True)
    workdir = Path(tempfile.mkdtemp(prefix="video_cutter_"))
    try:
        segments = []
        for index, (start, end) in enumerate(kept, 1):
            piece = workdir / f"part{index:03d}{suffix}"
            print(f"  extracting {index}/{len(kept)} ...")
            extract_segment(source, start, end, piece, mode, crf, preset, verbose)
            segments.append(piece)

        if len(segments) == 1:
            shutil.move(str(segments[0]), str(target))
        else:
            print(f"  joining {len(segments)} pieces ...")
            concat_segments(segments, target, verbose)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    print(f"  wrote {target}  ({target.stat().st_size / 1_048_576:.1f} MB)")
    return [target]


def load_jobs(config_path):
    """Read a batch edit list: either a bare list of jobs, or {"jobs": [...]}."""
    data = json.loads(Path(config_path).read_text(encoding="utf-8"))
    if isinstance(data, list):
        return {}, data
    if not isinstance(data, dict):
        raise RuntimeError(f"{config_path}: expected a list of jobs or an object")
    jobs = data.get("jobs")
    if not isinstance(jobs, list) or not jobs:
        raise RuntimeError(f"{config_path}: no \"jobs\" list")
    defaults = {key: value for key, value in data.items() if key != "jobs"}
    return defaults, jobs


def main():
    parser = argparse.ArgumentParser(
        description="Remove parts of a video and join what is left back together.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  video_cutter.py talk.mp4 --cut 0:00-0:12 --cut 5:30-6:10
  video_cutter.py talk.mp4 --keep 1:00-2:30 -o highlight.mp4
  video_cutter.py talk.mp4 --cut 0:10-0:25 --mode copy      # fast, keyframe-accurate
  video_cutter.py talk.mp4 --keep 0:00-1:00 --keep 2:00-3:00 --separate
  video_cutter.py --config edits.json
  video_cutter.py talk.mp4 --info                           # duration and streams
  video_cutter.py talk.mp4 --transcribe                     # speech to text, printed
  video_cutter.py talk.mp4 --transcribe -o talk.srt          # ... written as subtitles
""")
    parser.add_argument("input", nargs="?", help="the video to cut")
    parser.add_argument("--cut", action="append", default=[], metavar="START-END",
                        help="range to remove, e.g. 0:10-0:25 (repeatable)")
    parser.add_argument("--keep", action="append", default=[], metavar="START-END",
                        help="range to keep, dropping everything else (repeatable)")
    parser.add_argument("-o", "--output", help="output file, or a directory")
    parser.add_argument("--mode", choices=["reencode", "copy"], default="reencode",
                        help="reencode: frame-accurate (default). copy: no re-encode, "
                             "much faster, cuts land on keyframes")
    parser.add_argument("--separate", action="store_true",
                        help="write each kept range as its own file instead of joining")
    parser.add_argument("--crf", type=int, default=20,
                        help="re-encode quality, lower is better (default 20)")
    parser.add_argument("--preset", default="veryfast",
                        help="x264 preset (default veryfast)")
    parser.add_argument("--config", help="JSON edit list for several videos")
    parser.add_argument("--info", action="store_true",
                        help="just print duration and streams, cut nothing")
    parser.add_argument("--transcribe", action="store_true",
                        help="speech-to-text the input's audio, cut nothing")
    parser.add_argument("--engine", choices=["local", "groq"], default="local",
                        help="local: faster-whisper, offline after first download. "
                             "groq: Groq's cloud Whisper API, needs GROQ_API_KEY "
                             "(default local)")
    parser.add_argument("--asr-model", default="small",
                        help="faster-whisper model size: tiny/base/small/medium/large-v3 "
                             "(default small, --engine local only)")
    parser.add_argument("--groq-api-key", help="overrides the GROQ_API_KEY env var")
    parser.add_argument("--language", help="speech language code, e.g. en, zh (default: auto-detect)")
    parser.add_argument("--dry-run", action="store_true",
                        help="print what would be cut and kept, write nothing")
    parser.add_argument("--verbose", action="store_true", help="show ffmpeg output")
    args = parser.parse_args()

    try:
        require_tools()

        if args.info:
            if not args.input:
                parser.error("--info needs an input file")
            print_info(Path(args.input))
            return 0

        if args.transcribe:
            if not args.input:
                parser.error("--transcribe needs an input file")
            source = Path(args.input)
            if args.engine == "groq":
                segments = transcribe_audio_groq(source, language=args.language,
                                                 api_key=args.groq_api_key,
                                                 verbose=args.verbose)
            else:
                segments = transcribe_audio(source, model_size=args.asr_model,
                                            language=args.language, verbose=args.verbose)
            if args.output:
                target = Path(args.output)
                if target.suffix.lower() == ".srt":
                    write_srt(segments, target)
                else:
                    write_txt(segments, target)
                print(f"  wrote {target}")
            return 0

        if args.config:
            defaults, jobs = load_jobs(args.config)
            failures = 0
            for index, job in enumerate(jobs, 1):
                merged = dict(defaults, **job)
                source = merged.get("input")
                if not source:
                    print(f"\njob {index}: no \"input\" - skipped")
                    failures += 1
                    continue
                try:
                    cut_video(
                        source,
                        cuts=merged.get("cut", []),
                        keeps=merged.get("keep", []),
                        output=merged.get("output"),
                        mode=merged.get("mode", args.mode),
                        separate=bool(merged.get("separate", args.separate)),
                        crf=int(merged.get("crf", args.crf)),
                        preset=merged.get("preset", args.preset),
                        dry_run=args.dry_run,
                        verbose=args.verbose,
                    )
                except (RuntimeError, ValueError) as ex:
                    # One bad job should not abandon the rest of the batch.
                    print(f"  FAILED job {index} ({source}): {ex}")
                    failures += 1
            print(f"\n{len(jobs) - failures}/{len(jobs)} job(s) done")
            return 1 if failures else 0

        if not args.input:
            parser.error("pass a video file, or --config edits.json")
        cut_video(
            args.input, cuts=args.cut, keeps=args.keep, output=args.output,
            mode=args.mode, separate=args.separate, crf=args.crf, preset=args.preset,
            dry_run=args.dry_run, verbose=args.verbose,
        )
        return 0

    except (RuntimeError, ValueError, json.JSONDecodeError) as ex:
        print(f"error: {ex}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
