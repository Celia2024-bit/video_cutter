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

# So the page can tell a stale python process (old routes, new HTML) from a current one.
SERVER_FEATURES = ["cut", "join", "transcribe", "srt", "import-subs", "export-subs"]

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


@app.route("/api/health")
def api_health():
    """Tiny handshake so the page can tell this process has the latest routes."""
    return jsonify({"ok": True, "app": "video_cutter", "features": SERVER_FEATURES})


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
                sidecar = vc.find_sidecar_srt(entry)
                files.append({
                    "name": entry.name,
                    "path": str(entry),
                    "size": entry.stat().st_size,
                    "duration": video_duration(entry),
                    "playable": entry.suffix.lower() in {".mp4", ".m4v", ".webm", ".mov"},
                    "srt": str(sidecar) if sidecar else None,
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
    job = {"id": job_id, "status": "running", "log": "", "segments": [],
           "srt": None, "error": None}
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
                segments = list(job["segments"])
            srt_path = None
            if segments:
                srt_path = Path(source).with_suffix(".srt")
                vc.write_srt(segments, srt_path)
                print(f"  wrote {srt_path}")
        with JOBS_LOCK:
            job["srt"] = str(srt_path) if srt_path else None
            job["status"] = "done"
    except Exception as ex:
        with JOBS_LOCK:
            job["error"] = str(ex)
            job["status"] = "error"


@app.route("/api/join", methods=["POST"])
def api_join():
    """Start a video join in the background and hand back a job id to poll."""
    payload = request.json or {}
    inputs = payload.get("inputs") or []
    if len(inputs) < 2:
        return jsonify({"error": "need at least two videos to join"}), 400

    kwargs = {
        "sources": inputs,
        "output": (payload.get("output") or "").strip() or "joined.mp4",
        "mode": "copy" if payload.get("mode") == "copy" else "reencode",
        "crf": int(payload.get("crf") or 20),
        "preset": payload.get("preset") or "veryfast",
        "verbose": False,
    }

    job_id = uuid.uuid4().hex[:12]
    job = {"id": job_id, "status": "running", "log": "", "output": None, "error": None}
    with JOBS_LOCK:
        JOBS[job_id] = job

    thread = threading.Thread(target=run_join_job, args=(job, kwargs), daemon=True)
    thread.start()
    return jsonify({"job": job_id})


def run_join_job(job, kwargs):
    writer = LogWriter(job)
    try:
        with redirect_stdout(writer):
            output = vc.join_videos(**kwargs)
        with JOBS_LOCK:
            job["output"] = str(output)
            job["status"] = "done"
    except Exception as ex:
        with JOBS_LOCK:
            job["error"] = str(ex)
            job["status"] = "error"


@app.route("/api/srt", methods=["GET", "POST"])
def api_srt():
    """Load or save the sidecar .srt next to a video (never burned into the picture)."""
    if request.method == "GET":
        raw = (request.args.get("video") or "").strip()
        if not raw:
            return jsonify({"error": "no video path"}), 400
        srt = vc.find_sidecar_srt(Path(raw).expanduser())
        if not srt:
            return jsonify({"segments": [], "srt": None})
        try:
            segments = vc.read_srt(srt)
        except (ValueError, OSError) as ex:
            return jsonify({"error": str(ex)}), 400
        return jsonify({"segments": segments, "srt": str(srt.resolve())})

    payload = request.json or {}
    raw = (payload.get("video") or payload.get("srt") or "").strip()
    if not raw:
        return jsonify({"error": "no video path"}), 400
    segments = payload.get("segments")
    if not isinstance(segments, list):
        return jsonify({"error": "segments must be a list"}), 400
    target = Path(raw).expanduser()
    if target.suffix.lower() != ".srt":
        target = target.with_suffix(".srt")
    try:
        vc.write_srt(segments, target)
    except (OSError, ValueError) as ex:
        return jsonify({"error": str(ex)}), 400
    return jsonify({"srt": str(target.resolve())})


@app.route("/api/import-subs", methods=["POST"])
def api_import_subs():
    """Pull captions already on a video (embedded text track, else sidecar .srt)."""
    payload = request.json or {}
    source = (payload.get("video") or payload.get("input") or "").strip()
    if not source:
        return jsonify({"error": "no video path"}), 400
    path = Path(source).expanduser()
    if not path.is_file():
        return jsonify({"error": f"no such file: {path}"}), 404
    stream = payload.get("stream")
    try:
        stream = int(stream) if stream is not None and stream != "" else None
    except (TypeError, ValueError):
        return jsonify({"error": "stream must be a number"}), 400
    try:
        segments, srt = vc.import_subtitles(path, stream=stream)
    except RuntimeError as ex:
        return jsonify({"error": str(ex)}), 400
    return jsonify({"segments": segments, "srt": str(Path(srt).resolve())})


@app.route("/api/export-subs", methods=["POST"])
def api_export_subs():
    """Mux the edited captions into a new video as a toggleable subtitle track."""
    payload = request.json or {}
    source = (payload.get("video") or payload.get("input") or "").strip()
    if not source:
        return jsonify({"error": "no video path"}), 400

    segments = payload.get("segments") or []
    if not isinstance(segments, list) or not any(
            (seg.get("text") or "").strip() for seg in segments if isinstance(seg, dict)):
        return jsonify({"error": "no captions to export - transcribe or edit first"}), 400

    src = Path(source)
    output = (payload.get("output") or "").strip()
    if not output:
        output = str(src.with_name(f"{src.stem}_subs{src.suffix or '.mp4'}"))
    style = payload.get("style") if isinstance(payload.get("style"), dict) else {}

    job_id = uuid.uuid4().hex[:12]
    job = {"id": job_id, "status": "running", "log": "", "output": None, "error": None}
    with JOBS_LOCK:
        JOBS[job_id] = job

    thread = threading.Thread(
        target=run_export_subs_job,
        args=(job, source, segments, output, style),
        daemon=True,
    )
    thread.start()
    return jsonify({"job": job_id})


def run_export_subs_job(job, source, segments, output, style=None):
    writer = LogWriter(job)
    try:
        with redirect_stdout(writer):
            source_path = Path(source)
            target = Path(output)
            srt = source_path.with_suffix(".srt")
            vc.write_srt(segments, srt)
            print(f"  wrote {srt}")
            # Browsers and the Windows player ignore MP4 subtitle tracks, so the
            # words have to be drawn onto the picture or they look "missing".
            result = vc.burn_captions(source_path, segments, target, style=style or {})
            sidecar = Path(result).with_suffix(".srt")
            if sidecar.resolve() != srt.resolve():
                vc.write_srt(segments, sidecar)
                print(f"  wrote {sidecar}")
        with JOBS_LOCK:
            job["output"] = str(result)
            job["status"] = "done"
    except Exception as ex:
        with JOBS_LOCK:
            job["error"] = str(ex)
            job["status"] = "error"


@app.errorhandler(404)
def not_found(_err):
    if request.path.startswith("/api/"):
        return jsonify({
            "error": (
                f"unknown endpoint {request.method} {request.path}. "
                "The page is newer than this python process. In Task Manager end every "
                "python.exe, then in the video_cutter folder run: python web.py"
            ),
        }), 404
    return "not found", 404


@app.errorhandler(405)
def method_not_allowed(_err):
    if request.path.startswith("/api/"):
        return jsonify({
            "error": f"{request.method} not allowed on {request.path}",
        }), 405
    return "method not allowed", 405


@app.errorhandler(500)
def server_error(err):
    if request.path.startswith("/api/"):
        return jsonify({"error": str(err) or "internal server error"}), 500
    return "internal server error", 500


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
    print(f"  features: {', '.join(SERVER_FEATURES)}")
    print("  press Ctrl+C to stop")
    print("  if export fails with HTML/JSON errors, this window is not the one serving")
    print("  the browser — close other python.exe processes and start this again")
    if not args.no_browser:
        # The browser is on this machine, so opening it here actually works.
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()

    app.run(host=args.host, port=args.port, threaded=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
