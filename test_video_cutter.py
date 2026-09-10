#!/usr/bin/env python3
"""Tests for caption styling and burning. Run: python test_video_cutter.py"""

import subprocess
import tempfile
import unittest
from pathlib import Path

import video_cutter as vc


def _tiny_video(path, seconds=2):
    """A short silent clip so burn_captions can run against a real file."""
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", f"color=c=black:s=320x240:d={seconds}",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)],
        check=True, capture_output=True,
    )


class WriteAssTests(unittest.TestCase):
    def _write(self, segments, style=None):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "captions.ass"
            vc.write_ass(segments, target, style=style)
            return target.read_text(encoding="utf-8")

    def test_dialogue_uses_ass_centisecond_times_and_text(self):
        # A millisecond SRT clock (1.234 -> 00:00:01,234) would be the bug this
        # catches: ASS wants H:MM:SS.cc, so 1.234s is 0:00:01.23.
        body = self._write([{"start": 1.234, "end": 4.0, "text": "hello there"}])
        self.assertIn("Dialogue: 0,0:00:01.23,0:00:04.00,Default,,0,0,0,,hello there", body)

    def test_css_white_becomes_ass_bgr_white(self):
        # CSS #RRGGBB flipped into ASS &HAABBGGRR: #FFFFFF -> &H00FFFFFF,
        # #FF0000 (red) -> &H000000FF. Using RGB in ASS would tint the burn.
        body = self._write(
            [{"start": 0, "end": 1, "text": "x"}],
            style={"color": "#FF0000", "outline_color": "#000000"},
        )
        self.assertIn("&H000000FF", body)

    def test_bottom_position_uses_alignment_2(self):
        body = self._write(
            [{"start": 0, "end": 1, "text": "x"}],
            style={"position": "bottom"},
        )
        # Alignment is the 19th field of the Style line (1-based after "Style:").
        style_line = [line for line in body.splitlines() if line.startswith("Style:")][0]
        fields = style_line.split(",")
        self.assertEqual(fields[18], "2")

    def test_top_position_uses_alignment_8(self):
        body = self._write(
            [{"start": 0, "end": 1, "text": "x"}],
            style={"position": "top"},
        )
        style_line = [line for line in body.splitlines() if line.startswith("Style:")][0]
        fields = style_line.split(",")
        self.assertEqual(fields[18], "8")

    def test_ass_special_characters_in_text_are_escaped(self):
        body = self._write([{"start": 0, "end": 1, "text": r"say {hi} \and more"}])
        self.assertIn(r"say \{hi\} \\and more", body)


class ReadSrtTests(unittest.TestCase):
    def test_roundtrip_keeps_times_and_edited_text(self):
        # Saving an edited cue then reopening the .srt must not snap 1.234s
        # onto a whole second, or the overlay will appear at the wrong moment.
        original = [{"start": 1.234, "end": 4.0, "text": "hello there"}]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "talk.srt"
            vc.write_srt(original, path)
            vc.write_srt([{"start": 1.234, "end": 4.0, "text": "hello friend"}], path)
            loaded = vc.read_srt(path)
        self.assertEqual(len(loaded), 1)
        self.assertAlmostEqual(loaded[0]["start"], 1.234, places=3)
        self.assertAlmostEqual(loaded[0]["end"], 4.0, places=3)
        self.assertEqual(loaded[0]["text"], "hello friend")

    def test_reads_multiline_cue_text(self):
        body = (
            "1\n"
            "00:00:00,000 --> 00:00:02,000\n"
            "line one\n"
            "line two\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "talk.srt"
            path.write_text(body, encoding="utf-8")
            loaded = vc.read_srt(path)
        self.assertEqual(loaded[0]["text"], "line one\nline two")

    def test_reads_gb18030_chinese_srt(self):
        body = (
            "1\n"
            "00:00:00,000 --> 00:00:01,000\n"
            "你好\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "talk.srt"
            path.write_bytes(body.encode("gb18030"))
            loaded = vc.read_srt(path)
        self.assertEqual(loaded[0]["text"], "你好")


class BurnCaptionsTests(unittest.TestCase):
    def test_empty_segments_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "in.mp4"
            _tiny_video(source)
            with self.assertRaises(RuntimeError):
                vc.burn_captions(source, [], Path(tmp) / "out.mp4")

    def test_writes_a_video_that_keeps_the_source_duration(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "in.mp4"
            target = Path(tmp) / "out.mp4"
            _tiny_video(source, seconds=2)
            vc.burn_captions(
                source,
                [{"start": 0.2, "end": 1.8, "text": "hello"}],
                target,
                style={"font": "Arial", "fontsize": 24, "color": "#FFFFFF",
                       "position": "bottom"},
            )
            self.assertTrue(target.is_file())
            self.assertGreater(target.stat().st_size, 0)
            duration = vc.probe_duration(target)
            self.assertAlmostEqual(duration, 2.0, delta=0.15)


class SrtApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from web import app
        cls.client = app.test_client()

    def test_missing_sidecar_returns_empty_segments(self):
        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "talk.mp4"
            res = self.client.get("/api/srt", query_string={"video": str(video)})
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertEqual(data["segments"], [])
        self.assertIsNone(data.get("srt"))

    def test_save_then_load_returns_edited_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "talk.mp4"
            res = self.client.post("/api/srt", json={
                "video": str(video),
                "segments": [{"start": 0.5, "end": 2.0, "text": "edited line"}],
            })
            self.assertEqual(res.status_code, 200)
            srt = Path(res.get_json()["srt"])
            self.assertTrue(srt.is_file())
            self.assertEqual(srt.name, "talk.srt")
            loaded = self.client.get("/api/srt", query_string={"video": str(video)})
            cues = loaded.get_json()["segments"]
            self.assertEqual(cues[0]["text"], "edited line")
            self.assertAlmostEqual(cues[0]["start"], 0.5, places=3)

    def test_load_finds_language_tagged_sidecar(self):
        # clip.mp4 + clip.zh.srt is a normal pair; requiring the exact name
        # clip.srt would force the user to transcribe again after a restart.
        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "clip.mp4"
            video.write_bytes(b"x")
            vc.write_srt(
                [{"start": 0, "end": 1, "text": "already there"}],
                Path(tmp) / "clip.zh.srt",
            )
            res = self.client.get("/api/srt", query_string={"video": str(video)})
            self.assertEqual(res.status_code, 200)
            cues = res.get_json()["segments"]
            self.assertEqual(cues[0]["text"], "already there")

    def test_list_marks_videos_that_have_a_sidecar(self):
        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "clip.mp4"
            video.write_bytes(b"x")
            vc.write_srt([{"start": 0, "end": 1, "text": "x"}], Path(tmp) / "clip.srt")
            res = self.client.get("/api/list", query_string={"dir": tmp})
            files = res.get_json()["files"]
            self.assertTrue(files)
            self.assertTrue(files[0].get("srt"))


class AppPathTests(unittest.TestCase):
    def test_resource_root_contains_the_web_ui(self):
        from web import default_media_dir, resource_root
        root = resource_root()
        self.assertTrue((root / "webui" / "index.html").is_file())
        self.assertEqual(default_media_dir(), root)


class ImportSubsTests(unittest.TestCase):
    def test_extracts_text_track_from_a_muxed_video(self):
        # A video with no sidecar .srt but a mov_text track must still yield
        # the spoken line, otherwise "import captions from video" is empty.
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            bare = tmp / "bare.mp4"
            srt = tmp / "in.srt"
            muxed = tmp / "talk.mp4"
            out = tmp / "out.srt"
            _tiny_video(bare, seconds=2)
            vc.write_srt([{"start": 0.2, "end": 1.5, "text": "imported line"}], srt)
            subprocess.run(
                ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                 "-i", str(bare), "-i", str(srt),
                 "-map", "0:v", "-map", "1", "-c:v", "copy", "-c:s", "mov_text",
                 str(muxed)],
                check=True, capture_output=True,
            )
            streams = vc.list_subtitle_streams(muxed)
            self.assertTrue(streams, "muxed file should expose a subtitle stream")
            vc.extract_subtitles(muxed, out, stream=0)
            loaded = vc.read_srt(out)
            self.assertEqual(loaded[0]["text"], "imported line")
            self.assertAlmostEqual(loaded[0]["start"], 0.2, delta=0.05)

    def test_bare_video_has_no_subtitle_streams(self):
        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "bare.mp4"
            _tiny_video(video)
            self.assertEqual(vc.list_subtitle_streams(video), [])


class ImportSubsApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from web import app
        cls.client = app.test_client()

    def test_rejects_a_video_with_no_captions(self):
        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "bare.mp4"
            _tiny_video(video)
            res = self.client.post("/api/import-subs", json={"video": str(video)})
        self.assertEqual(res.status_code, 400)
        self.assertIn("subtitle", (res.get_json() or {}).get("error", "").lower())

    def test_imports_embedded_captions_and_writes_sidecar(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            bare = tmp / "bare.mp4"
            srt = tmp / "in.srt"
            muxed = tmp / "talk.mp4"
            _tiny_video(bare, seconds=2)
            vc.write_srt([{"start": 0.2, "end": 1.5, "text": "imported line"}], srt)
            subprocess.run(
                ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                 "-i", str(bare), "-i", str(srt),
                 "-map", "0:v", "-map", "1", "-c:v", "copy", "-c:s", "mov_text",
                 str(muxed)],
                check=True, capture_output=True,
            )
            res = self.client.post("/api/import-subs", json={"video": str(muxed)})
            self.assertEqual(res.status_code, 200, res.get_json())
            data = res.get_json()
            self.assertEqual(data["segments"][0]["text"], "imported line")
            self.assertTrue(Path(data["srt"]).is_file())
            self.assertEqual(Path(data["srt"]).name, "talk.srt")


class MuxSubtitlesTests(unittest.TestCase):
    def test_exported_file_has_a_toggleable_text_track(self):
        # Export must copy the picture and attach a subtitle stream — burning
        # pixels would make Show/No captions impossible in a player.
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            source = tmp / "talk.mp4"
            srt = tmp / "talk.srt"
            output = tmp / "talk_subs.mp4"
            _tiny_video(source, seconds=2)
            vc.write_srt([{"start": 0.2, "end": 1.5, "text": "exported line"}], srt)
            vc.mux_subtitles(source, srt, output)
            streams = vc.list_subtitle_streams(output)
            self.assertTrue(streams)
            self.assertTrue(streams[0]["text"])
            extracted = tmp / "out.srt"
            vc.extract_subtitles(output, extracted, stream=0)
            self.assertEqual(vc.read_srt(extracted)[0]["text"], "exported line")


class ExportSubsApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from web import app
        cls.client = app.test_client()

    def test_rejects_export_without_captions(self):
        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "talk.mp4"
            _tiny_video(video)
            res = self.client.post("/api/export-subs", json={
                "video": str(video),
                "segments": [],
            })
        self.assertEqual(res.status_code, 400)
        err = (res.get_json() or {}).get("error", "").lower()
        self.assertTrue("caption" in err or "subtitle" in err)

    def test_export_job_writes_a_file_with_subtitles(self):
        import time
        from web import JOBS, JOBS_LOCK
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            video = tmp / "talk.mp4"
            output = tmp / "talk_subs.mp4"
            _tiny_video(video, seconds=1)
            res = self.client.post("/api/export-subs", json={
                "video": str(video),
                "output": str(output),
                "segments": [{"start": 0, "end": 1, "text": "exported line"}],
            })
            self.assertEqual(res.status_code, 200, res.get_json())
            job_id = res.get_json()["job"]
            deadline = time.time() + 30
            job = None
            while time.time() < deadline:
                job = self.client.get(f"/api/job/{job_id}").get_json()
                if job["status"] != "running":
                    break
                time.sleep(0.1)
            self.assertEqual(job["status"], "done", job.get("error") if job else None)
            out = Path(job["output"])
            self.assertTrue(out.is_file())
            sidecar = out.with_suffix(".srt")
            self.assertTrue(sidecar.is_file(), "export should write an .srt next to the video")
            self.assertEqual(vc.read_srt(sidecar)[0]["text"], "exported line")
            # A subtitle *track* is invisible in browsers/Windows player. The picture
            # itself must change, otherwise "the exported video has no captions".
            src_png, out_png = tmp / "src.png", tmp / "out.png"
            for src, dest in ((video, src_png), (out, out_png)):
                subprocess.run(
                    ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                     "-ss", "0.5", "-i", str(src), "-frames:v", "1", str(dest)],
                    check=True, capture_output=True,
                )
            self.assertNotEqual(
                src_png.read_bytes(), out_png.read_bytes(),
                "exported frame matches the source — captions were not drawn onto the picture",
            )
            with JOBS_LOCK:
                JOBS.pop(job_id, None)

    def test_unknown_api_path_returns_json_not_html(self):
        res = self.client.get("/api/this-route-does-not-exist")
        self.assertEqual(res.status_code, 404)
        self.assertTrue(res.is_json)
        self.assertNotIn(b"<!doctype", res.data.lower())

    def test_export_get_returns_json_not_html(self):
        res = self.client.get("/api/export-subs")
        self.assertIn(res.status_code, (404, 405))
        self.assertTrue(res.is_json)
        self.assertNotIn(b"<!doctype", res.data.lower())

    def test_health_lists_export_subs(self):
        res = self.client.get("/api/health")
        self.assertEqual(res.status_code, 200)
        self.assertIn("export-subs", res.get_json().get("features", []))


if __name__ == "__main__":
    unittest.main()
