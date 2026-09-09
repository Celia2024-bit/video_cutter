#!/usr/bin/env python3
"""Local web UI for video_cutter.py - mark the parts to drop by watching the video.

    python web.py                 # opens http://127.0.0.1:8770 in your browser
    python web.py --dir D:/clips  # start in a specific folder

Nothing is uploaded: the page streams the file straight off your disk, and the cut is
done by the ffmpeg already installed on this machine. The server binds 127.0.0.1, so
it is not reachable from the network - but it does read and write local files, so do
not expose it.
"""

import argparse
import io
import mimetypes
import subprocess
import sys
import threading
import uuid
import webbrowser
from contextlib import redirect_stdout
from pathlib import Path

from flask import Flask, jsonify, request, send_file, send_from_directory

# Importing our own CLI keeps one implementation of the cutting logic.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import video_cutter as vc

HERE = Path(__file__).resolve().parent

VIDEO_SUFFIXES = {
    ".mp4", ".m4v", ".mov", ".mkv", ".webm", ".avi", ".wmv", ".flv",
    ".ts", ".m2ts", ".mpg", ".mpeg", ".3gp", ".ogv",
}

# Browsers need a usable type on the response, and mimetypes does not know them all.
MIME_TYPES = {
    ".mp4": "video/mp4", ".m4v": "video/mp4", ".mov": "video/quicktime",
    ".mkv": "video/x-matroska", ".webm": "video/webm", ".ts": "video/mp2t",
    ".ogv": "video/ogg",
}

app = Flask(__name__, static_folder=None)

# Running jobs, keyed by id. A cut can take minutes, so it runs in a thread and the
# page polls for the log.
JOBS = {}
JOBS_LOCK = threading.Lock()


class LogWriter(io.TextIOBase):
    """Collect what video_cutter prints, so the page can show it while it runs."""

    def __init__(self, job):
        self.job = job

    def write(self, text):
        if text:
            with JOBS_LOCK:
                self.job["log"] += text
        return len(text)


def video_duration(path):
    """Duration in seconds, or None when ffprobe cannot tell."""
    try:
        return vc.probe_duration(path)
    except RuntimeError:
        return None


@app.route("/")
def index():
    return send_from_directory(HERE / "webui", "index.html")


@app.route("/api/list")
def api_list():
    """List the sub-folders and videos of one directory."""
    raw = (request.args.get("dir") or ".").strip()
    try:
        base = Path(raw).expanduser().resolve()
    except OSError as ex:
        return jsonify({"error": str(ex)}), 400
    if not base.is_dir():
        return jsonify({"error": f"not a folder: {base}"}), 404

    dirs, files = [], []
    try:
        entries = sorted(base.iterdir(), key=lambda p: p.name.lower())
    except PermissionError as ex:
        return jsonify({"error": str(ex)}), 403

    for entry in entries:
        try:
            if entry.is_dir():
                if not entry.name.startswith("."):
                    dirs.append({"name": entry.name, "path": str(entry)})
            elif entry.suffix.lower() in VIDEO_SUFFIXES:
                files.append({
                    "name": entry.name,
                    "path": str(entry),
                    "size": entry.stat().st_size,
                    "duration": video_duration(entry),
                    "playable": entry.suffix.lower() in {".mp4", ".m4v", ".webm", ".mov"},
                })
        except OSError:
            continue

    parent = str(base.parent) if base.parent != base else None
    return jsonify({"dir": str(base), "parent": parent, "dirs": dirs, "files": files})


@app.route("/api/media")
def api_media():
    """Stream one video off the disk. conditional=True gives the player its seeking."""
    raw = (request.args.get("path") or "").strip()
    if not raw:
        return jsonify({"error": "no path"}), 400
    path = Path(raw).expanduser().resolve()
    if not path.is_file():
        return jsonify({"error": f"no such file: {path}"}), 404
    if path.suffix.lower() not in VIDEO_SUFFIXES:
        return jsonify({"error": "not a video file"}), 415

    mime = MIME_TYPES.get(path.suffix.lower()) or mimetypes.guess_type(path.name)[0]
    return send_file(path, mimetype=mime or "application/octet-stream",
                     conditional=True)


@app.route("/api/cut", methods=["POST"])
def api_cut():
    """Start a cut in the background and hand back a job id to poll."""
    payload = request.json or {}
    source = (payload.get("input") or "").strip()
    if not source:
        return jsonify({"error": "no input file"}), 400

    ranges = payload.get("ranges") or []
    mode_of_ranges = payload.get("range_mode", "cut")
    if not ranges:
        return jsonify({"error": "mark at least one range first"}), 400
    if mode_of_ranges not in ("cut", "keep"):
        return jsonify({"error": "range_mode must be 'cut' or 'keep'"}), 400

    kwargs = {
        "source": source,
        "cuts": ranges if mode_of_ranges == "cut" else [],
        "keeps": ranges if mode_of_ranges == "keep" else [],
        "output": (payload.get("output") or "").strip() or None,
        "mode": "copy" if payload.get("mode") == "copy" else "reencode",
        "separate": bool(payload.get("separate")),
        "crf": int(payload.get("crf") or 20),
        "preset": payload.get("preset") or "veryfast",
        "dry_run": bool(payload.get("dry_run")),
        "verbose": False,
    }

    job_id = uuid.uuid4().hex[:12]
    job = {"id": job_id, "status": "running", "log": "", "outputs": [], "error": None}
    with JOBS_LOCK:
        JOBS[job_id] = job

    thread = threading.Thread(target=run_job, args=(job, kwargs), daemon=True)
    thread.start()
    return jsonify({"job": job_id})


def run_job(job, kwargs):
    writer = LogWriter(job)
    try:
        with redirect_stdout(writer):
            outputs = vc.cut_video(**kwargs)
        with JOBS_LOCK:
            job["outputs"] = [str(path) for path in outputs]
            job["status"] = "done"
    except Exception as ex:
        with JOBS_LOCK:
            job["error"] = str(ex)
            job["status"] = "error"


@app.route("/api/transcribe", methods=["POST"])
def api_transcribe():
    """Start a speech-to-text job in the background and hand back a job id to poll."""
    payload = request.json or {}
    source = (payload.get("input") or "").strip()
    if not source:
        return jsonify({"error": "no input file"}), 400

    engine = payload.get("engine") or "local"
    if engine not in ("local", "groq"):
        return jsonify({"error": "engine must be 'local' or 'groq'"}), 400
    model_size = (payload.get("model") or "small").strip()
    language = (payload.get("language") or "").strip() or None
    groq_api_key = (payload.get("groq_api_key") or "").strip() or None

    job_id = uuid.uuid4().hex[:12]
    job = {"id": job_id, "status": "running", "log": "", "segments": [], "error": None}
    with JOBS_LOCK:
        JOBS[job_id] = job

    thread = threading.Thread(
        target=run_transcribe_job,
        args=(job, source, engine, model_size, language, groq_api_key),
        daemon=True,
    )
    thread.start()
    return jsonify({"job": job_id})


def run_transcribe_job(job, source, engine, model_size, language, groq_api_key):
    writer = LogWriter(job)

    def on_segment(start, end, text):
        with JOBS_LOCK:
            job["segments"].append({"start": start, "end": end, "text": text})

    try:
        with redirect_stdout(writer):
            if engine == "groq":
                vc.transcribe_audio_groq(source, language=language, api_key=groq_api_key,
                                         progress=on_segment)
            else:
                vc.transcribe_audio(source, model_size=model_size, language=language,
                                    progress=on_segment)
        with JOBS_LOCK:
            job["status"] = "done"
    except Exception as ex:
        with JOBS_LOCK:
            job["error"] = str(ex)
            job["status"] = "error"


@app.route("/api/job/<job_id>")
def api_job(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return jsonify({"error": "unknown job"}), 404
        return jsonify(dict(job))


@app.route("/api/reveal", methods=["POST"])
def api_reveal():
    """Open the folder of a result in the system file manager."""
    raw = ((request.json or {}).get("path") or "").strip()
    path = Path(raw).expanduser().resolve()
    if not path.exists():
        return jsonify({"error": f"no such path: {path}"}), 404
    try:
        if sys.platform == "win32":
            subprocess.Popen(["explorer", "/select,", str(path)])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path.parent)])
    except OSError as ex:
        return jsonify({"error": str(ex)}), 500
    return jsonify({"status": "ok"})


def main():
    parser = argparse.ArgumentParser(description="Local web UI for video_cutter.")
    parser.add_argument("--dir", default=".", help="folder to start in (default: .)")
    parser.add_argument("--port", type=int, default=8770)
    parser.add_argument("--host", default="127.0.0.1",
                        help="keep the default unless you know why you are changing it")
    parser.add_argument("--no-browser", action="store_true",
                        help="do not open a browser tab on start")
    args = parser.parse_args()

    try:
        vc.require_tools()
    except RuntimeError as ex:
        print(f"error: {ex}", file=sys.stderr)
        return 1

    # The starting folder travels in the query string; the page asks /api/list for it.
    start_dir = Path(args.dir).expanduser().resolve()
    url = f"http://{args.host}:{args.port}/?dir={start_dir}"
    print(f"video_cutter web UI -> http://{args.host}:{args.port}")
    print(f"  starting folder: {start_dir}")
    print("  press Ctrl+C to stop")
    if not args.no_browser:
        # The browser is on this machine, so opening it here actually works.
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()

    app.run(host=args.host, port=args.port, threaded=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
