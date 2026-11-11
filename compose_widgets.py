"""Widget cho tab Chỉnh sửa: kéo thả chọn vùng cắt trên video và sắp xếp chúng trên khung xuất."""
import math

from PySide6.QtCore import QObject, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QWidget

from video_editor import Region

COLORS = ["#ff5252", "#40c4ff", "#69f0ae", "#ffd740", "#e040fb", "#ff9100", "#18ffff", "#b2ff59"]
HANDLE = 7       # nửa cạnh ô nắm góc (px màn hình)
MIN_SRC = 16     # kích thước vùng cắt tối thiểu (px video)
MIN_DST = 16


def color_of(i):
    return QColor(COLORS[i % len(COLORS)])


class RegionModel(QObject):
    """Danh sách vùng cắt dùng chung cho 2 widget. Thứ tự = lớp (vùng sau nằm trên)."""
    changed = Signal()
    selection_changed = Signal(int)

    def __init__(self):
        super().__init__()
        self.regions = []
        self.sel = -1
        self.src_w, self.src_h = 1920, 1080
        self.out_w, self.out_h = 1080, 1920
        self.keep_ratio = True

    # ---- helpers
    def current(self):
        return self.regions[self.sel] if 0 <= self.sel < len(self.regions) else None

    def select(self, i):
        i = i if 0 <= i < len(self.regions) else -1
        if i != self.sel:
            self.sel = i
            self.selection_changed.emit(i)
            self.changed.emit()

    def _renumber(self):
        for i, r in enumerate(self.regions):
            r.name = f"Vùng {i + 1}"

    # ---- edit
    def add(self, sx, sy, sw, sh):
        r = Region(sx, sy, sw, sh)
        # đặt mặc định: rộng hết khung, nối tiếp dưới vùng trước, giữ tỉ lệ
        dw = self.out_w
        dh = dw * sh / sw
        if dh > self.out_h:
            dh = self.out_h
            dw = dh * sw / sh
        y = max((p.dy + p.dh for p in self.regions), default=0)
        if y + dh > self.out_h:
            y = max(0, self.out_h - dh)
        r.dx, r.dy, r.dw, r.dh = (self.out_w - dw) / 2, y, dw, dh
        self.regions.append(r)
        self._renumber()
        self.sel = len(self.regions) - 1
        self.selection_changed.emit(self.sel)
        self.changed.emit()

    def remove(self, i=None):
        i = self.sel if i is None else i
        if 0 <= i < len(self.regions):
            del self.regions[i]
            self._renumber()
            self.sel = min(i, len(self.regions) - 1)
            self.selection_changed.emit(self.sel)
            self.changed.emit()

    def clear(self):
        self.regions.clear()
        self.sel = -1
        self.selection_changed.emit(-1)
        self.changed.emit()

    def move_layer(self, d):
        i, j = self.sel, self.sel + d
        if 0 <= i < len(self.regions) and 0 <= j < len(self.regions):
            self.regions[i], self.regions[j] = self.regions[j], self.regions[i]
            self._renumber()
            self.sel = j
            self.selection_changed.emit(j)
            self.changed.emit()

    def set_src(self, r, sx, sy, sw, sh):
        sw = max(MIN_SRC, min(sw, self.src_w))
        sh = max(MIN_SRC, min(sh, self.src_h))
        sx = min(max(0, sx), self.src_w - sw)
        sy = min(max(0, sy), self.src_h - sh)
        r.sx, r.sy, r.sw, r.sh = sx, sy, sw, sh
        if self.keep_ratio and r.dw > 0:   # giữ chiều rộng trên khung, đổi chiều cao theo tỉ lệ mới
            r.dh = r.dw * sh / sw
        self.changed.emit()

    def set_dst(self, r, dx, dy, dw, dh):
        r.dx, r.dy, r.dw, r.dh = dx, dy, max(MIN_DST, dw), max(MIN_DST, dh)
        self.changed.emit()

    def set_canvas(self, w, h):
        """Đổi kích thước khung xuất, co giãn bố cục theo."""
        if (w, h) == (self.out_w, self.out_h):
            return
        fx, fy = w / self.out_w, h / self.out_h
        f = min(fx, fy)
        for r in self.regions:
            cx, cy = (r.dx + r.dw / 2) * fx, (r.dy + r.dh / 2) * fy
            r.dw, r.dh = r.dw * f, r.dh * f
            r.dx, r.dy = cx - r.dw / 2, cy - r.dh / 2
        self.out_w, self.out_h = w, h
        self.changed.emit()

    def set_source_size(self, w, h):
        if w and h and (w, h) != (self.src_w, self.src_h):
            fx, fy = w / self.src_w, h / self.src_h
            for r in self.regions:
                r.sx, r.sw, r.sy, r.sh = r.sx * fx, r.sw * fx, r.sy * fy, r.sh * fy
            self.src_w, self.src_h = w, h
            self.changed.emit()

    # ---- bố cục tự động
    def auto_layout(self, mode):
        rs = self.regions
        if not rs:
            return
        W, H = self.out_w, self.out_h
        if mode == "fill" and self.current():
            r = self.current()
            r.dx, r.dy, r.dw, r.dh = 0, 0, W, H
            if self.keep_ratio:   # vừa khít trong khung, căn giữa
                f = min(W / r.sw, H / r.sh)
                r.dw, r.dh = r.sw * f, r.sh * f
                r.dx, r.dy = (W - r.dw) / 2, (H - r.dh) / 2
        elif mode == "vertical":
            hs = [W * r.sh / r.sw for r in rs]
            f = min(1.0, H / sum(hs))
            y = (H - sum(hs) * f) / 2
            for r, h in zip(rs, hs):
                r.dw, r.dh = W * f, h * f
                r.dx, r.dy = (W - r.dw) / 2, y
                y += r.dh
        elif mode == "horizontal":
            ws = [H * r.sw / r.sh for r in rs]
            f = min(1.0, W / sum(ws))
            x = (W - sum(ws) * f) / 2
            for r, w in zip(rs, ws):
                r.dw, r.dh = w * f, H * f
                r.dx, r.dy = x, (H - r.dh) / 2
                x += r.dw
        elif mode == "grid":
            n = len(rs)
            cols = math.ceil(math.sqrt(n * W / H)) if W >= H else math.ceil(math.sqrt(n))
            cols = max(1, min(n, cols))
            rows = math.ceil(n / cols)
            cw, ch = W / cols, H / rows
            for i, r in enumerate(rs):
                f = min(cw / r.sw, ch / r.sh)
                r.dw, r.dh = r.sw * f, r.sh * f
                r.dx = (i % cols) * cw + (cw - r.dw) / 2
                r.dy = (i // cols) * ch + (ch - r.dh) / 2
        elif mode == "center_h" and self.current():
            self.current().dx = (W - self.current().dw) / 2
        elif mode == "center_v" and self.current():
            self.current().dy = (H - self.current().dh) / 2
        self.changed.emit()

    # ---- lưu / mở bố cục
    def to_json(self):
        return {"src": [self.src_w, self.src_h], "out": [self.out_w, self.out_h],
                "regions": [[r.sx, r.sy, r.sw, r.sh, r.dx, r.dy, r.dw, r.dh] for r in self.regions]}

    def from_json(self, d):
        sw0, sh0 = d.get("src", [self.src_w, self.src_h])
        fx, fy = self.src_w / sw0, self.src_h / sh0
        self.out_w, self.out_h = d.get("out", [self.out_w, self.out_h])
        self.regions = [Region(a * fx, b * fy, c * fx, e * fy, g, h, i, j)
                        for a, b, c, e, g, h, i, j in d.get("regions", [])]
        self._renumber()
        self.sel = 0 if self.regions else -1
        self.selection_changed.emit(self.sel)
        self.changed.emit()


def _corners(rect):
    return [rect.topLeft(), rect.topRight(), rect.bottomLeft(), rect.bottomRight()]


class _RectEditor(QWidget):
    """Phần chung: vẽ nền, map toạ độ, kéo để di chuyển / kéo góc để đổi kích thước."""

    def __init__(self, model, parent=None):
        super().__init__(parent)
        self.m = model
        self.image = None
        self._drag = None   # (mode, index, corner, start_pt(model), orig_rect)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setMinimumSize(240, 200)
        model.changed.connect(self.update)

    # các lớp con định nghĩa: space_size(), rect_of(r) -> QRectF (toạ độ không gian), apply(r, QRectF)
    def view_rect(self):
        W, H = self.space_size()
        s = min((self.width() - 8) / W, (self.height() - 8) / H)
        w, h = W * s, H * s
        return QRectF((self.width() - w) / 2, (self.height() - h) / 2, w, h), s

    def to_view(self, rect):
        vr, s = self.view_rect()
        return QRectF(vr.x() + rect.x() * s, vr.y() + rect.y() * s, rect.width() * s, rect.height() * s)

    def to_space(self, pt):
        vr, s = self.view_rect()
        return QPointF((pt.x() - vr.x()) / s, (pt.y() - vr.y()) / s)

    def hit(self, pos):
        """-> (index, corner) ; corner = 0..3 hoặc -1 (bên trong) ; (-1,-1) nếu trượt."""
        order = list(range(len(self.m.regions)))[::-1]   # lớp trên trước
        if self.m.sel in order:
            order.remove(self.m.sel)
            order.insert(0, self.m.sel)
        for i in order:
            vr = self.to_view(self.rect_of(self.m.regions[i]))
            for c, p in enumerate(_corners(vr)):
                if abs(p.x() - pos.x()) <= HANDLE + 2 and abs(p.y() - pos.y()) <= HANDLE + 2:
                    return i, c
        for i in order:
            if self.to_view(self.rect_of(self.m.regions[i])).contains(pos):
                return i, -1
        return -1, -1

    def mousePressEvent(self, e):
        if e.button() != Qt.LeftButton:
            return
        pos = e.position()
        i, c = self.hit(pos)
        if i >= 0:
            self.m.select(i)
            self._drag = ("resize" if c >= 0 else "move", i, c, self.to_space(pos),
                          QRectF(self.rect_of(self.m.regions[i])))
        else:
            self._drag = self.press_empty(pos)

    def press_empty(self, pos):
        self.m.select(-1)
        return None

    def mouseMoveEvent(self, e):
        pos = e.position()
        if not self._drag:
            i, c = self.hit(pos)
            self.setCursor(Qt.SizeFDiagCursor if c in (0, 3) else Qt.SizeBDiagCursor if c in (1, 2)
                           else Qt.SizeAllCursor if i >= 0 else self.empty_cursor())
            return
        mode, i, c, p0, orig = self._drag
        p = self.to_space(pos)
        dx, dy = p.x() - p0.x(), p.y() - p0.y()
        if mode == "move":
            rect = orig.translated(dx, dy)
            rect = self.snap_move(rect, i)
        elif mode == "resize":
            rect = self.resize_rect(orig, c, p, i)
        else:
            return self.drag_other(p)
        self.apply(self.m.regions[i], rect)

    def resize_rect(self, orig, c, p, i):
        # góc đối diện cố định
        fixed = _corners(orig)[3 - c]
        x0, x1 = sorted((fixed.x(), p.x()))
        y0, y1 = sorted((fixed.y(), p.y()))
        return QRectF(x0, y0, x1 - x0, y1 - y0)

    def snap_move(self, rect, i):
        return rect

    def empty_cursor(self):
        return Qt.ArrowCursor

    def drag_other(self, p):
        pass

    def mouseReleaseEvent(self, e):
        self._drag = None

    def keyPressEvent(self, e):
        r = self.m.current()
        if e.key() in (Qt.Key_Delete, Qt.Key_Backspace):
            self.m.remove()
        elif r and e.key() in (Qt.Key_Left, Qt.Key_Right, Qt.Key_Up, Qt.Key_Down):
            step = 10 if e.modifiers() & Qt.ShiftModifier else 1
            rect = QRectF(self.rect_of(r))
            rect.translate({Qt.Key_Left: -step, Qt.Key_Right: step}.get(e.key(), 0),
                           {Qt.Key_Up: -step, Qt.Key_Down: step}.get(e.key(), 0))
            self.apply(r, rect)
        else:
            super().keyPressEvent(e)

    def draw_regions(self, p, label=True):
        f = QFont(self.font())
        f.setBold(True)
        p.setFont(f)
        for i, r in enumerate(self.m.regions):
            vr = self.to_view(self.rect_of(r))
            col = color_of(i)
            sel = i == self.m.sel
            p.setPen(QPen(col, 3 if sel else 2, Qt.SolidLine if sel else Qt.DashLine))
            p.setBrush(Qt.NoBrush)
            p.drawRect(vr)
            if label:
                p.fillRect(QRectF(vr.x(), vr.y(), 62, 20), col)
                p.setPen(Qt.black)
                p.drawText(QRectF(vr.x(), vr.y(), 62, 20), Qt.AlignCenter, r.name)
            if sel:
                p.setPen(QPen(Qt.black, 1))
                p.setBrush(col)
                for pt in _corners(vr):
                    p.drawRect(QRectF(pt.x() - HANDLE, pt.y() - HANDLE, 2 * HANDLE, 2 * HANDLE))


class FrameView(_RectEditor):
    """Hiển thị frame video; kéo trên vùng trống để tạo vùng cắt mới."""

    def space_size(self):
        return self.m.src_w, self.m.src_h

    def rect_of(self, r):
        return QRectF(r.sx, r.sy, r.sw, r.sh)

    def apply(self, r, rect):
        self.m.set_src(r, rect.x(), rect.y(), rect.width(), rect.height())

    def set_image(self, img):
        self.image = img
        self.update()

    def empty_cursor(self):
        return Qt.CrossCursor

    def press_empty(self, pos):
        self.m.select(-1)
        p = self.to_space(pos)
        p = QPointF(min(max(0, p.x()), self.m.src_w), min(max(0, p.y()), self.m.src_h))
        return ("new", -1, -1, p, None)

    def drag_other(self, p):
        self._new_end = QPointF(min(max(0, p.x()), self.m.src_w), min(max(0, p.y()), self.m.src_h))
        self.update()

    def mouseReleaseEvent(self, e):
        d = self._drag
        self._drag = None
        end = getattr(self, "_new_end", None)
        self._new_end = None
        if d and d[0] == "new" and end is not None:
            p0 = d[3]
            x0, x1 = sorted((p0.x(), end.x()))
            y0, y1 = sorted((p0.y(), end.y()))
            if x1 - x0 >= MIN_SRC and y1 - y0 >= MIN_SRC:
                self.m.add(x0, y0, x1 - x0, y1 - y0)
        self.update()

    def resize_rect(self, orig, c, p, i):
        p = QPointF(min(max(0, p.x()), self.m.src_w), min(max(0, p.y()), self.m.src_h))
        return super().resize_rect(orig, c, p, i)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(self.rect(), QColor("#111"))
        vr, _s = self.view_rect()
        if self.image is not None and not self.image.isNull():
            p.drawImage(vr, self.image)
        else:
            p.fillRect(vr, QColor("#222"))
            p.setPen(QColor("#888"))
            p.drawText(vr, Qt.AlignCenter, "Mở video, rồi kéo chuột trên hình để chọn vùng cắt")
        # làm tối phần ngoài vùng chọn
        if self.m.regions:
            p.setBrush(QColor(0, 0, 0, 90))
            p.setPen(Qt.NoPen)
            p.drawRect(vr)
            if self.image is not None and not self.image.isNull():
                sx = self.image.width() / self.m.src_w
                sy = self.image.height() / self.m.src_h
                for r in self.m.regions:
                    p.drawImage(self.to_view(self.rect_of(r)), self.image,
                                QRectF(r.sx * sx, r.sy * sy, r.sw * sx, r.sh * sy))
        self.draw_regions(p)
        end = getattr(self, "_new_end", None)
        if self._drag and self._drag[0] == "new" and end is not None:
            p0 = self._drag[3]
            rect = QRectF(p0, end).normalized()
            p.setPen(QPen(QColor("white"), 1, Qt.DashLine))
            p.setBrush(QColor(255, 255, 255, 40))
            p.drawRect(self.to_view(rect))
            p.setPen(QColor("white"))
            vr2 = self.to_view(rect)
            p.drawText(vr2.bottomRight() + QPointF(-80, 16), f"{int(rect.width())}x{int(rect.height())}")


class LayoutCanvas(_RectEditor):
    """Khung xuất: kéo để di chuyển, kéo góc để phóng to/thu nhỏ từng vùng."""
    SNAP = 12  # px màn hình

    def __init__(self, model, parent=None):
        super().__init__(model, parent)
        self.bg = QColor("#000000")

    def space_size(self):
        return self.m.out_w, self.m.out_h

    def rect_of(self, r):
        return QRectF(r.dx, r.dy, r.dw, r.dh)

    def apply(self, r, rect):
        self.m.set_dst(r, rect.x(), rect.y(), rect.width(), rect.height())

    def set_image(self, img):
        self.image = img
        self.update()

    def snap_move(self, rect, i):
        _vr, s = self.view_rect()
        tol = self.SNAP / s
        W, H = self.m.out_w, self.m.out_h
        xs = [0, W / 2, W] + [v for j, o in enumerate(self.m.regions) if j != i for v in (o.dx, o.dx + o.dw)]
        ys = [0, H / 2, H] + [v for j, o in enumerate(self.m.regions) if j != i for v in (o.dy, o.dy + o.dh)]
        dx = self._best(xs, [rect.left(), rect.center().x(), rect.right()], tol)
        dy = self._best(ys, [rect.top(), rect.center().y(), rect.bottom()], tol)
        return rect.translated(dx, dy)

    @staticmethod
    def _best(targets, edges, tol):
        best = None
        for t in targets:
            for e in edges:
                d = t - e
                if abs(d) <= tol and (best is None or abs(d) < abs(best)):
                    best = d
        return best or 0

    def resize_rect(self, orig, c, p, i):
        r = self.m.regions[i]
        if not self.m.keep_ratio:
            return super().resize_rect(orig, c, p, i)
        fixed = _corners(orig)[3 - c]
        ratio = r.sw / r.sh
        w = max(MIN_DST, abs(p.x() - fixed.x()), abs(p.y() - fixed.y()) * ratio)
        h = w / ratio
        x = fixed.x() - w if p.x() < fixed.x() else fixed.x()
        y = fixed.y() - h if p.y() < fixed.y() else fixed.y()
        return QRectF(x, y, w, h)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        p.fillRect(self.rect(), QColor("#2b2b2b"))
        vr, _s = self.view_rect()
        p.fillRect(vr, self.bg)
        p.save()
        p.setClipRect(vr)
        for i, r in enumerate(self.m.regions):
            target = self.to_view(self.rect_of(r))
            if self.image is not None and not self.image.isNull():
                sx = self.image.width() / self.m.src_w
                sy = self.image.height() / self.m.src_h
                p.drawImage(target, self.image, QRectF(r.sx * sx, r.sy * sy, r.sw * sx, r.sh * sy))
            else:
                c = color_of(i)
                c.setAlpha(80)
                p.fillRect(target, c)
        p.restore()
        p.setPen(QPen(QColor("#888"), 1))
        p.setBrush(Qt.NoBrush)
        p.drawRect(vr)
        self.draw_regions(p)
        p.setPen(QColor("#aaa"))
        p.drawText(QRectF(vr.x(), vr.bottom() - 18, vr.width() - 4, 18), Qt.AlignRight | Qt.AlignVCenter,
                   f"{self.m.out_w}x{self.m.out_h}")
