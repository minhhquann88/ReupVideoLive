"""Tải ffmpeg + ffprobe vào thư mục bin/ của project (không cài global).

    python setup_ffmpeg.py

Nguồn: bản "release essentials" của gyan.dev (cũng là nguồn của winget Gyan.FFmpeg), có kiểm tra SHA-256.
"""
import hashlib
import os
import shutil
import sys
import tempfile
import zipfile

import requests

URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
BIN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bin")
NAMES = ("ffmpeg.exe", "ffprobe.exe")


def main():
    if sys.platform != "win32":
        sys.exit("Script này dành cho Windows. Trên máy khác hãy cài ffmpeg bằng trình quản lý gói.")
    if all(os.path.exists(os.path.join(BIN, n)) for n in NAMES) and "--force" not in sys.argv:
        print(f"ffmpeg đã có trong {BIN} (dùng --force để tải lại).")
        return
    expected = requests.get(URL + ".sha256", timeout=30).text.split()[0].strip().lower()
    tmp = tempfile.mkdtemp(prefix="nimo_ffmpeg_")
    try:
        zpath = os.path.join(tmp, "ffmpeg.zip")
        h = hashlib.sha256()
        with requests.get(URL, stream=True, timeout=60) as r:
            r.raise_for_status()
            total = int(r.headers.get("content-length") or 0)
            done = 0
            with open(zpath, "wb") as f:
                for chunk in r.iter_content(1 << 20):
                    f.write(chunk)
                    h.update(chunk)
                    done += len(chunk)
                    if total:
                        print(f"\rĐang tải ffmpeg: {done / 1e6:.0f}/{total / 1e6:.0f} MB", end="", flush=True)
        print()
        if h.hexdigest() != expected:
            sys.exit(f"Sai SHA-256 (tải lỗi hoặc file bị thay đổi): {h.hexdigest()} != {expected}")
        os.makedirs(BIN, exist_ok=True)
        with zipfile.ZipFile(zpath) as z:
            version = ""
            for info in z.infolist():
                base = os.path.basename(info.filename)
                if base in NAMES and "/bin/" in info.filename:
                    with z.open(info) as src, open(os.path.join(BIN, base), "wb") as dst:
                        shutil.copyfileobj(src, dst)
                    version = info.filename.split("/")[0]
        print(f"✅ Đã cài {version} vào {BIN}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    main()
