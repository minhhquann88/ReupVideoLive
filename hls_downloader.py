"""Core HLS (m3u8) downloader for Nimo TV - no Qt dependency, usable from CLI."""
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from urllib.parse import urljoin

import requests

from ffbin import FFMPEG

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
        if re.search(r"[?&](sdk_sid|dMod)=", url):
            why = ("Link này copy từ tab Network của trình duyệt: trình phát đã dùng nó để phát video, "
                   "mà mỗi link Nimo chỉ gọi được 1 lần.\n")
        else:
            why = "Mỗi link Nimo chỉ gọi được 1 lần (hoặc đã hết hạn).\n"
        raise LinkForbidden(
            f"Server từ chối link (403: {r.headers.get('x-exception-info', 'forbidden')}){hint}.\n"
            + why +
            "Hãy lấy link chưa dùng từ response API (liveVideoViewList → videoUrl / "
            "multiResolutionVideoUrl) và dán vào tool ngay, không mở video trên web trước.")
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

    def _download_segment(self, seg, retries=5):
        """Tải 1 segment, trả bytes (giữ trong RAM, không ghi file tạm)."""
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
                self.bytes_done += len(r.content)
                return r.content
            except (DownloadCancelled, LinkForbidden):
                raise
            except Exception as e:
                if attempt == retries - 1:
                    raise RuntimeError(f"Lỗi tải segment {seg.index}: {e}")
                time.sleep(1 + attempt)

    # ---------- main ----------
    def run(self):
        """Segment được ghi thẳng vào ffmpeg (stdin) theo thứ tự -> không có file .ts tạm,
        ổ đĩa chỉ cần chỗ cho file MP4 đầu ra."""
        pl = self.playlist or parse_playlist(self.session, self.url)
        self._query = pl.query
        if pl.encrypted:
            self.on_log("⚠ Playlist có mã hoá (EXT-X-KEY) - có thể không phát được.")
        self.on_info(pl)
        out_dir = os.path.dirname(os.path.abspath(self.output))
        os.makedirs(out_dir, exist_ok=True)
        ok = False
        try:
            if pl.is_live:
                self.on_log("🔴 Phát hiện LIVE stream - đang ghi, bấm Dừng để kết thúc và lưu.")
                self._open_ffmpeg(0.0, None)
                written = self._run_live(pl)
            else:
                self.on_log(f"📼 VOD: {len(pl.segments)} segment, dài {fmt_time(pl.total_duration)}")
                written = self._run_vod(pl)
            self._close_ffmpeg(abort=written == 0)
            if written == 0:
                raise RuntimeError("Không có dữ liệu nào được tải.")
            ok = True
            self.on_log(f"✅ Hoàn tất: {self.output}")
            return self.output
        finally:
            if not ok:
                self._close_ffmpeg(abort=True)
                self._remove_partial()

    # ---------- ffmpeg pipe ----------
    def _open_ffmpeg(self, offset, trim_len):
        self.on_log("🎬 Ghi thẳng sang MP4 (không nén lại, không dùng file tạm)...")
        cmd = [FFMPEG, "-y", "-hide_banner", "-loglevel", "error", "-f", "mpegts"]
        if offset > 0.05:
            cmd += ["-ss", f"{offset:.3f}"]
        cmd += ["-i", "pipe:0"]
        if trim_len:
            cmd += ["-t", f"{trim_len:.3f}"]
        cmd += ["-map", "0:v?", "-map", "0:a?", "-c", "copy", "-bsf:a", "aac_adtstoasc",
                "-movflags", "+faststart", self.output]
        self._ff_err = tempfile.TemporaryFile()
        self._ff = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                    stderr=self._ff_err, creationflags=CREATE_NO_WINDOW)

    def _ff_error(self):
        try:
            self._ff_err.seek(0)
            return self._ff_err.read().decode("utf-8", "replace")[-800:]
        except Exception:
            return ""

    def _feed(self, data):
        """Ghi vào ffmpeg. Trả False khi ffmpeg đã tự kết thúc thành công (đủ -t khi tải 1 đoạn)
        -> không cần gửi thêm dữ liệu."""
        try:
            self._ff.stdin.write(data)
            return True
        except (BrokenPipeError, OSError):
            if self._ff.wait() == 0:
                return False
            raise RuntimeError("ffmpeg lỗi: " + self._explain(self._ff_error()))

    def _close_ffmpeg(self, abort=False):
        ff = getattr(self, "_ff", None)
        if ff is None:
            return
        self._ff = None
        if abort:
            ff.kill()
            ff.wait()
            return
        try:
            ff.stdin.close()
        except OSError:   # ffmpeg đã tự đóng pipe
            pass
        if ff.wait() != 0:
            raise RuntimeError("ffmpeg lỗi: " + self._explain(self._ff_error()))

    def _explain(self, err):
        if "No space left" in err:
            free = shutil.disk_usage(os.path.dirname(os.path.abspath(self.output))).free
            return (f"Ổ đĩa đã đầy (còn {free / 1e9:.1f} GB). Hãy giải phóng dung lượng, chọn ổ khác "
                    "hoặc chỉ tải 1 đoạn / chất lượng thấp hơn.\n" + err)
        return err

    def _remove_partial(self):
        try:
            if os.path.exists(self.output):
                os.remove(self.output)
                self.on_log(f"🗑 Đã xoá file dở dang: {self.output}")
        except OSError:
            pass

    MIN_FREE = 300 * 10**6   # dừng khi ổ còn ít hơn mức này

    def _free_space(self):
        return shutil.disk_usage(os.path.dirname(os.path.abspath(self.output))).free

    def _check_space(self, done_sec, total_sec):
        """Ước lượng dung lượng file ra từ phần đã tải (1 lần) + theo dõi chỗ trống liên tục."""
        free = self._free_space()
        if free < self.MIN_FREE:
            raise RuntimeError(
                f"Ổ đĩa sắp đầy (còn {free / 1e6:.0f} MB) - đã dừng tải.\n"
                "Hãy chọn thư mục lưu ở ổ khác, giải phóng dung lượng, chọn chất lượng thấp hơn "
                "hoặc chỉ tải 1 đoạn (từ–đến).")
        if self._space_checked or done_sec < 60 and done_sec < total_sec:
            return
        self._space_checked = True
        need = self.bytes_done / done_sec * total_sec
        written = os.path.getsize(self.output) if os.path.exists(self.output) else 0
        left = need - written
        self.on_log(f"💾 Ước tính file ~{need / 1e9:.1f} GB, ổ còn trống {free / 1e9:.1f} GB")
        if left > free * 2:
            raise RuntimeError(
                f"Không đủ dung lượng: file cần khoảng {need / 1e9:.1f} GB nhưng ổ chỉ còn {free / 1e9:.1f} GB.\n"
                "Hãy chọn thư mục lưu ở ổ khác, giải phóng dung lượng, chọn chất lượng thấp hơn "
                "hoặc chỉ tải 1 đoạn (từ–đến).")
        if left > free:
            self.on_log("⚠ Có thể không đủ dung lượng - vẫn tải tiếp, sẽ dừng nếu ổ sắp đầy.")

    # ---------- VOD ----------
    def _run_vod(self, pl):
        segs = pl.segments
        start, end = self.start, self.end
        offset, trim_len = 0.0, None
        if start is not None or end is not None:
            s = start or 0.0
            e = end if end is not None else pl.total_duration
            segs = [x for x in segs if x.start + x.duration > s and x.start < e]
            if not segs:
                raise ValueError("Khoảng thời gian chọn nằm ngoài video")
            self.on_log(f"✂ Chỉ tải đoạn {fmt_time(s)} → {fmt_time(e)} ({len(segs)} segment)")
            first = segs[0].start
            offset = max(0.0, s - first)
            if end is not None:
                trim_len = end - max(s, first)
        total = len(segs)
        total_sec = sum(x.duration for x in segs)
        self._space_checked = False
        self._open_ffmpeg(offset, trim_len)
        self.on_progress(0, total, 0)
        window = max(4, self.threads * 3)   # số segment tải trước, giữ trong RAM
        done_sec = 0.0
        with ThreadPoolExecutor(max_workers=self.threads) as ex:
            futs = {}
            try:
                for i in range(total):
                    for j in range(i, min(total, i + window)):
                        if j not in futs:
                            futs[j] = ex.submit(self._download_segment, segs[j])
                    data = futs.pop(i).result()
                    if not self._feed(data):   # ffmpeg đã lấy đủ đoạn cần cắt
                        for f in futs.values():
                            f.cancel()
                        self.on_progress(total, total, self.bytes_done)
                        break
                    done_sec += segs[i].duration
                    if i % 10 == 0 or i == total - 1:
                        self._check_space(done_sec, total_sec)
                    self.on_progress(i + 1, total, self.bytes_done)
            except BaseException:
                self._stop.set()
                for f in futs.values():
                    f.cancel()
                raise
        return total

    # ---------- LIVE ----------
    def _run_live(self, pl):
        seen = set()
        count = 0
        recorded = 0.0  # seconds of media written
        while not self._stop.is_set():
            new = [s for s in pl.segments if s.url.split("?")[0] not in seen]
            for seg in new:
                if self._stop.is_set():
                    break
                seen.add(seg.url.split("?")[0])
                try:
                    data = self._download_segment(seg)
                except DownloadCancelled:
                    break
                except Exception as e:
                    self.on_log(f"⚠ {e}")
                    continue
                if not self._feed(data):
                    self._stop.set()
                    break
                count += 1
                if count % 10 == 0:
                    self._space_checked = True
                    self._check_space(0, 0)
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
        return count


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
