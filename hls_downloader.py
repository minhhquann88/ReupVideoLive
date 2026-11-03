"""Core HLS (m3u8) downloader for Nimo TV - no Qt dependency, usable from CLI."""
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from urllib.parse import urljoin

import requests

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/130.0 Safari/537.36",
    "Referer": "https://www.nimo.tv/",
    "Origin": "https://www.nimo.tv",
}

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


@dataclass
class Segment:
    index: int
    url: str
    duration: float
    start: float  # offset from beginning of playlist (seconds)


@dataclass
class Playlist:
    url: str
    segments: list = field(default_factory=list)
    is_live: bool = True
    target_duration: float = 6.0
    encrypted: bool = False
    query: str = ""  # auth query of the playlist URL, some CDNs need it on segments too

    @property
    def total_duration(self):
        return sum(s.duration for s in self.segments)


class DownloadCancelled(Exception):
    pass


class LinkForbidden(Exception):
    pass


def link_expiry(url):
    """Nimo/Huya signed links carry wsTime (hex unix time). Returns datetime or None."""
    import datetime
    m = re.search(r"[?&]wsTime=([0-9a-fA-F]+)", url)
    if not m:
        return None
    try:
        return datetime.datetime.fromtimestamp(int(m.group(1), 16))
    except (ValueError, OverflowError, OSError):
        return None


def fetch_text(session, url, timeout=20):
    r = session.get(url, headers=HEADERS, timeout=timeout)
    if r.status_code == 403:
        exp = link_expiry(url)
        hint = f" (hạn token: {exp:%H:%M:%S %d/%m})" if exp else ""
        raise LinkForbidden(
            f"Server từ chối link (403: {r.headers.get('x-exception-info', 'forbidden')}){hint}.\n"
            "Link Nimo có chữ ký và chỉ dùng được trong thời gian ngắn - hãy lấy link m3u8 mới "
            "(F12 → Network → lọc 'm3u8' khi mở video).")
    r.raise_for_status()
    return r.text


def parse_playlist(session, url):
    """Fetch and parse an m3u8. Resolves master playlists to the highest bandwidth variant."""
    text = fetch_text(session, url)
    if "#EXTM3U" not in text:
        raise ValueError("Link không phải playlist m3u8 hợp lệ")

    lines = [l.strip() for l in text.splitlines() if l.strip()]

    # Master playlist -> pick best variant
    if any(l.startswith("#EXT-X-STREAM-INF") for l in lines):
        best_bw, best_url = -1, None
        for i, l in enumerate(lines):
            if l.startswith("#EXT-X-STREAM-INF"):
                m = re.search(r"BANDWIDTH=(\d+)", l)
                bw = int(m.group(1)) if m else 0
                if i + 1 < len(lines) and bw > best_bw:
                    best_bw, best_url = bw, urljoin(url, lines[i + 1])
        if not best_url:
            raise ValueError("Không tìm thấy luồng trong master playlist")
        return parse_playlist(session, best_url)

    pl = Playlist(url=url)
    pl.query = url.split("?", 1)[1] if "?" in url else ""
    seq = 0
    duration = None
    offset = 0.0
    for l in lines:
        if l.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            seq = int(l.split(":", 1)[1])
        elif l.startswith("#EXT-X-TARGETDURATION:"):
            pl.target_duration = float(l.split(":", 1)[1])
        elif l.startswith("#EXT-X-ENDLIST"):
            pl.is_live = False
        elif l.startswith("#EXT-X-PLAYLIST-TYPE:VOD"):
            pl.is_live = False
        elif l.startswith("#EXT-X-KEY") and "METHOD=NONE" not in l:
            pl.encrypted = True
        elif l.startswith("#EXTINF:"):
            duration = float(l.split(":", 1)[1].split(",")[0])
        elif not l.startswith("#") and duration is not None:
            pl.segments.append(Segment(seq, urljoin(url, l), duration, offset))
            offset += duration
            seq += 1
            duration = None
    return pl


def fmt_time(sec):
    sec = max(0, int(sec))
    return f"{sec // 3600:02d}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


def parse_time(text):
    """'hh:mm:ss', 'mm:ss' or seconds -> float seconds. Empty -> None."""
    text = (text or "").strip()
    if not text:
        return None
    parts = [float(p) for p in text.split(":")]
    sec = 0.0
    for p in parts:
        sec = sec * 60 + p
    return sec


class HLSDownloader:
    """Downloads an HLS stream (VOD or live) to an MP4 file.

    Callbacks (all optional):
      on_log(str), on_progress(done, total, bytes_done), on_info(Playlist)
    """

    def __init__(self, url, output, start=None, end=None, threads=8,
                 on_log=None, on_progress=None, on_info=None, playlist=None):
        self.url = url
        self.output = output
        self.start = start
        self.end = end
        self.threads = threads
        self.on_log = on_log or (lambda s: print(s))
        self.on_progress = on_progress or (lambda d, t, b: None)
        self.on_info = on_info or (lambda p: None)
        self._stop = threading.Event()
        self.session = requests.Session()
        adapter = requests.adapters.HTTPAdapter(pool_connections=threads, pool_maxsize=threads * 2)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)
        self.bytes_done = 0
        self._query = ""
        # Nimo: segments need the playlist's auth token (wsSecret...) appended
        self._append_query = True
        self._tried_plain = False
        self._trim_len = None
        self.playlist = playlist  # pre-fetched Playlist; Nimo tokens allow ONE playlist request

    def stop(self):
        """Request stop. For live streams the recorded part is still saved."""
        self._stop.set()

    # ---------- segment fetch ----------
    def _seg_url(self, seg):
        if self._append_query and self._query:
            return seg.url + ("&" if "?" in seg.url else "?") + self._query
        return seg.url

    def _download_segment(self, seg, path, retries=5):
        for attempt in range(retries):
            if self._stop.is_set():
                raise DownloadCancelled()
            try:
                r = self.session.get(self._seg_url(seg), headers=HEADERS, timeout=30)
                if r.status_code == 403 and self._query and self._append_query and not self._tried_plain:
                    # some CDNs reject the extra query - retry once without it
                    self._tried_plain = True
                    self._append_query = False
                    r = self.session.get(self._seg_url(seg), headers=HEADERS, timeout=30)
                    if r.status_code == 403:
                        self._append_query = True
                if r.status_code == 403:
                    raise LinkForbidden("Segment bị từ chối (403) - link đã hết hạn, hãy lấy link mới.")
                r.raise_for_status()
                with open(path, "wb") as f:
                    f.write(r.content)
                self.bytes_done += len(r.content)
                return path
            except (DownloadCancelled, LinkForbidden):
                raise
            except Exception as e:
                if attempt == retries - 1:
                    raise RuntimeError(f"Lỗi tải segment {seg.index}: {e}")
                time.sleep(1 + attempt)

    # ---------- main ----------
    def run(self):
        tmpdir = tempfile.mkdtemp(prefix="nimo_dl_")
        ts_path = os.path.join(tmpdir, "joined.ts")
        try:
            pl = self.playlist or parse_playlist(self.session, self.url)
            self._query = pl.query
            if pl.encrypted:
                self.on_log("⚠ Playlist có mã hoá (EXT-X-KEY) - có thể không phát được.")
            self.on_info(pl)
            if pl.is_live:
                self.on_log("🔴 Phát hiện LIVE stream - đang ghi, bấm Dừng để kết thúc và lưu.")
                self._run_live(pl, ts_path)
                trim_offset = 0.0
            else:
                self.on_log(f"📼 VOD: {len(pl.segments)} segment, dài {fmt_time(pl.total_duration)}")
                trim_offset = self._run_vod(pl, tmpdir, ts_path)

            if not os.path.exists(ts_path) or os.path.getsize(ts_path) == 0:
                raise RuntimeError("Không có dữ liệu nào được tải.")
            self._remux(ts_path, trim_offset)
            self.on_log(f"✅ Hoàn tất: {self.output}")
            return self.output
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def _run_vod(self, pl, tmpdir, ts_path):
        segs = pl.segments
        start, end = self.start, self.end
        if start is not None or end is not None:
            s = start or 0.0
            e = end if end is not None else pl.total_duration
            segs = [x for x in segs if x.start + x.duration > s and x.start < e]
            if not segs:
                raise ValueError("Khoảng thời gian chọn nằm ngoài video")
            self.on_log(f"✂ Chỉ tải đoạn {fmt_time(s)} → {fmt_time(e)} ({len(segs)} segment)")
        total = len(segs)
        paths = {}
        done = 0
        self.on_progress(0, total, 0)
        with ThreadPoolExecutor(max_workers=self.threads) as ex:
            futs = {ex.submit(self._download_segment, sg, os.path.join(tmpdir, f"{i:06d}.ts")): i
                    for i, sg in enumerate(segs)}
            try:
                for fut in as_completed(futs):
                    paths[futs[fut]] = fut.result()
                    done += 1
                    self.on_progress(done, total, self.bytes_done)
            except BaseException:
                self._stop.set()
                for f in futs:
                    f.cancel()
                raise
        self.on_log("🔗 Đang ghép segment...")
        with open(ts_path, "wb") as out:
            for i in range(total):
                with open(paths[i], "rb") as f:
                    shutil.copyfileobj(f, out)
                os.remove(paths[i])
        # Precise trim offset inside the joined file
        self._trim_len = None
        if start is not None or end is not None:
            first = segs[0].start
            off = max(0.0, (start or 0.0) - first)
            if end is not None:
                self._trim_len = end - max(start or 0.0, first)
            return off
        return 0.0

    def _run_live(self, pl, ts_path):
        seen = set()
        self._trim_len = None
        count = 0
        recorded = 0.0  # seconds of media written
        with open(ts_path, "wb") as out:
            while not self._stop.is_set():
                new = [s for s in pl.segments if s.url.split("?")[0] not in seen]
                for seg in new:
                    if self._stop.is_set():
                        break
                    seen.add(seg.url.split("?")[0])
                    p = ts_path + ".part"
                    try:
                        self._download_segment(seg, p)
                    except DownloadCancelled:
                        break
                    except Exception as e:
                        self.on_log(f"⚠ {e}")
                        continue
                    with open(p, "rb") as f:
                        shutil.copyfileobj(f, out)
                    out.flush()
                    count += 1
                    recorded += seg.duration
                    self.on_progress(count, 0, self.bytes_done)
                    if self.end is not None and recorded >= self.end:
                        self.on_log("⏱ Đã đủ thời lượng ghi.")
                        self._stop.set()
                if not pl.is_live:
                    self.on_log("Stream đã kết thúc.")
                    break
                # wait then refresh playlist
                self._stop.wait(max(1.0, pl.target_duration / 2))
                if self._stop.is_set():
                    break
                try:
                    pl = parse_playlist(self.session, pl.url)
                except Exception as e:
                    self.on_log(f"⚠ Lỗi làm mới playlist: {e}")
        self.on_log(f"Đã ghi {count} segment.")

    def _remux(self, ts_path, offset):
        self.on_log("🎬 Đang chuyển sang MP4 (không nén lại)...")
        cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
        if offset > 0.05:
            cmd += ["-ss", f"{offset:.3f}"]
        cmd += ["-i", ts_path]
        if self._trim_len:
            cmd += ["-t", f"{self._trim_len:.3f}"]
        cmd += ["-c", "copy", "-bsf:a", "aac_adtstoasc", "-movflags", "+faststart", self.output]
        p = subprocess.run(cmd, capture_output=True, text=True, creationflags=CREATE_NO_WINDOW)
        if p.returncode != 0:
            raise RuntimeError("ffmpeg lỗi: " + p.stderr[-800:])


if __name__ == "__main__":
    import argparse
    import sys
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap =argparse.ArgumentParser(description="Tải video Nimo (m3u8)")
    ap.add_argument("url")
    ap.add_argument("-o", "--output", default="nimo.mp4")
    ap.add_argument("--start", help="hh:mm:ss")
    ap.add_argument("--end", help="hh:mm:ss")
    ap.add_argument("-t", "--threads", type=int, default=8)
    a = ap.parse_args()
    d = HLSDownloader(a.url, a.output, parse_time(a.start), parse_time(a.end), a.threads,
                      on_progress=lambda d, t, b: print(f"\r{d}/{t}  {b / 1e6:.1f} MB", end=""))
    d.run()
