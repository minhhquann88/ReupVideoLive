"""Đường dẫn ffmpeg/ffprobe: ưu tiên bản trong bin/ của project, sau đó mới tới PATH."""
import os
import shutil

BIN_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bin")


def _find(name):
    for cand in (name + ".exe", name):
        p = os.path.join(BIN_DIR, cand)
        if os.path.isfile(p):
            return p
    return shutil.which(name)


FFMPEG = _find("ffmpeg") or "ffmpeg"
FFPROBE = _find("ffprobe") or "ffprobe"


def available():
    return bool(_find("ffmpeg") and _find("ffprobe"))
