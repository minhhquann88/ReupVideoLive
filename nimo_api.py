"""Lấy video phát lại Nimo TV qua API nội bộ videoListForAgg.

Cho URL/id video + kênh (alias, URL kênh hoặc roomId) -> metadata + link m3u8 theo độ phân giải.

    python nimo_api.py https://www.nimo.tv/v/v-1880869346605400724 -c thaygiaoba
    python nimo_api.py --list -c thaygiaoba --type replay --limit 10
"""
import json
import re
import time
import warnings
from dataclasses import asdict, dataclass, field

import requests

API = "https://video.nimo.tv/v1/liveVideo"
LANG = 1066          # tiếng Việt (trang mặc định 1033)
PAGE_SIZE = 100
PAGE_DELAY = 0.3     # nghỉ giữa các trang để không gọi dồn
LINK_TTL = 50 * 60   # cache link m3u8 tối đa ~50 phút (còn dư trước khi hết hạn)

# dotType quan sát được: 1 = phát lại buổi live, 4 = clip người khác cắt, 11 = video upload cũ,
# 15 = video sự kiện (Dạo Nimoshow), 18 = "wonderful moment" tự động
DOT_TYPES = {"replay": 1, "clip": 4, "upload": 11, "event": 15, "moment": 18}
DOT_NAMES = {v: k for k, v in DOT_TYPES.items()}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/130.0 Safari/537.36",
    "Origin": "https://www.nimo.tv",
    "Referer": "https://www.nimo.tv/",
}


class NimoApiError(Exception):
    """API trả code khác 200 hoặc dữ liệu không đúng cấu trúc - có thể Nimo đã đổi API."""


@dataclass
class Stream:
    res: int        # 480 / 720 / 1080
    url: str


@dataclass
class NimoVideo:
    id: str
    title: str
    is_replay: bool
    dot_type: int
    duration_sec: int
    created_at: float   # unix seconds
    live_id: object
    room_id: int
    author: str
    play_num: int
    cover: str
    page_url: str
    default_url: str
    streams: list = field(default_factory=list)
    fetched_at: float = 0.0
    link_ttl: float = LINK_TTL

    def best_stream(self, max_res=None):
        """Link có độ phân giải cao nhất (<= max_res nếu có)."""
        cands = [s for s in self.streams if max_res is None or s.res <= max_res] or self.streams
        if not cands:
            return Stream(0, self.default_url)
        return max(cands, key=lambda s: s.res)

    @property
    def expired(self):
        return time.time() - self.fetched_at > self.link_ttl

    def to_dict(self):
        return asdict(self)


# ------------------------------------------------------------------ parse input

def parse_video_id(text):
    """'https://www.nimo.tv/v/v-188...' | 'v-188...' | '188...' -> '188...' (chuỗi, id 19 chữ số)."""
    s = str(text).strip()
    m = re.search(r"v-(\d+)", s)
    if m:
        return m.group(1)
    if re.fullmatch(r"\d+", s):
        return s
    raise ValueError(f"URL/id video không hợp lệ: {text}")


def is_video_url(text):
    return bool(re.search(r"nimo\.tv/v/v-\d+", str(text)))


_room_cache = {}


def resolve_room_id(channel, session=None):
    """roomId (số) | alias 'thaygiaoba' | URL kênh -> roomId, đọc G_roomBaseInfo trên trang kênh."""
    s = str(channel).strip()
    if re.fullmatch(r"\d+", s):
        return int(s)
    m = re.search(r"nimo\.tv/(?:live/)?([\w.-]+)", s)
    alias = m.group(1) if m else s.strip("/")
    if alias in _room_cache:
        return _room_cache[alias]
    if m and alias.isdigit():          # nimo.tv/live/2272219
        return int(alias)
    sess = session or requests.Session()
    r = sess.get(f"https://www.nimo.tv/{alias}", headers=HEADERS, timeout=20)
    if r.status_code == 404:
        raise NimoApiError(f"Kênh '{alias}' không tồn tại")
    r.raise_for_status()
    m = re.search(r"G_roomBaseInfo\s*=\s*\{[^<]*?\"roomId\"\s*:\s*(\d+)", r.text) \
        or re.search(r"\"roomId\"\s*:\s*(\d+)", r.text)
    if not m or m.group(1) == "0":
        raise NimoApiError(f"Không tìm thấy roomId trên trang kênh '{alias}' (sai alias?)")
    _room_cache[alias] = int(m.group(1))
    return _room_cache[alias]


# ------------------------------------------------------------------ API

def get_page(room_id, page, size=PAGE_SIZE, session=None, lang=LANG):
    """Một trang danh sách video của room, mới nhất trước. Trả (list, pageControlView)."""
    sess = session or requests.Session()
    url = f"{API}/videoListForAgg-{page}-{size}/{lang}/{room_id}"
    # FormData: keyType=0 (body JSON thường). Chỉ gửi roomId để giữ thứ tự mới nhất trước.
    form = {"keyType": (None, "0"), "body": (None, json.dumps({"roomId": int(room_id)}))}
    r = sess.post(url, files=form, headers=HEADERS, timeout=20)
    if r.status_code == 403:
        raise NimoApiError("Nimo chặn request (HTTP 403)")
    r.raise_for_status()
    try:
        js = r.json()
    except ValueError:
        raise NimoApiError(f"Response không phải JSON: {r.text[:200]}")
    code = js.get("code")
    if code != 200:
        msg = js.get("message")
        hint = {10401: " - body sai (kiểm tra roomId / FormData)",
                407: " - bị chặn, có thể cần header/cookie khác"}.get(code, " - có thể Nimo đã đổi API "
                                                                       "(vd. bắt buộc keyType=2 mã hoá)")
        warnings.warn(f"Nimo API trả code {code}: {msg}")
        raise NimoApiError(f"Nimo API {code}: {msg}{hint}")
    try:
        res = js["data"]["result"]
        return res.get("liveVideoViewList") or [], res.get("pageControlView") or {}
    except (KeyError, TypeError):
        raise NimoApiError("Cấu trúc response đã thay đổi (thiếu data.result)")


def to_video(v):
    streams = [Stream(int(s.get("iresolution") or 0), s["videoUrl"])
               for s in (v.get("multiResolutionVideoUrl") or []) if s.get("videoUrl")]
    loops = [s.get("loopTime") for s in (v.get("multiResolutionVideoUrl") or []) if s.get("loopTime")]
    ttl = LINK_TTL
    if any("wsSecret=" in s.url for s in streams) and loops:   # link có ký: tôn trọng loopTime
        ttl = min(LINK_TTL, max(60, min(loops) - 60))
    return NimoVideo(
        id=str(v["id"]),
        title=v.get("title") or "",
        is_replay=v.get("dotType") == 1,
        dot_type=v.get("dotType"),
        duration_sec=v.get("playDuration") or 0,
        created_at=(v.get("createdTime") or 0) / 1000,
        live_id=v.get("liveId"),
        room_id=v.get("roomId"),
        author=v.get("author") or v.get("anchorName") or "",
        play_num=v.get("playNum") or 0,
        cover=v.get("showScreenshots") or "",
        page_url=f"https://www.nimo.tv/v/v-{v['id']}",
        default_url=v.get("videoUrl") or "",
        streams=sorted(streams, key=lambda s: s.res),
        fetched_at=time.time(),
        link_ttl=ttl,
    )


def iter_videos(room_id, session=None, size=PAGE_SIZE, delay=PAGE_DELAY):
    """Lật từng trang, yield (page, raw_list). Dừng khi quá pageCount hoặc trang rỗng."""
    sess = session or requests.Session()
    page, page_count = 1, 1
    while page <= page_count:
        lst, ctl = get_page(room_id, page, size, sess)
        page_count = int(ctl.get("pageCount") or 0)
        if not lst:
            return
        yield page, lst
        page += 1
        if page <= page_count and delay:
            time.sleep(delay)


_video_cache = {}  # id -> NimoVideo
EARLY_STOP_MARGIN = 24 * 3600  # giây


def id_time_ms(video_id):
    """Thời điểm tạo (ms) suy từ id: id = createdTime_ms << 20 (đúng với phát lại / clip)."""
    return int(video_id) >> 20


def find_video(room_id, video, early_stop=True, session=None, use_cache=True, on_page=None):
    """Tìm video theo id trong danh sách của room. Trả NimoVideo hoặc None.

    early_stop: danh sách xếp theo createdTime giảm dần (KHÔNG phải theo id). Với video phát lại /
    clip, id = createdTime_ms << 20, nên khi video cuối trang đã tạo trước thời điểm suy từ id
    (trừ biên EARLY_STOP_MARGIN) thì video không có trong room. Video upload cũ (dotType 11) có id
    lệch tới ~200 ngày so với createdTime -> có thể bị dừng sớm nhầm; khi đó dùng early_stop=False.
    """
    target_id = parse_video_id(video)
    cached = _video_cache.get(target_id)
    if use_cache and cached and not cached.expired:
        return cached
    stop_before_ms = id_time_ms(target_id) - EARLY_STOP_MARGIN * 1000
    for page, lst in iter_videos(room_id, session):
        if on_page:
            on_page(page)
        for v in lst:
            if str(v.get("id")) == target_id:
                _video_cache[target_id] = found = to_video(v)
                return found
        if early_stop and (lst[-1].get("createdTime") or 0) < stop_before_ms:
            return None
    return None


def list_videos(room_id, dot_type=None, limit=None, session=None):
    """Danh sách video của room (mới nhất trước), lọc theo dotType nếu có."""
    out = []
    for _page, lst in iter_videos(room_id, session):
        for v in lst:
            if dot_type is None or v.get("dotType") == dot_type:
                out.append(to_video(v))
                if limit and len(out) >= limit:
                    return out
    return out


# ------------------------------------------------------------------ CLI

def _fmt_dur(sec):
    sec = int(sec)
    return f"{sec // 3600:d}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


def _print_video(v, full=True):
    created = time.strftime("%d/%m/%Y %H:%M", time.localtime(v.created_at))
    kind = DOT_NAMES.get(v.dot_type, f"dotType {v.dot_type}")
    print(f"{v.id}  {created}  {_fmt_dur(v.duration_sec)}  [{kind}]  {v.title}  ({v.play_num} view)")
    if full:
        print(f"  trang: {v.page_url}\n  liveId: {v.live_id}  roomId: {v.room_id}  tác giả: {v.author}")
        for s in v.streams:
            print(f"  {s.res}P: {s.url}")


def main():
    import argparse
    import sys
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="Lấy video phát lại Nimo TV qua API")
    ap.add_argument("video", nargs="?", help="URL video (nimo.tv/v/v-...) hoặc id")
    ap.add_argument("-c", "--channel", required=True, help="roomId, alias (thaygiaoba) hoặc URL kênh")
    ap.add_argument("--list", action="store_true", help="liệt kê video của kênh")
    ap.add_argument("--type", choices=list(DOT_TYPES), help="lọc khi liệt kê")
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--no-early-stop", action="store_true", help="duyệt hết các trang")
    ap.add_argument("--json", action="store_true", help="in JSON")
    ap.add_argument("--res", type=int, help="độ phân giải tối đa khi tải (480/720/1080)")
    ap.add_argument("-o", "--download", metavar="FILE.mp4", help="tải luôn video về file")
    ap.add_argument("--start", help="chỉ tải từ hh:mm:ss")
    ap.add_argument("--end", help="chỉ tải đến hh:mm:ss")
    a = ap.parse_args()

    room = resolve_room_id(a.channel)
    if a.list:
        vids = list_videos(room, DOT_TYPES.get(a.type), a.limit)
        if a.json:
            print(json.dumps([v.to_dict() for v in vids], ensure_ascii=False, indent=2))
        else:
            for v in vids:
                _print_video(v, full=False)
        return
    if not a.video:
        ap.error("cần URL/id video (hoặc --list)")
    v = find_video(room, a.video, early_stop=not a.no_early_stop,
                   on_page=lambda p: print(f"... trang {p}", file=sys.stderr))
    if v is None:
        print(f"Không tìm thấy video {parse_video_id(a.video)} trong room {room} "
              "(đã bị xoá hoặc sai kênh)."
              + ("" if a.no_early_stop else " Video upload cũ? Thử thêm --no-early-stop."), file=sys.stderr)
        sys.exit(1)
    if a.json:
        print(json.dumps(v.to_dict(), ensure_ascii=False, indent=2))
    else:
        _print_video(v)
    if a.download:
        from hls_downloader import HLSDownloader, parse_time
        s = v.best_stream(a.res)
        print(f"Tải {s.res}P -> {a.download}", file=sys.stderr)
        HLSDownloader(s.url, a.download, start=parse_time(a.start), end=parse_time(a.end),
                      on_log=lambda m: print(m, file=sys.stderr)).run()


if __name__ == "__main__":
    main()
