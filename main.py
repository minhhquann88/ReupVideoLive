"""Nimo Tool - tải video live/VOD Nimo TV (m3u8) và chỉnh sửa video. PySide6 + ffmpeg."""
import json
import os
import sys
import time

from PySide6.QtCore import QObject, QProcess, Qt, QThread, QUrl, Signal
from PySide6.QtGui import QColor, QFont, QImage
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer, QVideoFrame, QVideoSink
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QColorDialog, QComboBox, QFileDialog, QFormLayout,
    QGroupBox, QHBoxLayout, QLabel, QLineEdit, QListWidget, QMainWindow, QMessageBox,
    QPlainTextEdit, QProgressBar, QPushButton, QSlider, QSpinBox, QSplitter, QTabWidget, QVBoxLayout,
    QWidget,
)

import compose_widgets as cw
import ffbin
import nimo_api
import video_editor as ve
from hls_downloader import HLSDownloader, fmt_time, link_expiry, parse_playlist, parse_time

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_OUT = os.path.join(os.path.expanduser("~"), "Videos", "Nimo")


def browse_save(parent, line_edit, filt="Video (*.mp4)"):
    path, _ = QFileDialog.getSaveFileName(parent, "Lưu thành", line_edit.text(), filt)
    if path:
        line_edit.setText(path)


def time_edit(placeholder="hh:mm:ss"):
    e = QLineEdit()
    e.setPlaceholderText(placeholder)
    e.setMaximumWidth(110)
    return e


# ============================================================ Download

class DownloadWorker(QObject):
    log = Signal(str)
    progress = Signal(int, int, int)
    info = Signal(object)
    finished = Signal(str)
    failed = Signal(str)

    def __init__(self, **kw):
        super().__init__()
        self.dl = HLSDownloader(on_log=self.log.emit, on_progress=self.progress.emit,
                                on_info=self.info.emit, **kw)

    def run(self):
        try:
            self.finished.emit(self.dl.run())
        except Exception as e:
            self.failed.emit(str(e) or e.__class__.__name__)


class AnalyzeWorker(QObject):
    done = Signal(object, str)

    def __init__(self, url):
        super().__init__()
        self.url = url

    def run(self):
        import requests
        try:
            self.done.emit(parse_playlist(requests.Session(), self.url), "")
        except Exception as e:
            self.done.emit(None, str(e))


class ResolveWorker(QObject):
    """URL video nimo.tv/v/v-... + kênh -> NimoVideo qua API videoListForAgg."""
    done = Signal(object, str)
    log = Signal(str)

    def __init__(self, video, channel, early_stop=True):
        super().__init__()
        self.video, self.channel, self.early_stop = video, channel, early_stop

    def run(self):
        try:
            room = nimo_api.resolve_room_id(self.channel)
            self.log.emit(f"Kênh → roomId {room}, đang tìm video {nimo_api.parse_video_id(self.video)}...")
            v = nimo_api.find_video(room, self.video, early_stop=self.early_stop,
                                    on_page=lambda p: self.log.emit(f"  ... trang {p}"))
            if v is None and self.early_stop:
                self.log.emit("Không thấy khi dừng sớm, duyệt hết các trang (video upload cũ?)...")
                v = nimo_api.find_video(room, self.video, early_stop=False,
                                        on_page=lambda p: self.log.emit(f"  ... trang {p}"))
            if v is None:
                self.done.emit(None, f"Không tìm thấy video trong room {room} (đã bị xoá hoặc sai kênh).")
            else:
                self.done.emit(v, "")
        except Exception as e:
            self.done.emit(None, str(e) or e.__class__.__name__)


class DownloadTab(QWidget):
    open_in_editor = Signal(str)

    def __init__(self):
        super().__init__()
        self.thread = None
        self.worker = None
        self.last_output = None
        self.analyzed = None

        lay = QVBoxLayout(self)
        g = QGroupBox("Link video Nimo (.m3u8 - live hoặc VOD)")
        gl = QVBoxLayout(g)
        self.url = QPlainTextEdit()
        self.url.setPlaceholderText("Dán link video Nimo (https://www.nimo.tv/v/v-...) rồi bấm 'Lấy link', "
                                    "hoặc dán thẳng link m3u8")
        self.url.setMaximumHeight(70)
        gl.addWidget(self.url)
        api_row = QHBoxLayout()
        api_row.addWidget(QLabel("Kênh:"))
        self.channel = QLineEdit("thaygiaoba")
        self.channel.setPlaceholderText("alias / URL kênh / roomId")
        self.channel.setMaximumWidth(220)
        api_row.addWidget(self.channel)
        api_row.addWidget(QLabel("Chất lượng:"))
        self.res = QComboBox()
        self.res.addItems(["1080", "720", "480"])
        api_row.addWidget(self.res)
        self.btn_resolve = QPushButton("🎬 Lấy link từ video Nimo")
        self.btn_resolve.clicked.connect(self.resolve)
        api_row.addWidget(self.btn_resolve)
        api_row.addStretch()
        gl.addLayout(api_row)
        row = QHBoxLayout()
        self.btn_analyze = QPushButton("🔍 Phân tích link")
        self.btn_analyze.clicked.connect(self.analyze)
        row.addWidget(self.btn_analyze)
        self.lbl_info = QLabel("Chưa phân tích")
        self.lbl_info.setWordWrap(True)
        row.addWidget(self.lbl_info, 1)
        gl.addLayout(row)
        lay.addWidget(g)

        g2 = QGroupBox("Tuỳ chọn tải")
        f = QFormLayout(g2)
        out_row = QHBoxLayout()
        self.out = QLineEdit(os.path.join(DEFAULT_OUT, time.strftime("nimo_%Y%m%d_%H%M%S.mp4")))
        b = QPushButton("...")
        b.setMaximumWidth(40)
        b.clicked.connect(lambda: browse_save(self, self.out))
        out_row.addWidget(self.out)
        out_row.addWidget(b)
        f.addRow("Lưu vào:", out_row)

        rng = QHBoxLayout()
        self.chk_range = QCheckBox("Chỉ tải đoạn")
        self.start = time_edit("bắt đầu")
        self.end = time_edit("kết thúc")
        for w in (self.start, self.end):
            w.setEnabled(False)
        self.chk_range.toggled.connect(lambda on: (self.start.setEnabled(on), self.end.setEnabled(on)))
        rng.addWidget(self.chk_range)
        rng.addWidget(QLabel("Từ"))
        rng.addWidget(self.start)
        rng.addWidget(QLabel("đến"))
        rng.addWidget(self.end)
        rng.addWidget(QLabel("<i>(LIVE: 'đến' = thời lượng ghi tối đa)</i>"))
        rng.addStretch()
        f.addRow("Khoảng:", rng)

        self.threads = QSpinBox()
        self.threads.setRange(1, 32)
        self.threads.setValue(8)
        f.addRow("Số luồng tải:", self.threads)
        lay.addWidget(g2)

        btns = QHBoxLayout()
        self.btn_start = QPushButton("⬇ Tải video")
        self.btn_start.setMinimumHeight(36)
        self.btn_start.clicked.connect(self.start_download)
        self.btn_stop = QPushButton("⏹ Dừng (lưu phần đã tải nếu là live)")
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self.stop_download)
        self.btn_edit = QPushButton("✂ Mở trong Editor")
        self.btn_edit.setEnabled(False)
        self.btn_edit.clicked.connect(lambda: self.open_in_editor.emit(self.last_output))
        self.btn_folder = QPushButton("📂 Mở thư mục")
        self.btn_folder.clicked.connect(self.open_folder)
        for w in (self.btn_start, self.btn_stop, self.btn_edit, self.btn_folder):
            btns.addWidget(w)
        lay.addLayout(btns)

        self.bar = QProgressBar()
        lay.addWidget(self.bar)
        self.lbl_speed = QLabel("")
        lay.addWidget(self.lbl_speed)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        lay.addWidget(self.log, 1)

    def _url(self):
        return "".join(self.url.toPlainText().split())

    def append_log(self, s):
        self.log.appendPlainText(f"[{time.strftime('%H:%M:%S')}] {s}")

    # ---- resolve nimo.tv/v/ URL via API ----
    def resolve(self):
        text = self._url()
        if not text:
            QMessageBox.warning(self, "Thiếu link", "Dán link video Nimo (nimo.tv/v/v-...) hoặc id video.")
            return
        try:
            nimo_api.parse_video_id(text)
        except ValueError as e:
            QMessageBox.warning(self, "Link không hợp lệ", str(e))
            return
        self.btn_resolve.setEnabled(False)
        self.lbl_info.setText("Đang tìm video qua API...")

        def wire(w):
            w.log.connect(self.append_log)
            w.done.connect(self._resolved)
        self._run_in_thread(ResolveWorker(text, self.channel.text().strip() or "thaygiaoba"), "_rs", wire)

    def _resolved(self, v, err):
        self.btn_resolve.setEnabled(True)
        if err:
            self.lbl_info.setText(f"<span style='color:#e55'>❌ {err}</span>")
            self.append_log("❌ " + err)
            return
        s = v.best_stream(int(self.res.currentText()))
        self.url.setPlainText(s.url)
        self.analyzed = None
        created = time.strftime("%d/%m/%Y %H:%M", time.localtime(v.created_at))
        self.append_log(f"✅ {v.title} · {created} · dài {fmt_time(v.duration_sec)} · "
                        f"có {', '.join(f'{x.res}P' for x in v.streams)} → chọn {s.res}P")
        self.lbl_info.setText(f"<b>{v.title}</b> · {created} · {fmt_time(v.duration_sec)} · {s.res}P")
        safe = "".join(c for c in v.title if c.isalnum() or c in " -_").strip().replace(" ", "_")[:40]
        name = f"nimo_{time.strftime('%Y%m%d', time.localtime(v.created_at))}_{safe or v.id}_{s.res}p.mp4"
        self.out.setText(os.path.join(os.path.dirname(self.out.text()) or DEFAULT_OUT, name))

    # ---- analyze ----
    def analyze(self):
        url = self._url()
        if not url:
            return
        if nimo_api.is_video_url(url):
            return self.resolve()
        exp = link_expiry(url)
        self.lbl_info.setText("Đang phân tích..." + (f" (token hết hạn lúc {exp:%H:%M:%S %d/%m})" if exp else ""))
        self.btn_analyze.setEnabled(False)
        self._run_in_thread(AnalyzeWorker(url), "_an", lambda w: w.done.connect(self._analyzed))

    def _analyzed(self, pl, err):
        self.btn_analyze.setEnabled(True)
        self.analyzed = pl  # reused by the download: the link's token only allows one playlist request
        if err:
            self.lbl_info.setText(f"<span style='color:#e55'>❌ {err}</span>")
            return
        self._show_info(pl)

    def _show_info(self, pl):
        exp = link_expiry(pl.url)
        kind = "🔴 LIVE" if pl.is_live else "📼 VOD"
        txt = f"<b>{kind}</b> · {len(pl.segments)} segment · dài {fmt_time(pl.total_duration)}"
        if pl.segments:
            import re
            m = re.search(r"resolution=(\d+x\d+)", pl.segments[0].url)
            if m:
                txt += f" · {m.group(1)}"
        if exp:
            txt += f" · token hết hạn {exp:%H:%M %d/%m}"
        self.lbl_info.setText(txt)

    # ---- download ----
    def start_download(self):
        url = self._url()
        if not url:
            QMessageBox.warning(self, "Thiếu link", "Hãy dán link m3u8.")
            return
        if nimo_api.is_video_url(url):
            QMessageBox.information(self, "Link trang video",
                                    "Đây là link trang video - bấm '🎬 Lấy link từ video Nimo' trước.")
            return
        out = self.out.text().strip()
        if not out.lower().endswith(".mp4"):
            out += ".mp4"
            self.out.setText(out)
        os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
        try:
            start = parse_time(self.start.text()) if self.chk_range.isChecked() else None
            end = parse_time(self.end.text()) if self.chk_range.isChecked() else None
        except ValueError:
            QMessageBox.warning(self, "Sai thời gian", "Định dạng thời gian: hh:mm:ss")
            return
        self.bar.setValue(0)
        self.bar.setMaximum(100)
        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.btn_edit.setEnabled(False)
        self._t0 = time.time()
        self.append_log("Bắt đầu tải...")
        pl = self.analyzed if self.analyzed is not None and self.analyzed.url == url else None
        if pl is not None and pl.is_live:
            pl = None  # live playlists must be refreshed anyway
        w = DownloadWorker(url=url, output=out, start=start, end=end, threads=self.threads.value(),
                           playlist=pl)

        def wire(w):
            w.log.connect(self.append_log)
            w.progress.connect(self._progress)
            w.info.connect(self._show_info)
            w.finished.connect(self._done)
            w.failed.connect(self._failed)
        self.worker = w
        self._run_in_thread(w, "_dl", wire)

    def _run_in_thread(self, worker, attr, wire):
        th = QThread(self)
        worker.moveToThread(th)
        wire(worker)
        th.started.connect(worker.run)
        for sig in ("finished", "failed", "done"):
            if hasattr(worker, sig):
                getattr(worker, sig).connect(th.quit)
        th.finished.connect(th.deleteLater)
        setattr(self, attr, (th, worker))  # keep references alive
        th.start()

    def _progress(self, done, total, nbytes):
        el = max(0.1, time.time() - self._t0)
        mb = nbytes / 1e6
        if total:
            self.bar.setMaximum(total)
            self.bar.setValue(done)
            eta = (el / max(done, 1)) * (total - done)
            self.lbl_speed.setText(f"{done}/{total} segment · {mb:.1f} MB · {mb / el:.2f} MB/s · còn ~{fmt_time(eta)}")
        else:  # live
            self.bar.setMaximum(0)
            self.lbl_speed.setText(f"🔴 Đang ghi live: {done} segment · {mb:.1f} MB · đã ghi {fmt_time(el)}")

    def stop_download(self):
        if self.worker:
            self.worker.dl.stop()
            self.append_log("Đang dừng...")
            self.btn_stop.setEnabled(False)

    def _reset_buttons(self):
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.bar.setMaximum(100)

    def _done(self, path):
        self._reset_buttons()
        self.bar.setValue(100)
        self.last_output = path
        self.btn_edit.setEnabled(True)
        size = os.path.getsize(path) / 1e6 if os.path.exists(path) else 0
        self.append_log(f"Kích thước: {size:.1f} MB")
        # new default name for next download
        self.out.setText(os.path.join(os.path.dirname(path), time.strftime("nimo_%Y%m%d_%H%M%S.mp4")))

    def _failed(self, msg):
        self._reset_buttons()
        self.append_log("❌ " + msg)
        if "DownloadCancelled" not in msg:
            QMessageBox.critical(self, "Lỗi tải", msg)

    def open_folder(self):
        d = os.path.dirname(os.path.abspath(self.out.text()))
        os.makedirs(d, exist_ok=True)
        os.startfile(d) if os.name == "nt" else None


# ============================================================ FFmpeg runner (shared)

class FFmpegRunner(QObject):
    """Runs an ffmpeg command via QProcess, reports progress from -progress pipe:1."""
    progress = Signal(int)  # percent
    log = Signal(str)
    finished = Signal(bool, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.proc = None
        self.duration = None
        self._cleanup = []

    def run(self, cmd, duration=None, cleanup=()):
        self.duration = duration
        self._cleanup = list(cleanup)
        self._err = []
        self.proc = QProcess(self)
        self.proc.readyReadStandardOutput.connect(self._out)
        self.proc.readyReadStandardError.connect(
            lambda: self._err.append(bytes(self.proc.readAllStandardError()).decode("utf-8", "replace")))
        self.proc.finished.connect(self._finished)
        self.log.emit("$ " + " ".join(f'"{c}"' if " " in c else c for c in cmd))
        self.proc.start(cmd[0], cmd[1:])

    def is_running(self):
        return self.proc is not None and self.proc.state() != QProcess.NotRunning

    def kill(self):
        if self.is_running():
            self.proc.kill()

    def _out(self):
        data = bytes(self.proc.readAllStandardOutput()).decode("utf-8", "replace")
        for line in data.splitlines():
            if line.startswith("out_time_us=") and self.duration:
                try:
                    t = int(line.split("=")[1]) / 1e6
                    self.progress.emit(min(99, int(t / self.duration * 100)))
                except ValueError:
                    pass

    def _finished(self, code, _status):
        for p in self._cleanup:
            try:
                os.remove(p)
            except OSError:
                pass
        ok = code == 0
        if ok:
            self.progress.emit(100)
        err = "".join(self._err)
        self.finished.emit(ok, "" if ok else err[-1500:])


# ============================================================ Editor

class FrameConverter(QObject):
    """Chạy ở luồng phụ: QVideoFrame -> QImage đã thu nhỏ, đúng định dạng để vẽ nhanh."""
    ready = Signal(QImage)
    MAX_W, MAX_H = 1280, 720

    def convert(self, frame):
        img = frame.toImage()
        if not img.isNull():
            if img.width() > self.MAX_W or img.height() > self.MAX_H:
                img = img.scaled(self.MAX_W, self.MAX_H, Qt.KeepAspectRatio, Qt.FastTransformation)
            img = img.convertToFormat(QImage.Format_ARGB32_Premultiplied)
        self.ready.emit(img)


class EditorTab(QWidget):
    """Chọn nhiều vùng cắt trên video (kéo thả) rồi ghép vào 1 video theo bố cục tự sắp xếp."""
    _conv_request = Signal(QVideoFrame)

    def __init__(self):
        super().__init__()
        self.info = None
        self.model = cw.RegionModel()
        self._syncing = False
        root = QVBoxLayout(self)

        in_row = QHBoxLayout()
        self.inp = QLineEdit()
        self.inp.setPlaceholderText("Chọn video cần chỉnh sửa...")
        self.inp.returnPressed.connect(lambda: self.load(self.inp.text()))
        b = QPushButton("📁 Mở video")
        b.clicked.connect(self._browse_in)
        in_row.addWidget(self.inp)
        in_row.addWidget(b)
        self.lbl_meta = QLabel("")
        in_row.addWidget(self.lbl_meta)
        root.addLayout(in_row)

        split = QSplitter(Qt.Horizontal)
        root.addWidget(split, 1)

        # ---- 1. video gốc: kéo để chọn vùng
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        hint = QLabel("<b>1. Video gốc</b> - kéo chuột để chọn vùng cắt · kéo góc để chỉnh · Delete để xoá")
        hint.setWordWrap(True)
        ll.addWidget(hint)
        self.frame = cw.FrameView(self.model)
        ll.addWidget(self.frame, 1)
        self.player = QMediaPlayer(self)
        self.audio = QAudioOutput(self)
        self.player.setAudioOutput(self.audio)
        self.sink = QVideoSink(self)
        self.player.setVideoOutput(self.sink)
        self.sink.videoFrameChanged.connect(self._frame)
        self._conv_busy = False
        self._conv_pending = None
        self._conv_thread = QThread(self)
        self._conv = FrameConverter()
        self._conv.moveToThread(self._conv_thread)
        self._conv_request.connect(self._conv.convert)
        self._conv.ready.connect(self._converted)
        self._conv_thread.start()
        self.player.positionChanged.connect(self._pos_changed)
        self.player.durationChanged.connect(lambda d: self.slider.setRange(0, d))
        self.player.mediaStatusChanged.connect(self._media_status)
        self.slider = QSlider(Qt.Horizontal)
        self.slider.sliderMoved.connect(self.player.setPosition)
        ll.addWidget(self.slider)
        ctl = QHBoxLayout()
        self.btn_play = QPushButton("▶")
        self.btn_play.setMaximumWidth(40)
        self.btn_play.clicked.connect(self._toggle_play)
        ctl.addWidget(self.btn_play)
        for label, ms in (("-5s", -5000), ("-1s", -1000), ("+1s", 1000), ("+5s", 5000)):
            bb = QPushButton(label)
            bb.setMaximumWidth(44)
            bb.clicked.connect(lambda _=False, m=ms: self.player.setPosition(max(0, self.player.position() + m)))
            ctl.addWidget(bb)
        self.lbl_pos = QLabel("00:00:00 / 00:00:00")
        ctl.addWidget(self.lbl_pos)
        ctl.addStretch()
        ll.addLayout(ctl)
        rng = QHBoxLayout()
        rng.addWidget(QLabel("Đoạn xuất: từ"))
        self.start = time_edit("đầu video")
        rng.addWidget(self.start)
        b_in = QPushButton("[ Đặt")
        b_in.setToolTip("Lấy vị trí đang xem làm điểm đầu")
        b_in.clicked.connect(lambda: self.start.setText(self._cur_time()))
        rng.addWidget(b_in)
        rng.addWidget(QLabel("đến"))
        self.end = time_edit("cuối video")
        rng.addWidget(self.end)
        b_out = QPushButton("Đặt ]")
        b_out.setToolTip("Lấy vị trí đang xem làm điểm cuối")
        b_out.clicked.connect(lambda: self.end.setText(self._cur_time()))
        rng.addWidget(b_out)
        rng.addStretch()
        ll.addLayout(rng)
        split.addWidget(left)

        # ---- 2. khung xuất: sắp xếp bố cục
        mid = QWidget()
        ml = QVBoxLayout(mid)
        ml.setContentsMargins(0, 0, 0, 0)
        hint = QLabel("<b>2. Bố cục video xuất</b> - kéo để di chuyển · kéo góc để phóng to/thu nhỏ")
        hint.setWordWrap(True)
        ml.addWidget(hint)
        self.canvas = cw.LayoutCanvas(self.model)
        ml.addWidget(self.canvas, 1)
        crow = QHBoxLayout()
        self.preset = QComboBox()
        self.preset.addItems(ve.CANVAS_PRESETS.keys())
        crow.addWidget(self.preset, 1)
        self.cw_ = QSpinBox()
        self.ch_ = QSpinBox()
        for sp in (self.cw_, self.ch_):
            sp.setRange(64, 4096)
            sp.setSingleStep(2)
        self.cw_.setValue(1080)
        self.ch_.setValue(1920)
        for sp in (self.cw_, self.ch_):
            sp.valueChanged.connect(self._canvas_size_changed)
        crow.addWidget(self.cw_)
        crow.addWidget(QLabel("x"))
        crow.addWidget(self.ch_)
        self.btn_bg = QPushButton("Màu nền")
        self.btn_bg.clicked.connect(self._pick_bg)
        crow.addWidget(self.btn_bg)
        ml.addLayout(crow)
        lrow = QHBoxLayout()
        lrow.addWidget(QLabel("Xếp nhanh:"))
        for text, mode in (("Dọc", "vertical"), ("Ngang", "horizontal"), ("Lưới", "grid")):
            bb = QPushButton(text)
            bb.clicked.connect(lambda _=False, m=mode: self.model.auto_layout(m))
            lrow.addWidget(bb)
        self.keep = QCheckBox("Giữ tỉ lệ")
        self.keep.setToolTip("Bỏ chọn để kéo giãn vùng tự do (méo hình)")
        self.keep.setChecked(True)
        self.keep.toggled.connect(lambda on: setattr(self.model, "keep_ratio", on))
        lrow.addWidget(self.keep)
        lrow.addStretch()
        ml.addLayout(lrow)
        split.addWidget(mid)

        # ---- 3. danh sách vùng + xuất
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.addWidget(QLabel("<b>3. Các vùng</b> (vùng sau nằm đè lên vùng trước)"))
        self.lst = QListWidget()
        self.lst.setMaximumHeight(130)
        self.lst.currentRowChanged.connect(lambda i: None if self._syncing else self.model.select(i))
        rl.addWidget(self.lst)
        brow = QHBoxLayout()
        for text, fn, tip in (("🗑", lambda: self.model.remove(), "Xoá vùng (Delete)"),
                              ("⬆", lambda: self.model.move_layer(-1), "Đưa xuống lớp dưới"),
                              ("⬇", lambda: self.model.move_layer(1), "Đưa lên lớp trên"),
                              ("⛶", lambda: self.model.auto_layout("fill"), "Phóng vừa khung"),
                              ("↔", lambda: self.model.auto_layout("center_h"), "Căn giữa ngang"),
                              ("↕", lambda: self.model.auto_layout("center_v"), "Căn giữa dọc")):
            bb = QPushButton(text)
            bb.setToolTip(tip)
            bb.setMaximumWidth(36)
            bb.clicked.connect(fn)
            brow.addWidget(bb)
        brow.addStretch()
        rl.addLayout(brow)

        g = QGroupBox("Vùng đang chọn (pixel)")
        gf = QFormLayout(g)
        self.spin_src, self.spin_dst = [], []
        for label, store in (("Cắt X, Y, R, C:", self.spin_src), ("Đặt X, Y, R, C:", self.spin_dst)):
            row = QHBoxLayout()
            for _ in range(4):
                sp = QSpinBox()
                sp.setRange(-8192, 8192)
                sp.valueChanged.connect(self._spin_changed)
                row.addWidget(sp)
                store.append(sp)
            gf.addRow(label, row)
        rl.addWidget(g)
        lay_row = QHBoxLayout()
        b_save = QPushButton("💾 Lưu bố cục")
        b_save.clicked.connect(self._save_layout)
        b_load = QPushButton("📂 Mở bố cục")
        b_load.clicked.connect(self._load_layout)
        b_clear = QPushButton("Xoá hết")
        b_clear.clicked.connect(lambda: self.model.clear())
        for w in (b_save, b_load, b_clear):
            lay_row.addWidget(w)
        rl.addLayout(lay_row)

        g = QGroupBox("Xuất video")
        gf = QFormLayout(g)
        out_row = QHBoxLayout()
        self.out = QLineEdit()
        bo = QPushButton("...")
        bo.setMaximumWidth(40)
        bo.clicked.connect(lambda: browse_save(self, self.out))
        out_row.addWidget(self.out)
        out_row.addWidget(bo)
        gf.addRow("Lưu vào:", out_row)
        self.out_fps = QComboBox()
        self.out_fps.addItems(["30 fps (nhanh, chuẩn TikTok/Shorts)", "Giữ nguyên như video gốc"])
        self.out_fps.setToolTip("Video gốc 60 fps xuất ở 30 fps nhanh hơn ~1,7 lần")
        gf.addRow("FPS xuất:", self.out_fps)
        rl.addWidget(g)
        row = QHBoxLayout()
        self.btn_export = QPushButton("🎬 Xuất video")
        self.btn_export.setMinimumHeight(36)
        self.btn_export.clicked.connect(self.export)
        self.btn_cancel = QPushButton("Huỷ")
        self.btn_cancel.setEnabled(False)
        self.btn_cancel.clicked.connect(lambda: self.runner.kill())
        row.addWidget(self.btn_export, 1)
        row.addWidget(self.btn_cancel)
        rl.addLayout(row)
        self.bar = QProgressBar()
        rl.addWidget(self.bar)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        rl.addWidget(self.log, 1)
        split.addWidget(right)
        split.setSizes([560, 380, 320])

        self.runner = FFmpegRunner(self)
        self.runner.progress.connect(self.bar.setValue)
        self.runner.log.connect(self.log.appendPlainText)
        self.runner.finished.connect(self._export_done)
        self.model.changed.connect(self._sync_ui)
        self.preset.currentTextChanged.connect(self._preset_changed)
        self._preset_changed(self.preset.currentText())
        self._set_bg(QColor("#000000"))
        self._sync_ui()

    # ---- video
    def _browse_in(self):
        p, _ = QFileDialog.getOpenFileName(self, "Chọn video", self.inp.text() or DEFAULT_OUT,
                                           "Video (*.mp4 *.ts *.mkv *.flv *.mov *.webm);;Tất cả (*.*)")
        if p:
            self.load(p)

    def load(self, path):
        if not path or not os.path.exists(path):
            return
        self.inp.setText(path)
        try:
            self.info = ve.probe(path)
            i = self.info
            self.model.set_source_size(i["width"], i["height"])
            self.lbl_meta.setText(f"{i['width']}x{i['height']} · {i['fps']:.0f} fps · dài {fmt_time(i['duration'])}"
                                  + ("" if i["has_audio"] else " · không có âm thanh"))
        except Exception as e:
            self.info = None
            self.lbl_meta.setText(f"Lỗi đọc video: {e}")
        self.start.clear()
        self.end.clear()
        base, _ = os.path.splitext(path)
        self.out.setText(base + "_bocuc.mp4")
        self.player.setSource(QUrl.fromLocalFile(path))
        self.player.pause()

    def _media_status(self, st):
        if st == QMediaPlayer.LoadedMedia:   # hiện 1 frame để chọn vùng ngay
            self.player.setPosition(min(1000, max(0, self.player.duration() // 2)))

    def _frame(self, f):
        """Frame mới từ player. Đổi sang ảnh ở luồng phụ; nếu luồng phụ đang bận thì chỉ giữ frame
        mới nhất (bỏ các frame ở giữa) -> giao diện không bị nghẽn với video 1080p60."""
        if not f.isValid():
            return
        if not self.info:
            self.model.set_source_size(f.width(), f.height())
        if self._conv_busy:
            self._conv_pending = f
            return
        self._conv_busy = True
        self._conv_request.emit(f)

    def _converted(self, img):
        self._conv_busy = False
        if not img.isNull():
            self.frame.set_image(img)
            self.canvas.set_image(img)
        if self._conv_pending is not None:
            f, self._conv_pending = self._conv_pending, None
            self._frame(f)

    def shutdown(self):
        self.player.stop()
        self.runner.kill()
        self._conv_thread.quit()
        self._conv_thread.wait(2000)

    def _toggle_play(self):
        if self.player.playbackState() == QMediaPlayer.PlayingState:
            self.player.pause()
            self.btn_play.setText("▶")
        else:
            self.player.play()
            self.btn_play.setText("⏸")

    def _pos_changed(self, ms):
        if not self.slider.isSliderDown():
            self.slider.setValue(ms)
        self.lbl_pos.setText(f"{fmt_time(ms / 1000)} / {fmt_time(self.player.duration() / 1000)}")

    def _cur_time(self):
        ms = self.player.position()
        return f"{fmt_time(ms / 1000)}.{ms % 1000 // 100}"

    # ---- khung xuất
    def _preset_changed(self, name):
        size = ve.CANVAS_PRESETS.get(name)
        self.cw_.setEnabled(size is None)
        self.ch_.setEnabled(size is None)
        if size:
            self._syncing = True
            self.cw_.setValue(size[0])
            self.ch_.setValue(size[1])
            self._syncing = False
            self.model.set_canvas(*size)

    def _canvas_size_changed(self):
        if not self._syncing:
            self.model.set_canvas(self.cw_.value() // 2 * 2, self.ch_.value() // 2 * 2)

    def _set_bg(self, c):
        self.canvas.bg = c
        self.btn_bg.setStyleSheet(f"background:{c.name()}; color:{'black' if c.lightness() > 128 else 'white'}")
        self.canvas.update()

    def _pick_bg(self):
        c = QColorDialog.getColor(self.canvas.bg, self, "Màu nền")
        if c.isValid():
            self._set_bg(c)

    # ---- đồng bộ danh sách / ô số
    def _sync_ui(self):
        self._syncing = True
        try:
            m = self.model
            names = [f"{r.name} · cắt {int(r.sw)}x{int(r.sh)} → {int(r.dw)}x{int(r.dh)}" for r in m.regions]
            if [self.lst.item(i).text() for i in range(self.lst.count())] != names:
                self.lst.clear()
                for i, n in enumerate(names):
                    self.lst.addItem(n)
                    self.lst.item(i).setForeground(cw.color_of(i))
            self.lst.setCurrentRow(m.sel)
            r = m.current()
            for sp, v in zip(self.spin_src + self.spin_dst,
                             (r.sx, r.sy, r.sw, r.sh, r.dx, r.dy, r.dw, r.dh) if r else (0,) * 8):
                sp.setEnabled(r is not None)
                sp.setValue(int(round(v)))
            self.btn_export.setEnabled(bool(m.regions) and not self.runner.is_running())
        finally:
            self._syncing = False

    def _spin_changed(self):
        r = self.model.current()
        if self._syncing or r is None:
            return
        keep = self.model.keep_ratio
        self.model.keep_ratio = False   # nhập số = dùng đúng số người dùng gõ
        try:
            self.model.set_src(r, *[sp.value() for sp in self.spin_src])
            self.model.set_dst(r, *[sp.value() for sp in self.spin_dst])
        finally:
            self.model.keep_ratio = keep

    def _save_layout(self):
        if not self.model.regions:
            return
        p, _ = QFileDialog.getSaveFileName(self, "Lưu bố cục", os.path.join(DEFAULT_OUT, "bocuc.json"),
                                           "Bố cục (*.json)")
        if p:
            d = self.model.to_json()
            d["bg"] = self.canvas.bg.name()
            with open(p, "w", encoding="utf-8") as f:
                json.dump(d, f, indent=1)
            self.log.appendPlainText(f"💾 Đã lưu bố cục: {p}")

    def _load_layout(self):
        p, _ = QFileDialog.getOpenFileName(self, "Mở bố cục", DEFAULT_OUT, "Bố cục (*.json)")
        if p:
            self.load_layout(p)

    def load_layout(self, p):
        try:
            with open(p, encoding="utf-8") as f:
                d = json.load(f)
            self.model.from_json(d)
            w, h = self.model.out_w, self.model.out_h
            name = next((k for k, v in ve.CANVAS_PRESETS.items() if v == (w, h)), "Tuỳ chỉnh")
            self._syncing = True
            self.preset.setCurrentText(name)
            self.cw_.setValue(w)
            self.ch_.setValue(h)
            self.cw_.setEnabled(name == "Tuỳ chỉnh")
            self.ch_.setEnabled(name == "Tuỳ chỉnh")
            self._syncing = False
            self._set_bg(QColor(d.get("bg", "#000000")))
        except Exception as e:
            self._syncing = False
            QMessageBox.warning(self, "Lỗi bố cục", str(e))

    # ---- xuất
    def export(self):
        if self.runner.is_running():
            return
        src = self.inp.text().strip()
        if not os.path.exists(src) or not self.info:
            QMessageBox.warning(self, "Thiếu video", "Hãy chọn video đầu vào.")
            return
        if not self.model.regions:
            QMessageBox.warning(self, "Chưa có vùng", "Kéo chuột trên video gốc để chọn ít nhất 1 vùng.")
            return
        out = self.out.text().strip()
        if not out.lower().endswith(".mp4"):
            out += ".mp4"
            self.out.setText(out)
        if os.path.abspath(out) == os.path.abspath(src):
            QMessageBox.warning(self, "Trùng file", "File xuất phải khác file gốc.")
            return
        i = self.info
        fps = min(30.0, i["fps"]) if self.out_fps.currentIndex() == 0 else i["fps"]
        try:
            cmd, dur = ve.build_compose_cmd(
                src, out, self.model.regions, i["width"], i["height"], self.model.out_w, self.model.out_h,
                bg=self.canvas.bg.name(), fps=fps, start=parse_time(self.start.text()),
                end=parse_time(self.end.text()), has_audio=i["has_audio"], src_duration=i["duration"])
        except ValueError as e:
            QMessageBox.warning(self, "Sai tham số", str(e))
            return
        os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
        self.player.pause()
        self.btn_play.setText("▶")
        self.bar.setValue(0)
        self.btn_export.setEnabled(False)
        self.btn_cancel.setEnabled(True)
        self.runner.run(cmd, dur)

    def _export_done(self, ok, err):
        self.btn_export.setEnabled(bool(self.model.regions))
        self.btn_cancel.setEnabled(False)
        if ok:
            self.log.appendPlainText(f"✅ Xong: {self.out.text()}")
        else:
            self.log.appendPlainText("❌ Lỗi:\n" + err)
            QMessageBox.critical(self, "Lỗi xuất video", err[-600:] or "ffmpeg bị dừng")


# ============================================================ Main window

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Nimo Tool - Tải & Chỉnh sửa video")
        self.resize(1400, 820)
        tabs = QTabWidget()
        self.dl = DownloadTab()
        self.ed = EditorTab()
        tabs.addTab(self.dl, "⬇ Tải video")
        tabs.addTab(self.ed, "✂ Chỉnh sửa")
        self.dl.open_in_editor.connect(lambda p: (self.ed.load(p), tabs.setCurrentWidget(self.ed)))
        self.setCentralWidget(tabs)
        self.tabs = tabs

    def closeEvent(self, e):
        if self.dl.worker:
            self.dl.worker.dl.stop()
        self.ed.shutdown()
        super().closeEvent(e)


def main():
    app = QApplication(sys.argv)
    app.setFont(QFont("Segoe UI", 10))
    if not ffbin.available():
        QMessageBox.critical(None, "Thiếu ffmpeg",
                             "Không tìm thấy ffmpeg trong thư mục bin/ của tool.\n"
                             "Chạy lệnh sau trong thư mục tool để tải về (không cài global):\n\n"
                             "python setup_ffmpeg.py")
        return 1
    w = MainWindow()
    w.show()
    if len(sys.argv) > 1:
        w.dl.url.setPlainText(sys.argv[1])
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
