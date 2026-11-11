"""ffmpeg helpers: probe, ghép nhiều vùng cắt vào một bố cục."""
import json
import os
import subprocess
from dataclasses import dataclass

from ffbin import FFMPEG, FFPROBE

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

# Kích thước khung xuất có sẵn
CANVAS_PRESETS = {
    "Dọc 9:16 (1080x1920)": (1080, 1920),
    "Dọc 9:16 (720x1280)": (720, 1280),
    "Ngang 16:9 (1920x1080)": (1920, 1080),
    "Ngang 16:9 (1280x720)": (1280, 720),
    "Vuông 1:1 (1080x1080)": (1080, 1080),
    "Dọc 4:5 (1080x1350)": (1080, 1350),
    "Tuỳ chỉnh": None,
}


@dataclass
class Region:
    """Một vùng cắt: src = toạ độ trên video gốc, dst = vị trí/kích thước trên khung xuất (pixel)."""
    sx: float
    sy: float
    sw: float
    sh: float
    dx: float = 0
    dy: float = 0
    dw: float = 0
    dh: float = 0
    name: str = ""


def probe(path):
    """Return dict(duration, width, height, has_audio, fps)."""
    p = subprocess.run(
        [FFPROBE, "-v", "error", "-show_entries",
         "format=duration:stream=codec_type,width,height,r_frame_rate,avg_frame_rate", "-of", "json", path],
        capture_output=True, text=True, creationflags=CREATE_NO_WINDOW)
    if p.returncode != 0:
        raise RuntimeError(p.stderr.strip() or "ffprobe lỗi")
    d = json.loads(p.stdout)
    info = {"duration": float(d.get("format", {}).get("duration", 0) or 0),
            "width": 0, "height": 0, "has_audio": False, "fps": 30.0}
    for s in d.get("streams", []):
        if s.get("codec_type") == "video" and not info["width"]:
            info["width"], info["height"] = s.get("width", 0), s.get("height", 0)
            for key in ("r_frame_rate", "avg_frame_rate"):
                fps = _rate(s.get(key))
                if 1 <= fps <= 120:
                    info["fps"] = fps
                    break
        elif s.get("codec_type") == "audio":
            info["has_audio"] = True
    return info


def _rate(text):
    try:
        n, _, d = str(text).partition("/")
        return float(n) / float(d or 1)
    except (ValueError, ZeroDivisionError):
        return 0.0


def _even(v, lo=2):
    return max(lo, int(round(v / 2)) * 2)


def _even_floor(v):
    return int(v) // 2 * 2


def build_compose_filter(regions, src_w, src_h, out_w, out_h, bg="000000", fps=30.0, duration=None):
    """filter_complex đặt từng vùng cắt (scale theo dst) lên khung out_w x out_h. Nhãn ra: [vout].

    Tối ưu tốc độ: vùng dưới cùng được `pad` thành nền (thay vì nguồn `color` RGBA khiến cả khung
    1080x1920 bị đổi hệ màu ở mỗi frame), mọi bước giữ yuv420p, phần vùng nằm ngoài khung bị cắt bỏ
    trước khi ghép. Nhanh hơn ~3 lần so với cách cũ.
    """
    if not regions:
        raise ValueError("Chưa có vùng cắt nào")
    W, H = _even(out_w), _even(out_h)
    bg = "0x" + bg.lstrip("#")
    items = []   # (crop+scale+clip filter, x, y) cho các vùng còn thấy được trên khung
    for r in regions:
        sx = _even_floor(min(max(0, r.sx), src_w - 2))
        sy = _even_floor(min(max(0, r.sy), src_h - 2))
        sw = min(_even(r.sw), _even_floor(src_w - sx))
        sh = min(_even(r.sh), _even_floor(src_h - sy))
        dw, dh = _even(r.dw), _even(r.dh)
        dx, dy = int(round(r.dx)) // 2 * 2, int(round(r.dy)) // 2 * 2
        # phần nhìn thấy được trên khung
        x0, y0 = max(0, dx), max(0, dy)
        x1, y1 = min(W, dx + dw), min(H, dy + dh)
        vw, vh = _even_floor(x1 - x0), _even_floor(y1 - y0)
        if vw < 2 or vh < 2:
            continue
        f = f"crop={sw}:{sh}:{sx}:{sy},scale={dw}:{dh}"
        if (vw, vh) != (dw, dh):
            f += f",crop={vw}:{vh}:{x0 - dx}:{y0 - dy}"
        items.append((f, x0, y0))
    if not items:
        raise ValueError("Không có vùng nào nằm trong khung xuất")
    n = len(items)
    # fps: chuẩn hoá nguồn VFR (VOD Nimo có đoạn rất thưa frame) để không bị đứng hình / lệch tiếng
    parts = [f"[0:v]fps={fps:.3f},format=yuv420p" + (f",split={n}" + "".join(f"[s{i}]" for i in range(n))
                                                      if n > 1 else "[s0]")]
    for i, (f, x, y) in enumerate(items):
        if i == 0:   # vùng dưới cùng làm nền
            f += f",pad={W}:{H}:{x}:{y}:color={bg}"
        parts.append(f"[s{i}]{f}[c{i}]")
    prev = "c0"
    for i in range(1, n):
        _f, x, y = items[i]
        parts.append(f"[{prev}][c{i}]overlay={x}:{y}:format=yuv420[b{i}]")
        prev = f"b{i}"
    parts.append(f"[{prev}]setsar=1,format=yuv420p[vout]")
    return ";".join(parts)


def build_compose_cmd(input, output, regions, src_w, src_h, out_w, out_h, bg="000000", fps=30.0,
                      start=None, end=None, has_audio=True, crf=20, preset="veryfast", src_duration=None):
    """Return (cmd list, expected duration or None). Vùng sau đè lên vùng trước."""
    cmd = [FFMPEG, "-y", "-hide_banner", "-nostats", "-progress", "pipe:1"]
    if start:
        cmd += ["-ss", f"{start:.3f}"]
    length = None
    if end is not None:
        length = end - (start or 0)
        if length <= 0:
            raise ValueError("Điểm cuối phải lớn hơn điểm đầu")
        cmd += ["-t", f"{length:.3f}"]
    elif src_duration:
        length = max(0.1, src_duration - (start or 0))
    if not length:
        raise ValueError("Không biết thời lượng video")
    cmd += ["-i", input,
            "-filter_complex", build_compose_filter(regions, src_w, src_h, out_w, out_h, bg, fps, length),
            "-map", "[vout]"]
    if has_audio:
        cmd += ["-map", "0:a:0?", "-c:a", "aac", "-b:a", "160k"]
    cmd += ["-t", f"{length:.3f}", "-c:v", "libx264", "-preset", preset, "-crf", str(crf),
            "-movflags", "+faststart", output]
    return cmd, length

