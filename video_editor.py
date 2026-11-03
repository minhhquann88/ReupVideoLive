"""Build ffmpeg commands for editing videos (cut, resize, crop, speed, volume, watermark, merge)."""
import json
import os
import subprocess
import tempfile
from dataclasses import dataclass

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

# aspect name -> (w, h) ratio; cropped from the center
ASPECTS = {
    "Giữ nguyên": None,
    "9:16 (TikTok/Shorts - cắt giữa)": (9, 16),
    "1:1 (vuông - cắt giữa)": (1, 1),
    "16:9 (cắt giữa)": (16, 9),
}
RESOLUTIONS = {"Giữ nguyên": None, "1080p": 1080, "720p": 720, "480p": 480, "360p": 360}
WATERMARK_POS = {
    "Dưới phải": "x=w-tw-20:y=h-th-20",
    "Dưới trái": "x=20:y=h-th-20",
    "Trên phải": "x=w-tw-20:y=20",
    "Trên trái": "x=20:y=20",
    "Giữa": "x=(w-tw)/2:y=(h-th)/2",
}


@dataclass
class EditOptions:
    input: str
    output: str
    start: float = None
    end: float = None
    aspect: str = "Giữ nguyên"
    resolution: str = "Giữ nguyên"
    speed: float = 1.0
    volume: float = 1.0  # 0 = mute
    watermark: str = ""
    watermark_pos: str = "Dưới phải"
    fast_copy: bool = False  # cut without re-encoding (only when no filter is used)
    crf: int = 23
    preset: str = "veryfast"
    audio_only: bool = False  # export mp3


def probe(path):
    """Return dict(duration, width, height, has_audio)."""
    p = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "format=duration:stream=codec_type,width,height", "-of", "json", path],
        capture_output=True, text=True, creationflags=CREATE_NO_WINDOW)
    if p.returncode != 0:
        raise RuntimeError(p.stderr.strip() or "ffprobe lỗi")
    d = json.loads(p.stdout)
    info = {"duration": float(d.get("format", {}).get("duration", 0) or 0),
            "width": 0, "height": 0, "has_audio": False}
    for s in d.get("streams", []):
        if s.get("codec_type") == "video" and not info["width"]:
            info["width"], info["height"] = s.get("width", 0), s.get("height", 0)
        elif s.get("codec_type") == "audio":
            info["has_audio"] = True
    return info


def _filter_path(path):
    return path.replace("\\", "/").replace(":", "\\:").replace("'", "")


def _text_file(text):
    """drawtext reads the text from a file - avoids escaping issues (%, :, quotes, Vietnamese)."""
    import hashlib
    path = os.path.join(tempfile.gettempdir(),
                        "nimo_wm_" + hashlib.md5(text.encode("utf-8")).hexdigest()[:10] + ".txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return _filter_path(path)


def _font_path():
    for f in ("C:/Windows/Fonts/arialbd.ttf", "C:/Windows/Fonts/arial.ttf",
              "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"):
        if os.path.exists(f):
            return _filter_path(f)
    return None


def needs_reencode(o: EditOptions):
    return (ASPECTS.get(o.aspect) or RESOLUTIONS.get(o.resolution) or o.speed != 1.0
            or o.volume != 1.0 or o.watermark.strip())


def build_edit_cmd(o: EditOptions):
    """Return (cmd list, expected output duration in seconds or None)."""
    cmd = ["ffmpeg", "-y", "-hide_banner", "-nostats", "-progress", "pipe:1"]
    if o.start:
        cmd += ["-ss", f"{o.start:.3f}"]
    length = None
    if o.end is not None:
        length = o.end - (o.start or 0)
        if length <= 0:
            raise ValueError("Điểm cuối phải lớn hơn điểm đầu")
        cmd += ["-t", f"{length:.3f}"]  # input option: limits source length (before speed change)
    cmd += ["-i", o.input]

    if o.audio_only:
        af = _audio_filters(o)
        if af:
            cmd += ["-af", ",".join(af)]
        cmd += ["-vn", "-c:a", "libmp3lame", "-q:a", "2", o.output]
        return cmd, (length / o.speed if length else None)

    if o.fast_copy and not needs_reencode(o):
        cmd += ["-map", "0", "-c", "copy", "-avoid_negative_ts", "make_zero",
                "-movflags", "+faststart", o.output]
        return cmd, length

    vf = []
    ratio = ASPECTS.get(o.aspect)
    res = RESOLUTIONS.get(o.resolution)
    if ratio:
        rw, rh = ratio
        # largest centered rect with the ratio, even dimensions
        vf.append(f"crop='trunc(min(iw,ih*{rw}/{rh})/2)*2':'trunc(min(ih,iw*{rh}/{rw})/2)*2'")
        if res:  # exact output size, shorter side == res
            short, long_ = min(rw, rh), max(rw, rh)
            other = round(res * long_ / short / 2) * 2
            vf.append(f"scale={other if rw > rh else res}:{res if rw > rh else other}")
    elif res:
        # scale so the shorter side == res, keep even dimensions
        vf.append(f"scale='if(gt(iw,ih),-2,{res})':'if(gt(iw,ih),{res},-2)'")
    if o.speed != 1.0:
        vf.append(f"setpts=PTS/{o.speed}")
    if o.watermark.strip():
        font = _font_path()
        dt = f"drawtext=textfile='{_text_file(o.watermark.strip())}':expansion=none:fontsize=h/18:fontcolor=white" \
             f":borderw=2:bordercolor=black@0.7:{WATERMARK_POS.get(o.watermark_pos, WATERMARK_POS['Dưới phải'])}"
        if font:
            dt += f":fontfile='{font}'"
        vf.append(dt)
    if vf:
        cmd += ["-vf", ",".join(vf)]
    af = _audio_filters(o)
    if o.volume == 0:
        cmd += ["-an"]
    elif af:
        cmd += ["-af", ",".join(af)]
    cmd += ["-c:v", "libx264", "-preset", o.preset, "-crf", str(o.crf), "-pix_fmt", "yuv420p"]
    if o.volume != 0:
        cmd += ["-c:a", "aac", "-b:a", "160k"]
    cmd += ["-movflags", "+faststart", o.output]
    return cmd, (length / o.speed if length else None)


def _audio_filters(o):
    af = []
    if o.speed != 1.0:
        s = o.speed  # atempo supports 0.5..2.0 per instance -> chain for others
        while s > 2.0:
            af.append("atempo=2.0")
            s /= 2.0
        while s < 0.5:
            af.append("atempo=0.5")
            s /= 0.5
        af.append(f"atempo={s:.4f}")
    if o.volume not in (0, 1.0):
        af.append(f"volume={o.volume:.2f}")
    return af


def build_merge_cmd(inputs, output, reencode=False):
    """Concat files. Returns (cmd, list_file_path) - caller removes list file."""
    fd, list_path = tempfile.mkstemp(suffix=".txt", prefix="nimo_concat_")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        for p in inputs:
            f.write("file '" + os.path.abspath(p).replace("\\", "/").replace("'", "'\\''") + "'\n")
    cmd = ["ffmpeg", "-y", "-hide_banner", "-nostats", "-progress", "pipe:1",
           "-f", "concat", "-safe", "0", "-i", list_path]
    if reencode:
        cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-c:a", "aac"]
    else:
        cmd += ["-c", "copy"]
    cmd += ["-movflags", "+faststart", output]
    return cmd, list_path
