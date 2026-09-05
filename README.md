# video_cutter

Remove the parts of a video you do not want and join what is left back into one file.
A thin wrapper around ffmpeg: no library to install, the input is never modified.

## Requirements

`ffmpeg` and `ffprobe` on PATH. Nothing else - no Python packages.

```bash
ffmpeg -version        # if this prints a version, you are ready
```

## The web UI (easiest)

```bash
cd video_cutter
python web.py --dir "D:/my videos"      # opens http://127.0.0.1:8770 in your browser
```

Pick a folder on the left, click a video, watch it, and mark the parts you do not want:
press <kbd>I</kbd> where the unwanted part starts and <kbd>O</kbd> where it ends. The red
blocks on the timeline are what will be removed; the summary line shows how long the
result will be. Then press **Cut it** and watch the log.

| key           |                                     |
| ------------- | ----------------------------------- |
| `space` / `K` | play, pause                         |
| `←` `→`       | one second back / forward           |
| `J` `L`       | five seconds back / forward         |
| `I` `O`       | mark the start / the end of a range |

Also there: switch between "these ranges are removed" and "only these are kept", type any
time by hand, **Preview the plan** (writes nothing), play the result, show it in Explorer,
and **Copy CLI command** if you want the same cut as a command line.

Nothing is uploaded - the page streams the file off your disk and ffmpeg writes the result
next to it. The server listens on `127.0.0.1` only, but it does read and write local files,
so do not put it on a public interface. Requires `flask` (`pip install flask`).

Formats the browser cannot play (`.mkv`, `.avi`, ...) still get cut; you just do not get a
preview, so the times have to be typed.

## The CLI - say it one of two ways

```bash
# drop these ranges, keep the rest  ->  writes talk_cut.mp4 next to the input
python video_cutter.py talk.mp4 --cut 0:00-0:12 --cut 5:30-6:10

# keep only these ranges, drop the rest
python video_cutter.py talk.mp4 --keep 1:00-2:30 -o highlight.mp4
```

Times accept `90`, `1:30`, `0:01:30`, `1:30.5` and the word `end`.
Ranges are written `START-END` (`START..END` also works). `--cut` and `--keep` can be
repeated and may be given in any order; overlapping ranges are merged.

## Useful flags

| flag                             | what it does                                                                      |
| -------------------------------- | --------------------------------------------------------------------------------- |
| `--info`                         | print duration and streams, cut nothing - use it to pick your ranges              |
| `--dry-run`                      | print what would be cut and kept, write nothing                                   |
| `--mode copy`                    | no re-encoding, seconds instead of minutes, but cuts snap to the nearest keyframe |
| `--separate`                     | write each kept range as its own file (`talk_part01.mp4`, ...) instead of joining |
| `-o`                             | output file, or a directory to write into                                         |
| `--crf 20` / `--preset veryfast` | re-encode quality / speed. Lower CRF is better quality and a bigger file          |
| `--verbose`                      | show ffmpeg's own output                                                          |

### Which mode?

`reencode` (the default) is frame-accurate: the cut lands exactly where you said.
`copy` never touches the video streams, so it is many times faster and loses no quality,
but ffmpeg can only start a copied piece on a keyframe - the cut can be off by a second
or two. Use `copy` for rough trims of long recordings, the default when the exact frame
matters.

## Several videos at once

```bash
python video_cutter.py --config edits.json
```

```json
{
  "mode": "reencode",
  "crf": 22,
  "jobs": [
    { "input": "lecture1.mp4", "cut": ["0:00-0:14", "22:10-end"], "output": "clean/lecture1.mp4" },
    { "input": "lecture2.mp4", "cut": ["3:05-4:40"] },
    { "input": "demo.mkv",     "keep": ["1:00-2:30"], "mode": "copy" }
  ]
}
```

Keys outside `"jobs"` are defaults for every job; a job can override any of them.
A bare JSON list of jobs works too. One failing job does not stop the others - the exit
code is 1 if any job failed.

## How it works

For every range you keep, ffmpeg extracts that piece on its own
(`-ss START -t DURATION`), then the pieces are concatenated with the concat demuxer and
`-c copy`, so the join step never re-encodes. In `reencode` mode the pieces are H.264 +
AAC; a container that cannot hold those (`.webm`, `.avi`, ...) is written as `.mp4`.
Subtitle and data streams are dropped, because they break the concat step.



Web 

cd video_cutter
python web.py

会自动开浏览器到 http://127.0.0.1:8770，默认就从当前文件夹开始 —— 你放在 video_cutter/ 里的那几个录屏（Inject.mp4、StateMachine.mp4、Performance.mp4 …）会直接列出来。想从别处开始就 python web.py --dir "D:/my videos"。

操作方式：左边点文件夹和视频 → 播放 → 在不要的那段开头按 <kbd>I</kbd>、结尾按 <kbd>O</kbd>。时间轴上红块就是要删掉的部分，下面实时显示"原长 / 删掉多少 / 结果多长"。然后按 Cut it，日志实时滚，完成后可以直接在页面里播放结果、或者"Show in folder"跳到资源管理器。
