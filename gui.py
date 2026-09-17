from PySide6.QtWidgets import (QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
                               QPushButton, QLabel, QSlider, QSpinBox, QDoubleSpinBox,
                               QFileDialog, QColorDialog, QSplitter, QFrame, QGridLayout,
                               QCheckBox, QScrollArea, QLineEdit, QMessageBox, QGroupBox,
                               QSizePolicy, QTabWidget, QStackedWidget, QDialog, QComboBox,
                               QFormLayout, QDialogButtonBox, QAbstractSpinBox, QToolButton, QMenu)
from PySide6.QtCore import Qt, QTimer, Signal, QSize, QRect, QRectF, QPoint, QPointF, QEvent
from PySide6.QtGui import (QImage, QPixmap, QPainter, QColor, QPen, QBrush, QMouseEvent,
                           QWheelEvent, QFont, QLinearGradient, QPolygonF, QKeySequence,
                           QShortcut)
import cv2
import numpy as np
import av
from backend import VideoLoader, ImageProcessor, AtlasBuilder, write_image
import theme
import os
import json
import re
import time

VIDEO_EXTENSIONS = (".mp4", ".avi", ".mov", ".mkv", ".webm")
IMAGE_EXTENSIONS = (".png",)

def natural_path_key(path):
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", os.path.basename(path))]


class UndoStack:
    """Простой undo/redo на снимках состояния.

    Снимок — любое сравнимое значение (кортеж). В стек кладём состояние
    ДО правки; undo отдаёт его обратно, а текущее уходит в redo.
    """

    def __init__(self, limit=200):
        self.limit = limit
        self._undo = []
        self._redo = []

    def push(self, state):
        if self._undo and self._undo[-1] == state:
            return
        self._undo.append(state)
        if len(self._undo) > self.limit:
            del self._undo[0]
        self._redo.clear()

    def undo(self, current):
        # Пропускаем снимки, совпадающие с текущим — иначе Ctrl+Z «ничего не делает»
        while self._undo:
            state = self._undo.pop()
            if state != current:
                self._redo.append(current)
                return state
        return None

    def redo(self, current):
        while self._redo:
            state = self._redo.pop()
            if state != current:
                self._undo.append(current)
                return state
        return None

    def clear(self):
        self._undo.clear()
        self._redo.clear()


class ColorButton(QPushButton):
    colorChanged = Signal(object) # (r,g,b)

    def __init__(self, color=(128, 128, 128), text="Color"):
        super().__init__(text)
        self.setObjectName("colorButton")
        self.color = color
        self.clicked.connect(self.pick_color)
        self.update_style()

    def update_style(self):
        r, g, b = self.color
        # Calculate contrasting text color
        brightness = (r * 299 + g * 587 + b * 114) / 1000
        text_color = "black" if brightness > 128 else "white"
        
        self.setStyleSheet(f"""
            #colorButton {{
                background-color: rgb({r}, {g}, {b});
                color: {text_color};
                border: 1px solid {theme.BORDER};
                border-radius: 3px;
                padding: 5px 8px;
                text-align: left;
            }}
            #colorButton:hover {{
                border: 1px solid {theme.ACCENT_HOVER};
            }}
            #colorButton:disabled {{
                color: {theme.TEXT_DISABLED};
                border: 1px solid #2b2b2b;
            }}
        """)

    def set_color(self, color):
        self.color = color
        self.update_style()
        self.colorChanged.emit(color)

    def pick_color(self):
        c = QColorDialog.getColor(QColor(*self.color), self, "Select Color")
        if c.isValid():
            self.set_color((c.red(), c.green(), c.blue()))

class AtlasViewer(QWidget):
    cellClicked = Signal(int, int)  # клик по ячейке атласа: (индекс ячейки, кадр в ней)

    CLICK_SLOP = 4                  # сдвиг мыши в пикселях, после которого клик становится панорамой

    def __init__(self, placeholder_text="Atlas Preview"):
        super().__init__()
        self.setMinimumSize(400, 400)
        self.setStyleSheet(f"background-color: {theme.VIEWPORT_BG_ATLAS};")
        self.placeholder_text = placeholder_text
        self.pixmap = None
        self.scale_factor = 1.0
        self.offset = QPoint(0, 0)
        self.last_mouse_pos = QPoint()
        self.dragging = False
        # Интерактивная сетка (только превью атласа): наведение подсвечивает ячейку, клик — переход к кадру
        self.cells = None           # (cols, rows, cell_w, cell_h, frames)
        self.hover_cell = -1
        self._press_pos = None      # нажали на ячейку и ещё не потащили
        self.setMouseTracking(True)

    def set_image(self, qimg):
        self.pixmap = QPixmap.fromImage(qimg)
        self.update()

    def set_cells(self, cols, rows, cell_w, cell_h, frames):
        """Сетка атласа для наведения и клика: frames[i] — кадр в i-й ячейке."""
        self.cells = (cols, rows, cell_w, cell_h, list(frames))
        if self.hover_cell >= len(frames):
            self.hover_cell = -1
        self.update()

    def cell_at(self, pos):
        if self.cells is None or self.pixmap is None or self.scale_factor <= 0:
            return -1
        cols, rows, cell_w, cell_h, frames = self.cells
        img_x = (pos.x() - self.offset.x()) / self.scale_factor
        img_y = (pos.y() - self.offset.y()) / self.scale_factor
        if img_x < 0 or img_y < 0:
            return -1
        col = int(img_x // cell_w)
        row = int(img_y // cell_h)
        if col >= cols or row >= rows:
            return -1
        index = row * cols + col
        return index if index < len(frames) else -1

    def _update_hover(self, pos):
        index = self.cell_at(pos)
        if index != self.hover_cell:
            self.hover_cell = index
            self.update()
        self.setCursor(Qt.PointingHandCursor if index >= 0 else Qt.ArrowCursor)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(theme.VIEWPORT_BG_ATLAS))

        if self.pixmap is None:
            painter.setPen(QColor(theme.DROPZONE_HINT))
            painter.setFont(QFont("Segoe UI", 10))
            painter.drawText(self.rect(), Qt.AlignCenter, self.placeholder_text)
            return

        w = self.pixmap.width() * self.scale_factor
        h = self.pixmap.height() * self.scale_factor

        cw, ch = self.width(), self.height()

        x = self.offset.x()
        y = self.offset.y()

        target_rect = QRect(x, y, int(w), int(h))

        painter.setRenderHint(QPainter.SmoothPixmapTransform, self.scale_factor < 1.0)
        painter.drawPixmap(target_rect, self.pixmap)

        self._paint_hover_cell(painter, x, y)

        zoom_text = f"{int(self.scale_factor * 100)}%"
        painter.setPen(Qt.white)
        painter.setFont(QFont("Arial", 12, QFont.Bold))

        fm = painter.fontMetrics()
        tw = fm.horizontalAdvance(zoom_text)
        th = fm.height()

        painter.drawText(cw - tw - 10, th + 5, zoom_text)

    def _paint_hover_cell(self, painter, x, y):
        if self.hover_cell < 0 or self.cells is None:
            return
        cols, rows, cell_w, cell_h, frames = self.cells
        s = self.scale_factor
        col = self.hover_cell % cols
        row = self.hover_cell // cols
        rect = QRectF(x + col * cell_w * s, y + row * cell_h * s, cell_w * s, cell_h * s)

        accent = QColor(theme.ACCENT_HOVER)
        fill = QColor(accent)
        fill.setAlpha(40)
        painter.fillRect(rect, fill)
        painter.setPen(QPen(accent, 2))
        painter.setBrush(Qt.NoBrush)
        painter.drawRect(rect.adjusted(1, 1, -1, -1))

        # Подпись кадра в углу ячейки
        label = f"Frame {frames[self.hover_cell]}"
        painter.setFont(QFont("Segoe UI", 9, QFont.DemiBold))
        metrics = painter.fontMetrics()
        chip = QRectF(rect.left() + 6, rect.top() + 6,
                      metrics.horizontalAdvance(label) + 14, metrics.height() + 6)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(0, 0, 0, 180))
        painter.drawRoundedRect(chip, 4, 4)
        painter.setPen(QColor("#ffffff"))
        painter.drawText(chip, Qt.AlignCenter, label)
        painter.setRenderHint(QPainter.Antialiasing, False)

    def wheelEvent(self, event: QWheelEvent):
        delta = event.angleDelta().y()
        zoom_in = delta > 0

        old_scale = self.scale_factor

        if zoom_in:
            self.scale_factor *= 1.1
        else:
            self.scale_factor /= 1.1

        self.scale_factor = max(0.1, min(self.scale_factor, 20.0))

        mx = event.position().x()
        my = event.position().y()

        img_x = (mx - self.offset.x()) / old_scale
        img_y = (my - self.offset.y()) / old_scale

        new_off_x = mx - img_x * self.scale_factor
        new_off_y = my - img_y * self.scale_factor

        self.offset = QPoint(int(new_off_x), int(new_off_y))
        self._update_hover(event.position())

        self.update()

    def mousePressEvent(self, event):
        pos = event.position().toPoint()
        if event.button() == Qt.LeftButton and self.cell_at(event.position()) >= 0:
            # По ячейке клик переносит курсор таймлайна; панорама — только если потащили
            self._press_pos = pos
            self.last_mouse_pos = pos
            return
        if event.button() == Qt.LeftButton or event.button() == Qt.MiddleButton:
            self.dragging = True
            self.last_mouse_pos = pos
            self.setCursor(Qt.ClosedHandCursor)

    def mouseMoveEvent(self, event):
        pos = event.position().toPoint()
        if self._press_pos is not None and (pos - self._press_pos).manhattanLength() >= self.CLICK_SLOP:
            self._press_pos = None
            self.dragging = True
            self.setCursor(Qt.ClosedHandCursor)

        if self.dragging:
            delta = pos - self.last_mouse_pos
            self.offset += delta
            self.last_mouse_pos = pos
            self.update()
        elif self._press_pos is None:
            self._update_hover(event.position())

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self._press_pos is not None:
            self._press_pos = None
            index = self.cell_at(event.position())
            if index >= 0:
                self.cellClicked.emit(index, self.cells[4][index])
            return
        if event.button() == Qt.LeftButton or event.button() == Qt.MiddleButton:
            self.dragging = False
            self._update_hover(event.position())

    def leaveEvent(self, event):
        if self.hover_cell != -1:
            self.hover_cell = -1
            self.update()
        super().leaveEvent(event)

    def reset_view(self):
        if self.pixmap:
            w = self.pixmap.width()
            h = self.pixmap.height()
            cw = self.width()
            ch = self.height()

            if w > 0 and h > 0:
                scale_w = cw / w
                scale_h = ch / h
                self.scale_factor = min(scale_w, scale_h) * 0.9

                new_w = w * self.scale_factor
                new_h = h * self.scale_factor
                self.offset = QPoint(int((cw - new_w)/2), int((ch - new_h)/2))
                self.update()


class ViewportWidget(QLabel):
    colorPicked = Signal(object) # (r, g, b)
    spriteMoveRequested = Signal(float, float)
    spriteScaleRequested = Signal(float)
    loadRequested = Signal()          # клик по пустому вьюпорту
    filesDropped = Signal(object)     # список путей, брошенных на вьюпорт
    spriteEditStarted = Signal()      # начали тащить спрайт (для undo)
    spriteEditFinished = Signal()     # отпустили спрайт
    transformEditStarted = Signal()   # начали тянуть рамку Transform (для undo)
    transformDragged = Signal(object) # (pos_x, pos_y, scale_x %, scale_y %) по ходу жеста
    transformEditFinished = Signal()  # отпустили рамку Transform

    HANDLE = 8                        # размер ручки рамки Transform на экране
    HANDLE_HIT = 7                    # радиус попадания в ручку
    FRAME_CURSORS = {
        "move": Qt.SizeAllCursor,
        "l": Qt.SizeHorCursor, "r": Qt.SizeHorCursor,
        "t": Qt.SizeVerCursor, "b": Qt.SizeVerCursor,
        "tl": Qt.SizeFDiagCursor, "br": Qt.SizeFDiagCursor,
        "tr": Qt.SizeBDiagCursor, "bl": Qt.SizeBDiagCursor,
    }

    def __init__(self):
        super().__init__()
        self.setMinimumSize(640, 360)
        self.setAlignment(Qt.AlignCenter)
        self.setAcceptDrops(True)
        self.background_color = QColor(theme.VIEWPORT_BG_ATLAS)
        self.image_background_color = None
        # Стиль только для самого вьюпорта: без селектора он перекрыл бы тему у панели воспроизведения
        self.setObjectName("viewport")
        self.setStyleSheet(f"#viewport {{ background-color: {theme.VIEWPORT_BG_ATLAS}; }}")
        self._current_image = None
        self.picking_mode = False
        self.crop_rect = None
        self.placeholder_text = "Drop a video with animation here"
        self.placeholder_hint = "Click to browse, or drag & drop a file"
        self._drag_active = False

        # Zoom & Drag
        self.pixmap_item = None
        self.scale_factor = 1.0
        self.offset = QPoint(0, 0)
        self.last_mouse_pos = QPoint()
        self.dragging = False
        self.sprite_mode = False
        self.sprite_selected = False
        self.sprite_mask = None
        self.sprite_dragging = False
        self._overlay = None
        self._has_alpha = False
        self._checker = self._make_checker_brush()
        # Рамка Transform (Atlas): ручки масштабируют содержимое, перетаскивание внутри — сдвигает
        self.transform_frame = None     # (pos_x, pos_y, scale_x %, scale_y %) или None — рамки нет
        self.transform_linked = True    # связанный масштаб — как кнопка-звено в Transform
        self.transform_drag = None      # снимок на начало перетаскивания рамки
        self.frame_hover = None         # часть рамки под мышью
        self.setMouseTracking(True)

    # --- рамка Transform ---------------------------------------------------
    def set_transform_frame(self, transform):
        """Рамка Transform поверх кадра; None — рамки нет (Sprites, пустой вьюпорт)."""
        if transform == self.transform_frame:
            return
        self.transform_frame = transform
        if transform is None:
            self.frame_hover = None
        self.update()

    def set_transform_linked(self, linked):
        self.transform_linked = bool(linked)

    def _frame_box(self):
        """Прямоугольник содержимого после Transform в координатах картинки (по краям пикселей)."""
        if self.transform_frame is None or self._current_image is None:
            return None
        pos_x, pos_y, scale_x, scale_y = self.transform_frame
        h, w = self._current_image.shape[:2]
        box_w = w * scale_x / 100.0
        box_h = h * scale_y / 100.0
        return QRectF(w / 2.0 + pos_x - box_w / 2.0, h / 2.0 + pos_y - box_h / 2.0, box_w, box_h)

    def _image_to_screen(self, rect):
        s = self.scale_factor
        return QRectF(self.offset.x() + rect.left() * s, self.offset.y() + rect.top() * s,
                      rect.width() * s, rect.height() * s)

    def _screen_to_image(self, pos):
        s = self.scale_factor
        return QPointF((pos.x() - self.offset.x()) / s, (pos.y() - self.offset.y()) / s)

    def _frame_handles(self, screen_box):
        """Ручки рамки: имя → центр на экране. Боковые — только если сторона не слишком короткая."""
        left, top = screen_box.left(), screen_box.top()
        right, bottom = screen_box.right(), screen_box.bottom()
        center = screen_box.center()
        handles = {
            "tl": QPointF(left, top), "tr": QPointF(right, top),
            "bl": QPointF(left, bottom), "br": QPointF(right, bottom),
        }
        if screen_box.width() >= self.HANDLE * 3:
            handles["t"] = QPointF(center.x(), top)
            handles["b"] = QPointF(center.x(), bottom)
        if screen_box.height() >= self.HANDLE * 3:
            handles["l"] = QPointF(left, center.y())
            handles["r"] = QPointF(right, center.y())
        return handles

    def _frame_part_at(self, pos):
        """Часть рамки под мышью: имя ручки, 'move' внутри или None."""
        box = self._frame_box()
        if box is None or self.scale_factor <= 0:
            return None
        screen = self._image_to_screen(box)
        # Углы проверяются раньше боковых ручек
        for name, point in self._frame_handles(screen).items():
            if abs(pos.x() - point.x()) <= self.HANDLE_HIT and abs(pos.y() - point.y()) <= self.HANDLE_HIT:
                return name
        return "move" if screen.contains(pos) else None

    def _update_frame_hover(self, pos):
        part = self._frame_part_at(pos)
        if part != self.frame_hover:
            self.frame_hover = part
            self.update()
        self.setCursor(self.FRAME_CURSORS.get(part, Qt.ArrowCursor))

    def _begin_frame_drag(self, part, pos):
        h, w = self._current_image.shape[:2]
        self.transform_drag = {
            "part": part,
            "press": self._screen_to_image(pos),
            "box": self._frame_box(),
            "transform": self.transform_frame,
            "size": (w, h),
        }
        self.frame_hover = part
        self.setCursor(self.FRAME_CURSORS[part])
        self.transformEditStarted.emit()
        self.update()

    def _drag_frame(self, pos, modifiers):
        """Новый Transform по положению мыши — всегда от снимка на начало жеста."""
        drag = self.transform_drag
        point = self._screen_to_image(pos)
        dx = point.x() - drag["press"].x()
        dy = point.y() - drag["press"].y()
        pos_x, pos_y, scale_x, scale_y = drag["transform"]
        part = drag["part"]

        if part == "move":
            if modifiers & Qt.ShiftModifier:
                # Shift — сдвиг только по одной оси
                if abs(dx) >= abs(dy):
                    dy = 0.0
                else:
                    dx = 0.0
            self.transformDragged.emit((pos_x + dx, pos_y + dy, scale_x, scale_y))
            return

        w, h = drag["size"]
        box = drag["box"]
        horizontal = "l" in part or "r" in part
        vertical = "t" in part or "b" in part
        from_center = bool(modifiers & Qt.AltModifier)
        # Shift на время жеста переключает связанный масштаб
        linked = self.transform_linked != bool(modifiers & Qt.ShiftModifier)
        grow = 2.0 if from_center else 1.0

        width, height = box.width(), box.height()
        if horizontal:
            width += grow * (dx if "r" in part else -dx)
        if vertical:
            height += grow * (dy if "b" in part else -dy)

        # Пределы как у полей Transform: 1..1000 %
        if linked:
            kx = width / box.width() if horizontal else None
            ky = height / box.height() if vertical else None
            if kx is not None and ky is not None:
                k = kx if abs(kx - 1.0) >= abs(ky - 1.0) else ky
            else:
                k = kx if kx is not None else ky
            k_min = max(w * 0.01 / box.width(), h * 0.01 / box.height())
            k_max = min(w * 10.0 / box.width(), h * 10.0 / box.height())
            k = max(k_min, min(k, k_max))
            width, height = box.width() * k, box.height() * k
        else:
            width = max(w * 0.01, min(width, w * 10.0))
            height = max(h * 0.01, min(height, h * 10.0))

        # Противоположная сторона стоит на месте (с Alt — центр); ось, которую не тянули, растёт от центра
        if horizontal and not from_center:
            left = box.right() - width if "l" in part else box.left()
        else:
            left = box.center().x() - width / 2.0
        if vertical and not from_center:
            top = box.bottom() - height if "t" in part else box.top()
        else:
            top = box.center().y() - height / 2.0

        self.transformDragged.emit((
            left + width / 2.0 - w / 2.0,
            top + height / 2.0 - h / 2.0,
            width / w * 100.0,
            height / h * 100.0,
        ))

    def _paint_transform_frame(self, painter):
        box = self._frame_box()
        if box is None or self.scale_factor <= 0:
            return
        screen = self._image_to_screen(box)
        accent = QColor(theme.ACCENT_HOVER)

        painter.save()
        painter.setBrush(Qt.NoBrush)
        # Тёмная подложка под линией — рамку видно и на светлом кадре
        painter.setPen(QPen(QColor(0, 0, 0, 140), 3))
        painter.drawRect(screen)
        painter.setPen(QPen(accent, 1))
        painter.drawRect(screen)

        center = screen.center()
        painter.drawLine(QPointF(center.x() - 5, center.y()), QPointF(center.x() + 5, center.y()))
        painter.drawLine(QPointF(center.x(), center.y() - 5), QPointF(center.x(), center.y() + 5))

        half = self.HANDLE / 2.0
        for name, point in self._frame_handles(screen).items():
            painter.setPen(QPen(QColor(theme.TL_KEY_EDGE), 1))
            painter.setBrush(accent if name == self.frame_hover else QColor("#ffffff"))
            painter.drawRect(QRectF(point.x() - half, point.y() - half, self.HANDLE, self.HANDLE))

        if self.transform_drag is not None:
            # Текущие значения рядом с рамкой, пока её тянут
            pos_x, pos_y, scale_x, scale_y = self.transform_frame
            if self.transform_drag["part"] == "move":
                text = f"X {pos_x:.0f}   Y {pos_y:.0f}"
            else:
                text = f"{scale_x:.1f}% × {scale_y:.1f}%"
            painter.setFont(QFont("Segoe UI", 9, QFont.DemiBold))
            metrics = painter.fontMetrics()
            chip = QRectF(0, 0, metrics.horizontalAdvance(text) + 14, metrics.height() + 6)
            chip.moveBottomLeft(QPointF(screen.left(), screen.top() - 8))
            if chip.top() < 6:
                chip.moveTop(screen.top() + 8)
            chip.moveLeft(max(6.0, min(chip.left(), self.width() - chip.width() - 6)))
            chip.moveTop(max(6.0, min(chip.top(), self.height() - chip.height() - 6)))
            painter.setRenderHint(QPainter.Antialiasing, True)
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(0, 0, 0, 190))
            painter.drawRoundedRect(chip, 4, 4)
            painter.setPen(QColor("#ffffff"))
            painter.drawText(chip, Qt.AlignCenter, text)
        painter.restore()

    @staticmethod
    def _make_checker_brush():
        tile = QPixmap(16, 16)
        tile.fill(QColor(theme.VIEWPORT_CHECKER_A))
        painter = QPainter(tile)
        painter.fillRect(0, 0, 8, 8, QColor(theme.VIEWPORT_CHECKER_B))
        painter.fillRect(8, 8, 8, 8, QColor(theme.VIEWPORT_CHECKER_B))
        painter.end()
        return QBrush(tile)

    def set_overlay(self, widget):
        """Плавающая панель в левом нижнем углу вьюпорта (кнопки воспроизведения)."""
        widget.setParent(self)
        self._overlay = widget
        self._place_overlay()

    def _place_overlay(self):
        if self._overlay is None:
            return
        size = self._overlay.sizeHint()
        self._overlay.resize(size)
        self._overlay.move(12, max(0, self.height() - size.height() - 12))
        self._overlay.raise_()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._place_overlay()

    def set_background_color(self, color):
        self.background_color = QColor(color)
        self.setStyleSheet(f"#viewport {{ background-color: {self.background_color.name()}; }}")
        self.update()

    def set_image_background_color(self, color=None):
        self.image_background_color = QColor(color) if color is not None else None
        self.update()

    def set_crop_rect(self, rect):
        self.crop_rect = rect
        self.update() 

    def set_sprite_interaction(self, enabled, mask=None):
        self.sprite_mode = enabled
        self.sprite_mask = mask
        if not enabled:
            self.sprite_selected = False
            self.sprite_dragging = False
            self.setCursor(Qt.ArrowCursor)
        self.update()

    def set_placeholder(self, text, hint=""):
        self.placeholder_text = text
        self.placeholder_hint = hint
        if self.pixmap_item is None:
            self.update()

    def set_image(self, cv_image):
        self._current_image = cv_image
        if cv_image is None:
            self.pixmap_item = None
            self.clear()
            self.setCursor(Qt.PointingHandCursor)
            self.update()
            return

        if self.pixmap_item is None:
            self.setCursor(Qt.ArrowCursor)

        h, w = cv_image.shape[:2]
        ch = 3
        if len(cv_image.shape) > 2:
            ch = cv_image.shape[2]
            
        bytes_per_line = ch * w
        
        if ch == 3:
            fmt = QImage.Format_RGB888
        elif ch == 4:
            fmt = QImage.Format_RGBA8888
            pass
        self._has_alpha = ch == 4

        qimg = QImage(cv_image.data, w, h, bytes_per_line, fmt)
        self.pixmap_item = QPixmap.fromImage(qimg)
        
        # Reset view if this is the first image or if scale is 0
        if self.scale_factor == 0:
             self.reset_view()
             
        self.update()

    def reset_view(self):
        if self.pixmap_item:
            w = self.pixmap_item.width()
            h = self.pixmap_item.height()
            cw = self.width()
            ch = self.height()
            
            if w > 0 and h > 0:
                scale_w = cw / w
                scale_h = ch / h
                self.scale_factor = min(scale_w, scale_h) * 0.9 
                
                new_w = w * self.scale_factor
                new_h = h * self.scale_factor
                self.offset = QPoint(int((cw - new_w)/2), int((ch - new_h)/2))
                self.update()

    def _paint_placeholder(self, painter):
        painter.setRenderHint(QPainter.Antialiasing)

        rect = self.rect().adjusted(26, 26, -26, -26)
        if rect.width() < 120 or rect.height() < 100:
            rect = self.rect().adjusted(4, 4, -4, -4)

        if self._drag_active:
            border_color = QColor(theme.DROPZONE_BORDER_ACTIVE)
            painter.setBrush(QColor(theme.DROPZONE_FILL_ACTIVE))
            pen = QPen(border_color, 2, Qt.DashLine)
        else:
            border_color = QColor(theme.DROPZONE_BORDER)
            painter.setBrush(Qt.NoBrush)
            pen = QPen(border_color, 1, Qt.DashLine)
        pen.setDashPattern([6, 5])
        painter.setPen(pen)
        painter.drawRoundedRect(rect, 8, 8)

        cx = rect.center().x()
        cy = rect.center().y()

        # Иконка «добавить файл»: рамка с плюсом
        painter.setBrush(Qt.NoBrush)
        painter.setPen(QPen(border_color, 2))
        painter.drawRoundedRect(QRect(cx - 22, cy - 74, 44, 44), 6, 6)
        painter.drawLine(cx - 11, cy - 52, cx + 11, cy - 52)
        painter.drawLine(cx, cy - 63, cx, cy - 41)

        painter.setPen(QColor(theme.DROPZONE_TEXT))
        painter.setFont(QFont("Segoe UI", 11, QFont.DemiBold))
        painter.drawText(QRect(rect.left() + 12, cy - 20, rect.width() - 24, 44),
                         Qt.AlignHCenter | Qt.AlignTop | Qt.TextWordWrap,
                         self.placeholder_text)

        if self.placeholder_hint:
            painter.setPen(QColor(theme.DROPZONE_HINT))
            painter.setFont(QFont("Segoe UI", 9))
            painter.drawText(QRect(rect.left() + 12, cy + 30, rect.width() - 24, 22),
                             Qt.AlignHCenter | Qt.AlignTop,
                             self.placeholder_hint)

    def paintEvent(self, event):
        # We don't call super().paintEvent because we draw manually now
        painter = QPainter(self)
        painter.fillRect(self.rect(), self.background_color)

        if not self.pixmap_item:
            self._paint_placeholder(painter)
            return

        w = self.pixmap_item.width() * self.scale_factor
        h = self.pixmap_item.height() * self.scale_factor
        
        x = self.offset.x()
        y = self.offset.y()
        
        target_rect = QRect(x, y, int(w), int(h))
        if self.image_background_color is not None:
            painter.fillRect(target_rect, self.image_background_color)
        elif self._has_alpha:
            # Прозрачное (поля после Transform) — шахматкой, чтобы не сливалось с фоном вьюпорта
            painter.fillRect(target_rect, self._checker)

        painter.setRenderHint(QPainter.SmoothPixmapTransform, self.scale_factor < 1.0)
        painter.drawPixmap(target_rect, self.pixmap_item)

        # Draw Crop Rect
        if self.crop_rect:
            pw = self.pixmap_item.width()
            ph = self.pixmap_item.height()
            
            # Crop rect coords in original image pixels
            cx, cy, cw, ch = self.crop_rect
            
            # Transform to screen coords
            # screen_x = offset_x + image_x * scale
            
            vx = x + int(cx * self.scale_factor)
            vy = y + int(cy * self.scale_factor)
            vw = int(cw * self.scale_factor)
            vh = int(ch * self.scale_factor)
            
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(0, 0, 0, 150)) 
            
            # Draw semi-transparent overlay around crop area
            # 1. Top
            painter.drawRect(x, y, int(w), vy - y)
            # 2. Bottom
            painter.drawRect(x, vy + vh, int(w), (y + int(h)) - (vy + vh))
            # 3. Left
            painter.drawRect(x, vy, vx - x, vh)
            # 4. Right
            painter.drawRect(vx + vw, vy, (x + int(w)) - (vx + vw), vh)
            
            # Draw dashed border
            painter.setPen(QPen(Qt.white, 1, Qt.DashLine))
            painter.setBrush(Qt.NoBrush)
            painter.drawRect(vx, vy, vw, vh)

        self._paint_transform_frame(painter)

        if self.sprite_mode and self.sprite_selected and self.sprite_mask is not None:
            ys, xs = np.where(self.sprite_mask > 0)
            if xs.size > 0 and ys.size > 0:
                min_x, max_x = int(xs.min()), int(xs.max())
                min_y, max_y = int(ys.min()), int(ys.max())
                sx = x + int(min_x * self.scale_factor)
                sy = y + int(min_y * self.scale_factor)
                sw = int((max_x - min_x + 1) * self.scale_factor)
                sh = int((max_y - min_y + 1) * self.scale_factor)
                painter.setPen(QPen(QColor(255, 220, 60), 2, Qt.DashLine))
                painter.setBrush(Qt.NoBrush)
                painter.drawRect(sx, sy, sw, sh)

        # Масштаб — в правом верхнем углу, как у превью атласа (левый нижний занят панелью воспроизведения).
        # Рисуем после затемнения вокруг кадра, чтобы оно не глушило текст
        zoom_text = f"{int(self.scale_factor * 100)}%"
        painter.setPen(Qt.white)
        painter.setFont(QFont("Arial", 12, QFont.Bold))
        fm = painter.fontMetrics()
        painter.drawText(self.width() - fm.horizontalAdvance(zoom_text) - 10, fm.height() + 5, zoom_text)

        # Controls legend
        legend_font = QFont("Arial", 9)
        painter.setFont(legend_font)
        fm = painter.fontMetrics()
        if self.transform_frame is not None:
            lines = ["Drag frame: move · Handles: scale",
                     "Alt: from center · Shift: proportions",
                     "Wheel: zoom · Middle drag: pan"]
        else:
            lines = ["Mouse Wheel to Zoom", "Mouse Drag to Pan"]
        line_h = fm.height()
        margin = 8
        max_w = max(fm.horizontalAdvance(l) for l in lines)
        bx = self.width() - max_w - margin * 2
        by = self.height() - line_h * len(lines) - margin * 2
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(0, 0, 0, 120))
        painter.drawRoundedRect(bx - margin, by - margin,
                                max_w + margin * 2, line_h * len(lines) + margin * 2,
                                4, 4)
        painter.setPen(QColor(200, 200, 200))
        for i, line in enumerate(lines):
            painter.drawText(bx, by + line_h * (i + 1) - fm.descent(), line)

    def _mouse_to_image(self, event):
        if not self.pixmap_item or self.scale_factor <= 0:
            return None
        mx = event.position().x()
        my = event.position().y()
        img_x = int((mx - self.offset.x()) / self.scale_factor)
        img_y = int((my - self.offset.y()) / self.scale_factor)
        h, w = self._current_image.shape[:2]
        if 0 <= img_x < w and 0 <= img_y < h:
            return img_x, img_y
        return None

    def wheelEvent(self, event: QWheelEvent):
        if self.sprite_mode and self.sprite_selected:
            delta = event.angleDelta().y()
            if delta != 0:
                step = 1.05 if delta > 0 else 1 / 1.05
                self.spriteScaleRequested.emit(step)
            return

        delta = event.angleDelta().y()
        zoom_in = delta > 0
        
        old_scale = self.scale_factor
        if old_scale == 0: old_scale = 1.0
        
        if zoom_in:
            self.scale_factor *= 1.1
        else:
            self.scale_factor /= 1.1
            
        self.scale_factor = max(0.01, min(self.scale_factor, 50.0))
        
        mx = event.position().x()
        my = event.position().y()
        
        # Calculate cursor position relative to image before zoom
        # mouse_x = offset_x + img_x * old_scale
        # img_x = (mouse_x - offset_x) / old_scale
        
        img_x = (mx - self.offset.x()) / old_scale
        img_y = (my - self.offset.y()) / old_scale
        
        # Calculate new offset to keep point under cursor stable
        # new_offset_x = mouse_x - img_x * new_scale
        
        new_off_x = mx - img_x * self.scale_factor
        new_off_y = my - img_y * self.scale_factor
        
        self.offset = QPoint(int(new_off_x), int(new_off_y))
        
        self.update()

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            self._drag_active = True
            self.update()

    def dragMoveEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dragLeaveEvent(self, event):
        self._drag_active = False
        self.update()

    def dropEvent(self, event):
        self._drag_active = False
        self.update()
        if not event.mimeData().hasUrls():
            return
        paths = [url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile()]
        paths = [p for p in paths if p]
        if paths:
            event.acceptProposedAction()
            self.filesDropped.emit(paths)

    def mousePressEvent(self, event):
        # Пустой вьюпорт работает как кнопка загрузки
        if self.pixmap_item is None:
            if event.button() == Qt.LeftButton:
                self.loadRequested.emit()
            return

        # Picking Mode Logic
        if self.picking_mode and self._current_image is not None and event.button() == Qt.LeftButton:
            if not self.pixmap_item: return

            pos = self._mouse_to_image(event)
            if pos is not None:
                img_x, img_y = pos
                color = self._current_image[img_y, img_x]
                rgb = (color[2], color[1], color[0])
                self.colorPicked.emit(rgb)
                self.picking_mode = False
                self.setCursor(Qt.ArrowCursor)
            return

        if self.sprite_mode and event.button() == Qt.LeftButton:
            pos = self._mouse_to_image(event)
            hit = False
            if pos is not None and self.sprite_mask is not None:
                img_x, img_y = pos
                if 0 <= img_y < self.sprite_mask.shape[0] and 0 <= img_x < self.sprite_mask.shape[1]:
                    hit = bool(self.sprite_mask[img_y, img_x] > 0)

            if hit:
                if not self.sprite_selected:
                    self.sprite_selected = True
                self.spriteEditStarted.emit()
                self.sprite_dragging = True
                self.last_mouse_pos = event.position().toPoint()
                self.setCursor(Qt.SizeAllCursor)
                self.update()
                return

            if self.sprite_selected:
                self.sprite_selected = False
                self.update()

        # Рамка Transform: ручки — масштаб, внутри — сдвиг; мимо рамки и средней кнопкой — панорама
        if event.button() == Qt.LeftButton and self.transform_frame is not None and not self.dragging:
            part = self._frame_part_at(event.position())
            if part:
                self._begin_frame_drag(part, event.position())
                return

        # Dragging Logic
        if event.button() == Qt.LeftButton or event.button() == Qt.MiddleButton:
            self.dragging = True
            self.last_mouse_pos = event.position().toPoint()
            self.setCursor(Qt.ClosedHandCursor)

    def mouseMoveEvent(self, event):
        if self.transform_drag is not None:
            self._drag_frame(event.position(), event.modifiers())
            return

        if self.sprite_dragging:
            delta = event.position().toPoint() - self.last_mouse_pos
            self.last_mouse_pos = event.position().toPoint()
            if self.scale_factor > 0:
                dx = delta.x() / self.scale_factor
                dy = delta.y() / self.scale_factor
                self.spriteMoveRequested.emit(dx, dy)
            return

        if self.dragging:
            delta = event.position().toPoint() - self.last_mouse_pos
            self.offset += delta
            self.last_mouse_pos = event.position().toPoint()
            self.update()
        elif self.transform_frame is not None:
            self._update_frame_hover(event.position())

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self.transform_drag is not None:
            self.transform_drag = None
            self.transformEditFinished.emit()
            self._update_frame_hover(event.position())
            self.update()
            return

        if event.button() == Qt.LeftButton and self.sprite_dragging:
            self.sprite_dragging = False
            self.setCursor(Qt.ArrowCursor if not self.dragging else Qt.ClosedHandCursor)
            self.spriteEditFinished.emit()
            return

        if event.button() == Qt.LeftButton or event.button() == Qt.MiddleButton:
            self.dragging = False
            self.setCursor(Qt.ArrowCursor)
            if self.transform_frame is not None:
                self._update_frame_hover(event.position())

    def leaveEvent(self, event):
        if self.frame_hover is not None and self.transform_drag is None:
            self.frame_hover = None
            self.update()
        super().leaveEvent(event)

class TimelineWidget(QWidget):
    frameChanged = Signal(int)          # скраб/переход на кадр
    cropChanged = Signal(int, int)      # границы видео (start, end)
    markersChanged = Signal(object)     # ключи после ручной правки: перетаскивание, меню, Start Here
    editStarted = Signal()              # сейчас начнут править ключи или границу (для undo)
    editFinished = Signal()             # правка закончена

    PAD = 16            # поля слева/справа, чтобы ручки границ влезали целиком
    RULER_H = 20        # линейка с номерами кадров
    KEY_LANE_H = 26     # дорожка с ключами-ромбами
    HANDLE_W = 9        # ширина «хвата» границы
    HIT = 9             # радиус попадания мышью
    KEY_R = 7           # полудиагональ ромба
    BOX_PAD = 13        # рамка выделения шире крайних ключей на столько пикселей
    BOX_EDGE = 4        # полуширина зоны захвата края рамки
    CLICK_SLOP = 3      # сдвиг мыши в пикселях, после которого нажатие считается перетаскиванием

    def __init__(self):
        super().__init__()
        self.setFixedHeight(96)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setToolTip(
            "Diamonds are atlas keyframes: drag them or nudge with arrow keys\n"
            "Drag along the key lane to select several keys: drag the box to move them, its edges to stretch\n"
            "Right-click a key: Delete, Duplicate, Start Here\n"
            "Blue handles set the video range\n"
            "Click the track to scrub\n"
            "Ctrl+Z / Ctrl+Shift+Z: undo / redo"
        )
        self.total_frames = 0
        self.current_frame = 0
        self.crop_start = 0
        self.crop_end = 0
        self.markers = []           # кадры ключей по возрастанию — позиции на таймлайне
        self.selection = set()      # индексы выбранных ключей в markers
        self.start_key = 0          # Start Here: индекс ключа, с которого начинается атлас
        self.hover_key = -1
        self.hover_handle = None    # 'start' | 'end'
        self.hover_box = None       # 'move' | 'left' | 'right' — часть рамки выделения под мышью
        self.dragging = None        # 'current' | 'start' | 'end' | 'keys' | 'left' | 'right' | 'band'
        self._gesture = None        # снимок ключей на начало правки
        self._band = None           # (x0, x1) резиновой рамки

    # --- данные ---------------------------------------------------------
    def set_range(self, total_frames):
        self.total_frames = max(0, int(total_frames))
        self.crop_start = 0
        self.crop_end = self.last_frame()
        self.current_frame = 0
        self.markers = []
        self.selection = set()
        self.start_key = 0
        self.hover_key = -1
        self.update()

    def set_current(self, frame):
        self.current_frame = frame
        self.update()

    def set_markers(self, markers, start_key=0):
        self.markers = sorted(int(frame) for frame in markers)
        self.start_key = start_key if 0 <= start_key < len(self.markers) else 0
        self.selection = set()
        self.hover_key = -1
        self.hover_box = None
        self.update()

    def atlas_order(self):
        """Кадры в порядке атласа: от ключа Start Here до конца, затем всё, что было до него."""
        start = self.start_key if 0 <= self.start_key < len(self.markers) else 0
        return self.markers[start:] + self.markers[:start]

    def start_frame(self):
        """Кадр ключа Start Here или None, если атлас идёт с первого ключа."""
        if 0 < self.start_key < len(self.markers):
            return self.markers[self.start_key]
        return None

    def set_start_near(self, frame):
        """Start Here на ключ, ближайший к кадру, — чтобы стык пережил пересчёт ключей."""
        if frame is None or not self.markers:
            self.start_key = 0
        else:
            self.start_key = min(range(len(self.markers)), key=lambda i: (abs(self.markers[i] - frame), i))
        self.update()

    def last_frame(self):
        return max(0, self.total_frames - 1)

    # --- геометрия ------------------------------------------------------
    def track_left(self):
        return self.PAD

    def track_right(self):
        return max(self.PAD + 1, self.width() - self.PAD)

    def x_pos(self, frame):
        last = self.last_frame()
        if last <= 0:
            return self.track_left()
        span = self.track_right() - self.track_left()
        f = max(0, min(int(frame), last))
        return int(self.track_left() + span * f / last)

    def get_frame_at_x(self, x):
        last = self.last_frame()
        span = self.track_right() - self.track_left()
        if last <= 0 or span <= 0:
            return 0
        f = int(round((x - self.track_left()) / span * last))
        return max(0, min(f, last))

    def _key_lane_top(self):
        return self.height() - self.KEY_LANE_H

    def _tick_step(self):
        span = self.track_right() - self.track_left()
        last = self.last_frame()
        if last <= 0 or span <= 0:
            return 1
        raw = last * 58.0 / span
        for step in (1, 2, 5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000):
            if step >= raw:
                return step
        return 10000

    def _selected_span(self):
        frames = [self.markers[i] for i in self.selection]
        return min(frames), max(frames)

    def _box_rect(self):
        """Рамка вокруг выделения из двух и больше ключей."""
        if len(self.selection) < 2:
            return None
        lo, hi = self._selected_span()
        key_top = self._key_lane_top()
        left = self.x_pos(lo) - self.BOX_PAD
        right = self.x_pos(hi) + self.BOX_PAD
        return QRectF(left, key_top + 2, right - left, self.height() - key_top - 4)

    # --- отрисовка ------------------------------------------------------
    def paintEvent(self, event):
        painter = QPainter(self)
        w = self.width()
        h = self.height()

        painter.fillRect(0, 0, w, h, QColor(theme.TL_BG))
        painter.fillRect(0, 0, w, self.RULER_H, QColor(theme.TL_RULER_BG))

        key_top = self._key_lane_top()
        painter.fillRect(0, key_top, w, h - key_top, QColor(theme.TL_KEYLANE))

        if self.total_frames <= 0:
            painter.setPen(QColor(theme.TL_TEXT))
            painter.setFont(QFont("Segoe UI", 9))
            painter.drawText(self.rect(), Qt.AlignCenter, "Timeline appears once media is loaded")
            return

        left = self.track_left()
        right = self.track_right()
        track_top = self.RULER_H
        track_h = key_top - track_top

        xs = self.x_pos(self.crop_start)
        xe = self.x_pos(self.crop_end)

        # Дорожка и рабочая область
        painter.fillRect(left, track_top, right - left, track_h, QColor(theme.TL_TRACK))

        gradient = QLinearGradient(0, track_top, 0, track_top + track_h)
        gradient.setColorAt(0.0, QColor(theme.TL_RANGE_TOP))
        gradient.setColorAt(1.0, QColor(theme.TL_RANGE_BOTTOM))
        painter.fillRect(xs, track_top, max(1, xe - xs), track_h, QBrush(gradient))

        # Всё, что вне выбранных границ, гасим
        painter.fillRect(0, track_top, xs, track_h, QColor(theme.TL_OUT))
        painter.fillRect(xe, track_top, w - xe, track_h, QColor(theme.TL_OUT))

        painter.setPen(QPen(QColor(theme.TL_KEY_EDGE), 1))
        painter.drawLine(0, track_top, w, track_top)
        painter.drawLine(0, key_top - 1, w, key_top - 1)

        self._paint_ruler(painter, track_top, track_h)
        self._paint_selection(painter)
        self._paint_seam(painter, track_top)
        self._paint_keys(painter, track_top, key_top)
        self._paint_handles(painter, xs, xe, track_top, track_h)
        self._paint_playhead(painter)

    def _paint_ruler(self, painter, track_top, track_h):
        step = self._tick_step()
        minor = max(1, step // 5)
        last = self.last_frame()

        painter.setFont(QFont("Segoe UI", 8))
        metrics = painter.fontMetrics()

        for frame in range(0, last + 1, minor):
            x = self.x_pos(frame)
            major = (frame % step == 0)

            painter.setPen(QPen(QColor(theme.TL_TICK), 1))
            painter.drawLine(x, self.RULER_H - (8 if major else 4), x, self.RULER_H - 1)

            if not major:
                continue

            painter.setPen(QPen(QColor(theme.TL_GRID), 1))
            painter.drawLine(x, track_top + 1, x, track_top + track_h - 1)

            label = str(frame)
            tx = x + 3
            if tx + metrics.horizontalAdvance(label) > self.width():
                tx = x - 3 - metrics.horizontalAdvance(label)
            painter.setPen(QColor(theme.TL_TEXT))
            painter.drawText(tx, self.RULER_H - 9, label)

    def _paint_selection(self, painter):
        """Резиновая рамка, пока выделяют, или рамка вокруг выбранных ключей."""
        key_top = self._key_lane_top()
        painter.setRenderHint(QPainter.Antialiasing, True)

        if self._band is not None:
            x0, x1 = self._band
            fill = QColor(theme.TL_BAND)
            fill.setAlpha(46)
            painter.setPen(QPen(QColor(theme.TL_BAND), 1))
            painter.setBrush(fill)
            painter.drawRect(QRectF(min(x0, x1), key_top + 2.5, abs(x1 - x0), self.height() - key_top - 5))
        else:
            rect = self._box_rect()
            if rect is not None:
                color = QColor(theme.TL_KEY_SELECTED)
                active = self.hover_box is not None or self.dragging in ("keys", "left", "right")
                fill = QColor(color)
                fill.setAlpha(40 if active else 24)
                edge = QColor(color)
                edge.setAlpha(210 if active else 150)
                painter.setPen(QPen(edge, 1))
                painter.setBrush(fill)
                painter.drawRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5), 3, 3)

                lo, hi = self._selected_span()
                if hi > lo:
                    # Хваты по краям: за них рамка растягивается, как границы таймлайна
                    painter.setPen(Qt.NoPen)
                    for part, x in (("left", rect.left()), ("right", rect.right())):
                        grip = QColor(color)
                        grip.setAlpha(255 if part in (self.hover_box, self.dragging) else 170)
                        painter.setBrush(grip)
                        painter.drawRoundedRect(QRectF(x - 1.5, rect.top() + 5, 3, rect.height() - 10), 1.5, 1.5)

        painter.setRenderHint(QPainter.Antialiasing, False)

    def _paint_seam(self, painter, track_top):
        """Start Here: пунктир на стыке — атлас начинается отсюда, всё левее уходит в конец."""
        frame = self.start_frame()
        if frame is None:
            return
        x = self.x_pos(frame)
        color = QColor(theme.TL_SEAM)
        pen = QPen(color, 1)
        pen.setStyle(Qt.CustomDashLine)
        pen.setDashPattern([4, 3])
        painter.setPen(pen)
        painter.drawLine(x, track_top, x, self.height())

        painter.setFont(QFont("Segoe UI", 7, QFont.Bold))
        metrics = painter.fontMetrics()
        label = "START"
        chip = QRect(x, track_top + 1, metrics.horizontalAdvance(label) + 8, 13)
        if chip.right() >= self.width():
            chip.moveRight(x)
        painter.setPen(Qt.NoPen)
        painter.setBrush(color)
        painter.drawRect(chip)
        painter.setPen(QColor(theme.TL_SEAM_TEXT))
        painter.drawText(chip, Qt.AlignCenter, label)

    def _draw_diamond(self, painter, cx, cy, radius, color, outline=None):
        shape = QPolygonF([
            QPointF(cx, cy - radius),
            QPointF(cx + radius, cy),
            QPointF(cx, cy + radius),
            QPointF(cx - radius, cy),
        ])
        painter.setBrush(QBrush(color))
        painter.setPen(QPen(outline if outline is not None else QColor(theme.TL_KEY_EDGE), 1))
        painter.drawPolygon(shape)

    def _paint_keys(self, painter, track_top, key_top):
        if not self.markers:
            return

        painter.setRenderHint(QPainter.Antialiasing, True)
        lane_cy = key_top + (self.height() - key_top) // 2

        for index, frame in enumerate(self.markers):
            x = self.x_pos(frame)
            selected = index in self.selection
            hovered = (index == self.hover_key)

            painter.setPen(QPen(QColor(255, 255, 255, 60 if (selected or hovered) else 26), 1))
            painter.drawLine(x, track_top + 2, x, key_top - 2)

            if selected:
                color = QColor(theme.TL_KEY_SELECTED)
            elif hovered:
                color = QColor(theme.TL_KEY_HOVER)
            else:
                color = QColor(theme.TL_KEY)

            if selected:
                halo = QColor(theme.TL_KEY_SELECTED)
                halo.setAlpha(90)
                self._draw_diamond(painter, x, lane_cy, self.KEY_R + 3, QColor(0, 0, 0, 0), halo)

            self._draw_diamond(painter, x, lane_cy, self.KEY_R, color)

        painter.setRenderHint(QPainter.Antialiasing, False)

    def _paint_handles(self, painter, xs, xe, track_top, track_h):
        for kind, x in (("start", xs), ("end", xe)):
            hovered = (self.hover_handle == kind or self.dragging == kind)
            color = QColor(theme.TL_HANDLE_HOVER if hovered else theme.TL_HANDLE)

            painter.setPen(QPen(color, 2))
            painter.drawLine(x, track_top, x, self.height())

            tab_h = min(30, track_h - 6)
            tab_top = track_top + 4
            if kind == "start":
                tab = QRect(x, tab_top, self.HANDLE_W, tab_h)
            else:
                tab = QRect(x - self.HANDLE_W, tab_top, self.HANDLE_W, tab_h)

            painter.setPen(QPen(QColor(theme.TL_KEY_EDGE), 1))
            painter.setBrush(QBrush(color))
            painter.drawRoundedRect(tab, 2, 2)

            painter.setPen(QPen(QColor(theme.TL_HANDLE_GRIP), 1))
            gx = tab.center().x()
            for offset in (-2, 1):
                painter.drawLine(gx + offset, tab.top() + 5, gx + offset, tab.bottom() - 5)

        # Номера кадров границ
        painter.setFont(QFont("Segoe UI", 8))
        metrics = painter.fontMetrics()
        label_y = track_top + track_h - 20
        for kind, x, text in (("start", xs, str(self.crop_start)), ("end", xe, str(self.crop_end))):
            tw = metrics.horizontalAdvance(text) + 10
            if xe - xs < tw * 2 + 8:
                continue
            if kind == "start":
                chip = QRect(x + self.HANDLE_W + 3, label_y, tw, 16)
            else:
                chip = QRect(x - self.HANDLE_W - 3 - tw, label_y, tw, 16)
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(0, 0, 0, 130))
            painter.drawRoundedRect(chip, 2, 2)
            painter.setPen(QColor(theme.TL_HANDLE_HOVER))
            painter.drawText(chip, Qt.AlignCenter, text)

    def _paint_playhead(self, painter):
        cx = self.x_pos(self.current_frame)

        painter.setPen(QPen(QColor(theme.TL_PLAYHEAD), 1))
        painter.drawLine(cx, self.RULER_H, cx, self.height())

        painter.setFont(QFont("Segoe UI", 8, QFont.DemiBold))
        metrics = painter.fontMetrics()
        label = str(self.current_frame)
        tw = max(24, metrics.horizontalAdvance(label) + 12)
        hx = max(0, min(cx - tw // 2, self.width() - tw))
        head = QRect(hx, 1, tw, self.RULER_H - 3)

        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(theme.TL_PLAYHEAD))
        painter.drawRoundedRect(head, 2, 2)
        painter.setPen(QColor(theme.TL_PLAYHEAD_TEXT))
        painter.drawText(head, Qt.AlignCenter, label)

    # --- попадания мышью ------------------------------------------------
    def _key_at(self, x, y):
        if not self.markers or y < self._key_lane_top() - 6:
            return -1
        best = -1
        best_dist = self.HIT + 1
        for index, frame in enumerate(self.markers):
            dist = abs(x - self.x_pos(frame))
            if dist < best_dist:
                best = index
                best_dist = dist
        return best if best_dist <= self.HIT else -1

    def _handle_at(self, x, y):
        if y < self.RULER_H - 2:
            return None
        xs = self.x_pos(self.crop_start)
        xe = self.x_pos(self.crop_end)
        on_start = xs - self.HIT <= x <= xs + self.HANDLE_W
        on_end = xe - self.HANDLE_W <= x <= xe + self.HIT
        if on_start and on_end:
            return "start" if abs(x - xs) <= abs(x - xe) else "end"
        if on_start:
            return "start"
        if on_end:
            return "end"
        return None

    def _box_part_at(self, x, y):
        """Часть рамки выделения под мышью: 'left'/'right' — края, 'move' — внутри."""
        rect = self._box_rect()
        if rect is None or not (rect.top() - 2 <= y <= rect.bottom() + 2):
            return None
        lo, hi = self._selected_span()
        # Ключи в одном кадре растягивать некуда — остаётся только сдвиг
        if hi > lo:
            if abs(x - rect.left()) <= self.BOX_EDGE:
                return "left"
            if abs(x - rect.right()) <= self.BOX_EDGE:
                return "right"
        if rect.left() <= x <= rect.right():
            return "move"
        return None

    # --- правка ключей --------------------------------------------------
    def _reorder(self, frames, selected, start, focus=None):
        """Сортирует кадры ключей и переносит выделение, Start Here и фокус на новые индексы.

        Возвращает новый индекс фокуса (или None).
        """
        order = sorted(range(len(frames)), key=lambda i: (frames[i], i))
        position = {old: new for new, old in enumerate(order)}
        self.markers = [frames[i] for i in order]
        self.selection = {position[i] for i in selected}
        # Без Start Here атлас идёт по времени: первым остаётся тот, кто сейчас левее всех
        self.start_key = position[start] if start > 0 else 0
        return position[focus] if focus is not None else None

    def _begin_keys_gesture(self, kind, grab=None, x=0.0):
        """Начало правки выбранных ключей: 'keys' — сдвиг, 'left'/'right' — растяжение рамки."""
        frames = [self.markers[i] for i in self.selection]
        self.editStarted.emit()
        self.dragging = kind
        self._gesture = {
            "frames": list(self.markers),
            "selected": set(self.selection),
            "start": self.start_key,
            "grab": grab,               # ключ под мышью — за ним едет курсор таймлайна
            "lo": min(frames),
            "hi": max(frames),
            "press_x": x,
            "press_frame": self.get_frame_at_x(x),
            "moved": False,
            "edited": False,
        }

    def _drag_keys(self, delta):
        """Сдвиг или растяжение выбранных ключей от снимка на начало жеста — округление не копится."""
        gesture = self._gesture
        frames = gesture["frames"]
        selected = gesture["selected"]
        lo, hi = gesture["lo"], gesture["hi"]
        values = list(frames)
        focus = gesture["grab"]

        if self.dragging == "keys":
            # Группа упирается в границы диапазона целиком — расстояния между ключами не меняются
            delta = max(self.crop_start - lo, min(delta, self.crop_end - hi))
            for i in selected:
                values[i] = frames[i] + delta
        else:
            # Как у границ таймлайна: противоположный край стоит, ключи расходятся пропорционально
            if self.dragging == "right":
                anchor, moving = lo, hi
                edge = max(lo, min(hi + delta, self.crop_end))
            else:
                anchor, moving = hi, lo
                edge = min(hi, max(lo + delta, self.crop_start))
            ratio = (edge - anchor) / (moving - anchor)
            for i in selected:
                values[i] = int(round(anchor + (frames[i] - anchor) * ratio))
            # Курсор таймлайна показывает кадр на растягиваемом краю
            focus = next(i for i in selected if frames[i] == moving)

        before = (self.markers, self.selection, self.start_key)
        focus = self._reorder(values, selected, gesture["start"], focus)
        if (self.markers, self.selection, self.start_key) == before:
            return
        gesture["edited"] = True
        if focus is not None:
            self.current_frame = self.markers[focus]
            self.frameChanged.emit(self.current_frame)
        self.update()

    def _scale_keys_to_range(self):
        """Границы тянут: ключи масштабируются вместе с диапазоном, взаимное расположение сохраняется.

        Считаем от снимка на начало жеста, иначе округление до кадров копилось бы на каждом шаге.
        """
        gesture = self._gesture
        start0, end0 = gesture["range"]
        start1, end1 = self.crop_start, self.crop_end
        ratio = (end1 - start1) / (end0 - start0)
        values = [max(start1, min(end1, int(round(start1 + (frame - start0) * ratio))))
                  for frame in gesture["frames"]]
        before = (self.markers, self.selection, self.start_key)
        self._reorder(values, gesture["selected"], gesture["start"])
        if (self.markers, self.selection, self.start_key) != before:
            gesture["edited"] = True

    def _finish_keys_gesture(self):
        gesture = self._gesture
        self._gesture = None
        self.dragging = None
        if not gesture["moved"] and gesture["grab"] is not None and len(gesture["selected"]) > 1:
            # Клик по ключу из выделения без перетаскивания оставляет выбранным только его
            self.selection = {gesture["grab"]}
        if gesture["edited"]:
            self.markersChanged.emit(list(self.markers))
        self.editFinished.emit()

    def _free_frame_near(self, frame, taken):
        """Ближайший кадр без ключа: сначала правее, потом левее; весь диапазон занят — тот же кадр."""
        for candidate in range(frame + 1, self.crop_end + 1):
            if candidate not in taken:
                return candidate
        for candidate in range(frame - 1, self.crop_start - 1, -1):
            if candidate not in taken:
                return candidate
        return frame

    def delete_keys(self, indices):
        """Удаляет ключи, хотя бы один остаётся. Start Here с удалённого ключа переходит на следующий."""
        doomed = set(indices)
        count = len(self.markers)
        if not doomed or len(doomed) >= count:
            return
        start = self.start_key
        if start in doomed:
            start = next((start + step) % count for step in range(1, count)
                         if (start + step) % count not in doomed)
        keep = [i for i in range(count) if i not in doomed]

        self.editStarted.emit()
        self.markers = [self.markers[i] for i in keep]
        self.start_key = keep.index(start)
        self.selection = set()
        self.hover_key = -1
        self.hover_box = None
        self.update()
        self.markersChanged.emit(list(self.markers))
        self.editFinished.emit()

    def duplicate_keys(self, indices):
        """Копии ключей встают на ближайший свободный кадр справа и становятся выделением."""
        sources = sorted(set(indices))
        if not sources:
            return
        taken = set(self.markers)
        copies = []
        for index in sources:
            frame = self._free_frame_near(self.markers[index], taken)
            taken.add(frame)
            copies.append(frame)

        self.editStarted.emit()
        count = len(self.markers)
        self._reorder(self.markers + copies, range(count, count + len(copies)), self.start_key)
        self.hover_key = -1
        self.hover_box = None
        self.update()
        self.markersChanged.emit(list(self.markers))
        self.editFinished.emit()

    def set_start_key(self, index):
        """Start Here: атлас начинается с этого ключа, ключи левее уходят в конец."""
        if not (0 <= index < len(self.markers)) or index == self.start_key:
            return
        self.editStarted.emit()
        self.start_key = index
        self.update()
        self.markersChanged.emit(list(self.markers))
        self.editFinished.emit()

    def _show_key_menu(self, index, global_pos):
        # Правый клик по ключу вне выделения выбирает только этот ключ
        if index not in self.selection:
            self.selection = {index}
        self.hover_key = -1
        self.hover_box = None
        self.update()

        targets = sorted(self.selection)
        noun = "Key" if len(targets) == 1 else f"{len(targets)} Keys"

        menu = QMenu(self)
        menu.setToolTipsVisible(True)
        act_delete = menu.addAction(f"Delete {noun}")
        # Хотя бы один ключ в атласе должен остаться
        act_delete.setEnabled(len(targets) < len(self.markers))
        act_duplicate = menu.addAction(f"Duplicate {noun}")
        act_duplicate.setToolTip("The copy goes to the next free frame on the right")
        menu.addSeparator()
        act_start = menu.addAction("Start Here")
        act_start.setToolTip("The atlas starts from this key; keys before it move to the end")
        act_start.setEnabled(index != self.start_key)

        chosen = menu.exec(global_pos)
        if chosen is act_delete:
            self.delete_keys(targets)
        elif chosen is act_duplicate:
            self.duplicate_keys(targets)
        elif chosen is act_start:
            self.set_start_key(index)

    # --- события --------------------------------------------------------
    def _update_hover(self, x, y):
        part = self._box_part_at(x, y)
        key = -1 if part in ("left", "right") else self._key_at(x, y)
        handle = None if (key >= 0 or part) else self._handle_at(x, y)
        if (key, handle, part) != (self.hover_key, self.hover_handle, self.hover_box):
            self.hover_key = key
            self.hover_handle = handle
            self.hover_box = part
            self.update()
        if part in ("left", "right") or handle:
            self.setCursor(theme.split_h_cursor())
        elif key >= 0 or part == "move":
            self.setCursor(Qt.SizeHorCursor)
        else:
            self.setCursor(Qt.ArrowCursor)

    def mousePressEvent(self, event):
        if self.total_frames <= 0 or self.dragging is not None:
            return

        x = event.position().x()
        y = event.position().y()

        if event.button() == Qt.RightButton:
            # Правый клик мимо ключей сбрасывает выделение; по ключу откроется меню (contextMenuEvent)
            if self._key_at(x, y) < 0 and self.selection:
                self.selection = set()
                self.hover_box = None
                self.update()
            return

        if event.button() != Qt.LeftButton:
            return

        part = self._box_part_at(x, y)
        if part in ("left", "right"):
            self._begin_keys_gesture(part, x=x)
            self.setCursor(theme.split_h_cursor())
            self.update()
            return

        index = self._key_at(x, y)
        if index >= 0:
            if index not in self.selection:
                self.selection = {index}
            self._begin_keys_gesture("keys", grab=index, x=x)
            self.setCursor(Qt.SizeHorCursor)
            self.current_frame = self.markers[index]
            self.update()
            self.frameChanged.emit(self.current_frame)
            return

        if part == "move":
            self._begin_keys_gesture("keys", x=x)
            self.setCursor(Qt.SizeHorCursor)
            self.update()
            return

        handle = self._handle_at(x, y)
        if handle:
            self.editStarted.emit()
            self.dragging = handle
            # Ключи будут масштабироваться вместе с диапазоном — от этого снимка
            self._gesture = {
                "frames": list(self.markers),
                "selected": set(self.selection),
                "start": self.start_key,
                "range": (self.crop_start, self.crop_end),
                "edited": False,
            }
            self.setCursor(theme.split_h_cursor())
            self.update()
            return

        # Клик в свободном месте сбрасывает выделение
        self.selection = set()
        if y >= self._key_lane_top():
            # В дорожке ключей тянется резиновая рамка выделения
            self.dragging = "band"
            self._band = (x, x)
            self.update()
            return

        self.dragging = "current"
        frame = self.get_frame_at_x(x)
        self.current_frame = frame
        self.update()
        self.frameChanged.emit(frame)

    def mouseMoveEvent(self, event):
        if self.total_frames <= 0:
            return

        x = event.position().x()
        y = event.position().y()

        if self.dragging is None:
            self._update_hover(x, y)
            return

        if self.dragging == "band":
            x0 = self._band[0]
            self._band = (x0, x)
            lo, hi = min(x0, x), max(x0, x)
            # Ключ попадает в выделение, если рамка задела его ромб
            if hi - lo < self.CLICK_SLOP:
                self.selection = set()
            else:
                self.selection = {
                    index for index, frame in enumerate(self.markers)
                    if lo - self.KEY_R <= self.x_pos(frame) <= hi + self.KEY_R
                }
            self.update()
            return

        frame = self.get_frame_at_x(x)

        if self.dragging in ("keys", "left", "right"):
            gesture = self._gesture
            if not gesture["moved"]:
                if abs(x - gesture["press_x"]) < self.CLICK_SLOP:
                    return
                gesture["moved"] = True
            self._drag_keys(frame - gesture["press_frame"])
        elif self.dragging in ("start", "end"):
            if self.dragging == "start":
                self.crop_start = max(0, min(frame, self.crop_end - 1))
            else:
                self.crop_end = min(self.last_frame(), max(frame, self.crop_start + 1))
            self._scale_keys_to_range()
            self.cropChanged.emit(self.crop_start, self.crop_end)
            self.update()
        else:
            self.current_frame = frame
            self.update()
            self.frameChanged.emit(frame)

    def mouseReleaseEvent(self, event):
        if event.button() != Qt.LeftButton or self.dragging is None:
            return
        was_dragging = self.dragging
        if was_dragging == "band":
            self.dragging = None
            self._band = None
        elif was_dragging in ("keys", "left", "right"):
            self._finish_keys_gesture()
        elif was_dragging in ("start", "end"):
            gesture = self._gesture
            self._gesture = None
            self.dragging = None
            if gesture["edited"]:
                self.markersChanged.emit(list(self.markers))
            self.editFinished.emit()
        else:
            self.dragging = None
        self._update_hover(event.position().x(), event.position().y())
        self.update()

    def contextMenuEvent(self, event):
        if self.total_frames <= 0 or self.dragging is not None:
            return
        index = self._key_at(event.pos().x(), event.pos().y())
        if index >= 0:
            self._show_key_menu(index, event.globalPos())

    def leaveEvent(self, event):
        if self.hover_key != -1 or self.hover_handle is not None or self.hover_box is not None:
            self.hover_key = -1
            self.hover_handle = None
            self.hover_box = None
            self.update()
        super().leaveEvent(event)

    def keyPressEvent(self, event):
        if self.total_frames <= 0 or self.dragging is not None:
            super().keyPressEvent(event)
            return

        key = event.key()
        if key in (Qt.Key_Delete, Qt.Key_Backspace) and self.selection:
            self.delete_keys(self.selection)
            return
        if key == Qt.Key_Escape and self.selection:
            self.selection = set()
            self.update()
            return
        if key not in (Qt.Key_Left, Qt.Key_Right):
            super().keyPressEvent(event)
            return

        step = 10 if event.modifiers() & Qt.ShiftModifier else 1
        delta = -step if key == Qt.Key_Left else step

        if self.selection:
            # Один выбранный ключ тянет за собой курсор таймлайна, группа — нет
            grab = next(iter(self.selection)) if len(self.selection) == 1 else None
            self._begin_keys_gesture("keys", grab=grab)
            self._gesture["moved"] = True
            self._drag_keys(delta)
            self._finish_keys_gesture()
            return

        frame = max(0, min(self.current_frame + delta, self.last_frame()))
        self.current_frame = frame
        self.update()
        self.frameChanged.emit(frame)

class ExportVideoDialog(QDialog):
    PRESETS = [
        {"label": "PNG Sequence",  "ext": ".png", "codec": None,        "pix_fmt": None},
        {"label": "MP4 (H.264)",   "ext": ".mp4", "codec": "libx264",   "pix_fmt": "yuv420p"},
        {"label": "MP4 (H.265)",   "ext": ".mp4", "codec": "libx265",   "pix_fmt": "yuv420p"},
        {"label": "WebM (VP9)",    "ext": ".webm", "codec": "libvpx-vp9","pix_fmt": "yuv420p"},
        {"label": "WebM (VP8)",    "ext": ".webm", "codec": "libvpx",    "pix_fmt": "yuv420p"},
        {"label": "AVI (MJPEG)",   "ext": ".avi", "codec": "mjpeg",     "pix_fmt": "yuvj420p"},
        {"label": "MOV (ProRes)",  "ext": ".mov", "codec": "prores_ks", "pix_fmt": "yuva444p10le"},
    ]

    def __init__(self, fps=30.0, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Export Video")
        self.setMinimumWidth(340)

        layout = QFormLayout(self)

        self.combo_format = QComboBox()
        for p in self.PRESETS:
            self.combo_format.addItem(p["label"])
        self.combo_format.currentIndexChanged.connect(self._on_format_changed)
        layout.addRow("Format:", self.combo_format)

        self.spin_bitrate = QSpinBox()
        self.spin_bitrate.setRange(100, 200000)
        self.spin_bitrate.setValue(5000)
        self.spin_bitrate.setSuffix(" kbps")
        self.lbl_bitrate = QLabel("Bitrate:")
        layout.addRow(self.lbl_bitrate, self.spin_bitrate)

        self.spin_fps = QSpinBox()
        self.spin_fps.setRange(1, 120)
        self.spin_fps.setValue(max(1, int(fps)))
        layout.addRow("FPS:", self.spin_fps)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)

        self._on_format_changed(0)

    def _on_format_changed(self, index):
        is_sequence = self.PRESETS[index]["codec"] is None
        self.spin_bitrate.setVisible(not is_sequence)
        self.lbl_bitrate.setVisible(not is_sequence)

    def is_sequence(self):
        return self.PRESETS[self.combo_format.currentIndex()]["codec"] is None

    def get_settings(self):
        preset = self.PRESETS[self.combo_format.currentIndex()]
        return {
            "ext": preset["ext"],
            "codec": preset["codec"],
            "pix_fmt": preset["pix_fmt"],
            "bitrate": self.spin_bitrate.value() * 1000,
            "fps": self.spin_fps.value(),
            "filter": f"Video (*{preset['ext']})" if preset["codec"] else "PNG Image (*.png)",
            "label": preset["label"],
        }


class PlaybackBar(QFrame):
    """Плавающая панель воспроизведения поверх вьюпорта Atlas."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("playbackBar")
        self.setAttribute(Qt.WA_StyledBackground, True)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 5, 8, 5)
        layout.setSpacing(3)

        self.icon_play = theme.make_icon("play", "#ffffff")
        self.icon_pause = theme.make_icon("pause", "#ffffff")

        # Play — главная круглая кнопка, остальные «призрачные»
        self.btn_play = self._button(self.icon_play, "Play the range")
        self.btn_play.setObjectName("playButton")
        self.btn_play.setFixedSize(30, 30)
        self.btn_stop = self._button(theme.make_icon("stop"), "Stop and go back to the start")
        self.btn_stop.setFixedSize(28, 28)

        divider = QFrame()
        divider.setObjectName("playbackDivider")
        divider.setFixedSize(1, 18)

        self.btn_play_frames = self._button(theme.make_icon("keys", on_color="#ffffff"),
                                            "Play only the atlas frames")
        self.btn_play_frames.setCheckable(True)
        self.btn_play_frames.setText("Frames")
        self.btn_play_frames.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.btn_play_frames.setFixedHeight(28)

        self.spin_fps_frames = QSpinBox()
        self.spin_fps_frames.setRange(1, 60)
        self.spin_fps_frames.setValue(10)
        self.spin_fps_frames.setSuffix(" fps")
        self.spin_fps_frames.setFixedWidth(72)
        self.spin_fps_frames.setToolTip("Frames playback speed")

        layout.addWidget(self.btn_play)
        layout.addWidget(self.btn_stop)
        layout.addSpacing(5)
        layout.addWidget(divider)
        layout.addSpacing(5)
        layout.addWidget(self.btn_play_frames)
        layout.addSpacing(3)
        layout.addWidget(self.spin_fps_frames)

    def _button(self, icon, tooltip):
        button = QToolButton()
        button.setIcon(icon)
        button.setIconSize(QSize(16, 16))
        button.setToolTip(tooltip)
        button.setCursor(Qt.PointingHandCursor)
        # Без фокуса пробел и стрелки не нажимают кнопки случайно
        button.setFocusPolicy(Qt.NoFocus)
        return button

    def mousePressEvent(self, event):
        # Клик по фону панели не должен уходить во вьюпорт: панорама, открытие файла
        event.accept()


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Atlas Converter")
        self.resize(1600, 900)

        self.atlas_loader = VideoLoader()
        self.sprites_loader = VideoLoader()
        self.timer = QTimer()
        self.timer.timeout.connect(self.next_frame)

        self.frames_timer = QTimer()
        self.frames_timer.timeout.connect(self.next_atlas_frame)

        # Пересборка превью атласа откладывается, пока тянут границы таймлайна
        self.preview_timer = QTimer()
        self.preview_timer.setSingleShot(True)
        self.preview_timer.setInterval(150)
        self.preview_timer.timeout.connect(self.update_atlas_preview)
        self.current_marker_index = 0
        self.mode = "atlas"
        self.sprite_index = 0
        self.sprite_suffix = "_bg"
        self.sprite_offset_x = 0.0
        self.sprite_offset_y = 0.0
        self.sprite_scale = 1.0
        self.sprite_add_bg = False
        self.sprite_shadow_enabled = False
        self.sprite_shadow_x = 4
        self.sprite_shadow_y = 10
        self.sprite_shadow_softness = 8
        self.sprite_shadow_opacity = 60
        self.sprite_shadow_color = (0, 0, 0)
        self.sprite_alpha_erode = 0.0
        self.sprite_alpha_blur = 0.0
        self.sprite_per_file_transform = {}

        # Undo/redo: у каждой вкладки свой стек, как и свои ассеты
        self.atlas_undo = UndoStack()
        self.sprites_undo = UndoStack()
        self._atlas_edit_before = None
        self._sprite_edit_before = None
        self._last_sprite_wheel = 0.0
        self._transform_prev = (0, 0, 100.0, 100.0)  # Transform до последней правки (для undo)
        self._last_transform_edit = 0.0
        self._transform_drag_before = None           # Transform на начало перетаскивания рамки во вьюпорте
        self._keyed_cache = None                     # (кадр, параметры keying, результат) для вьюпорта

        self.setup_ui()
        self.setup_connections()
        self.setup_shortcuts()

    def setup_ui(self):
        main_widget = QWidget()
        self.setCentralWidget(main_widget)
        layout = QHBoxLayout(main_widget)
        layout.setContentsMargins(0, 0, 0, 0)

        splitter = QSplitter(Qt.Horizontal)
        layout.addWidget(splitter)

        left_panel = QWidget()
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(10, 10, 6, 10)
        left_layout.setSpacing(8)

        self.mode_tabs = QTabWidget()
        self.mode_tabs.setMaximumHeight(40)
        self.mode_tabs.addTab(QWidget(), "Atlas")
        self.mode_tabs.addTab(QWidget(), "Sprites")
        left_layout.addWidget(self.mode_tabs)

        self.import_stack = QStackedWidget()

        atlas_import_group = QGroupBox("Import")
        atlas_import_layout = QHBoxLayout(atlas_import_group)
        self.btn_load_atlas = QPushButton("Load Media")
        atlas_import_layout.addWidget(self.btn_load_atlas)
        atlas_import_layout.addStretch()
        self.import_stack.addWidget(atlas_import_group)

        sprites_import_group = QGroupBox("Import")
        sprites_import_layout = QHBoxLayout(sprites_import_group)
        self.btn_load_sprites = QPushButton("Load Files")
        sprites_import_layout.addWidget(self.btn_load_sprites)
        sprites_import_layout.addStretch()
        self.import_stack.addWidget(sprites_import_group)

        left_layout.addWidget(self.import_stack)
        self.lbl_info = QLabel("No media loaded")
        self.lbl_info.setObjectName("infoLabel")
        left_layout.addWidget(self.lbl_info)

        self.viewport = ViewportWidget()
        left_layout.addWidget(self.viewport, 1)

        # Воспроизведение — плавающая панель на самом вьюпорте
        self.playback_bar = PlaybackBar()
        self.btn_play = self.playback_bar.btn_play
        self.btn_stop = self.playback_bar.btn_stop
        self.btn_play_frames = self.playback_bar.btn_play_frames
        self.spin_fps_frames = self.playback_bar.spin_fps_frames
        self.viewport.set_overlay(self.playback_bar)

        self.nav_stack = QStackedWidget()

        atlas_nav = QGroupBox("Timeline")
        atlas_nav_layout = QVBoxLayout(atlas_nav)
        self.timeline = TimelineWidget()
        atlas_nav_layout.addWidget(self.timeline)
        self.nav_stack.addWidget(atlas_nav)

        sprites_nav = QGroupBox("Files Navigation")
        sprites_nav_layout = QHBoxLayout(sprites_nav)
        self.btn_sprite_prev = QPushButton("<")
        self.btn_sprite_next = QPushButton(">")
        self.spin_sprite_index = QSpinBox()
        self.spin_sprite_index.setRange(1, 1)
        self.spin_sprite_index.setValue(1)
        self.spin_sprite_index.setFixedWidth(90)
        self.lbl_sprite_total = QLabel("/ 0")
        self.chk_transform_all = QCheckBox("Transform ALL")
        self.chk_transform_all.setChecked(False)
        sprites_nav_layout.addWidget(self.btn_sprite_prev)
        sprites_nav_layout.addWidget(self.btn_sprite_next)
        sprites_nav_layout.addWidget(QLabel("Index:"))
        sprites_nav_layout.addWidget(self.spin_sprite_index)
        sprites_nav_layout.addWidget(self.lbl_sprite_total)
        sprites_nav_layout.addWidget(self.chk_transform_all)
        sprites_nav_layout.addStretch()
        self.nav_stack.addWidget(sprites_nav)

        left_layout.addWidget(self.nav_stack)

        # Transform: сдвиг и масштаб содержимого секвенции внутри кадра атласа (только Atlas)
        self.transform_group = QGroupBox("Transform")
        self.transform_group.setToolTip(
            "Moves and scales the video inside the atlas frame.\n"
            "Applies to every frame: viewport, atlas preview and export."
        )
        transform_layout = QGridLayout(self.transform_group)
        transform_layout.setHorizontalSpacing(4)
        transform_layout.setVerticalSpacing(4)

        self.spin_pos_x = QSpinBox()
        self.spin_pos_y = QSpinBox()
        for spin in (self.spin_pos_x, self.spin_pos_y):
            spin.setRange(-10000, 10000)
            spin.setSuffix(" px")
        self.spin_pos_x.setToolTip("Horizontal shift, in source pixels")
        self.spin_pos_y.setToolTip("Vertical shift, in source pixels")

        self.spin_scale_x = QDoubleSpinBox()
        self.spin_scale_y = QDoubleSpinBox()
        for spin in (self.spin_scale_x, self.spin_scale_y):
            spin.setRange(1.0, 1000.0)
            spin.setDecimals(1)
            spin.setSingleStep(1.0)
            spin.setValue(100.0)
            spin.setSuffix(" %")
        self.spin_scale_x.setToolTip("Horizontal scale")
        self.spin_scale_y.setToolTip("Vertical scale")

        for spin in (self.spin_pos_x, self.spin_pos_y, self.spin_scale_x, self.spin_scale_y):
            spin.setFixedWidth(86)
            # Значение применяется по Enter или уходу фокуса, а не на каждую набранную цифру
            spin.setKeyboardTracking(False)

        self.btn_scale_link = QToolButton()
        self.btn_scale_link.setObjectName("iconButton")
        self.btn_scale_link.setCheckable(True)
        self.btn_scale_link.setChecked(True)
        self.btn_scale_link.setIcon(theme.make_icon("unlink", theme.TEXT_DIM,
                                                    on_kind="link", on_color=theme.ACCENT_HOVER))
        self.btn_scale_link.setIconSize(QSize(16, 16))
        self.btn_scale_link.setToolTip("Keep scale proportions")

        self.btn_transform_reset = QToolButton()
        self.btn_transform_reset.setObjectName("iconButton")
        self.btn_transform_reset.setIcon(theme.make_icon("reset"))
        self.btn_transform_reset.setIconSize(QSize(16, 16))
        self.btn_transform_reset.setToolTip("Reset transform")

        def axis_label(text):
            label = QLabel(text)
            label.setObjectName("dimLabel")
            label.setAlignment(Qt.AlignCenter)
            return label

        transform_layout.addWidget(QLabel("Position"), 0, 0)
        transform_layout.addWidget(axis_label("X"), 0, 1)
        transform_layout.addWidget(self.spin_pos_x, 0, 2)
        transform_layout.addWidget(axis_label("Y"), 0, 3)
        transform_layout.addWidget(self.spin_pos_y, 0, 4)
        transform_layout.addWidget(QLabel("Scale"), 1, 0)
        transform_layout.addWidget(axis_label("X"), 1, 1)
        transform_layout.addWidget(self.spin_scale_x, 1, 2)
        # Звено между X и Y: связанный масштаб
        transform_layout.addWidget(self.btn_scale_link, 1, 3, Qt.AlignCenter)
        transform_layout.addWidget(self.spin_scale_y, 1, 4)
        transform_layout.addWidget(self.btn_transform_reset, 0, 5, 2, 1, Qt.AlignCenter)

        controls_group = QGroupBox("Controls")
        controls_layout = QHBoxLayout(controls_group)
        self.chk_keying = QCheckBox("Keying")
        self.chk_mask = QCheckBox("Mask")

        self.chk_drop_shadow = QCheckBox("Drop Shadow")
        self.spin_shadow_x = QSpinBox()
        self.spin_shadow_x.setRange(-999, 999)
        self.spin_shadow_x.setValue(self.sprite_shadow_x)
        self.spin_shadow_y = QSpinBox()
        self.spin_shadow_y.setRange(-999, 999)
        self.spin_shadow_y.setValue(self.sprite_shadow_y)
        self.spin_shadow_softness = QSpinBox()
        self.spin_shadow_softness.setRange(0, 50)
        self.spin_shadow_softness.setValue(self.sprite_shadow_softness)
        self.spin_shadow_opacity = QSpinBox()
        self.spin_shadow_opacity.setRange(0, 100)
        self.spin_shadow_opacity.setValue(self.sprite_shadow_opacity)
        self.spin_shadow_opacity.setSuffix("%")
        self.btn_shadow_color = ColorButton(self.sprite_shadow_color, "Shadow")
        for spin in (
            self.spin_shadow_x,
            self.spin_shadow_y,
            self.spin_shadow_softness,
            self.spin_shadow_opacity,
        ):
            spin.setFixedWidth(68)

        shadow_color_wrap = QWidget()
        shadow_color_layout = QHBoxLayout(shadow_color_wrap)
        shadow_color_layout.setContentsMargins(0, 0, 0, 0)
        shadow_color_layout.setSpacing(4)
        shadow_color_layout.addWidget(QLabel("Opacity"))
        shadow_color_layout.addWidget(self.spin_shadow_opacity)
        shadow_color_layout.addWidget(self.btn_shadow_color, 1)

        shadow_params_wrap = QWidget()
        shadow_params_layout = QHBoxLayout(shadow_params_wrap)
        shadow_params_layout.setContentsMargins(0, 0, 0, 0)
        shadow_params_layout.setSpacing(4)
        shadow_params_layout.addWidget(QLabel("X"))
        shadow_params_layout.addWidget(self.spin_shadow_x)
        shadow_params_layout.addWidget(QLabel("Y"))
        shadow_params_layout.addWidget(self.spin_shadow_y)
        shadow_params_layout.addWidget(QLabel("Soft"))
        shadow_params_layout.addWidget(self.spin_shadow_softness)
        shadow_params_layout.addStretch()

        self.btn_bg_color = ColorButton((128, 128, 128), "BG Color")
        self.btn_key_color = ColorButton((0, 255, 0), "Key Color")
        self.bg_color_history = [self.btn_bg_color.color]
        self.bg_preset_buttons = []
        bg_picker_wrap = QWidget()
        self.chk_add_bg = QCheckBox("Add BG")
        bg_picker_layout = QHBoxLayout(bg_picker_wrap)
        bg_picker_layout.setContentsMargins(0, 0, 0, 0)
        bg_picker_layout.setSpacing(2)
        bg_picker_layout.addWidget(self.btn_bg_color)
        for i in range(6):
            btn = QPushButton("")
            btn.setFixedSize(18, 18)
            btn.clicked.connect(lambda _, index=i: self.apply_bg_preset(index))
            self.bg_preset_buttons.append(btn)
            bg_picker_layout.addWidget(btn)

        self.spin_alpha_erode = QDoubleSpinBox()
        self.spin_alpha_erode.setRange(0.0, 5.0)
        self.spin_alpha_erode.setSingleStep(0.1)
        self.spin_alpha_erode.setDecimals(1)
        self.spin_alpha_erode.setValue(self.sprite_alpha_erode)
        self.spin_alpha_erode.setFixedWidth(74)
        self.spin_alpha_erode.setSuffix(" px")

        self.spin_alpha_blur = QDoubleSpinBox()
        self.spin_alpha_blur.setRange(0.0, 20.0)
        self.spin_alpha_blur.setSingleStep(0.5)
        self.spin_alpha_blur.setDecimals(1)
        self.spin_alpha_blur.setValue(self.sprite_alpha_blur)
        self.spin_alpha_blur.setFixedWidth(74)
        self.spin_alpha_blur.setSuffix(" px")

        alpha_refine_wrap = QWidget()
        alpha_refine_layout = QHBoxLayout(alpha_refine_wrap)
        alpha_refine_layout.setContentsMargins(0, 0, 0, 0)
        alpha_refine_layout.setSpacing(4)
        alpha_refine_layout.addWidget(QLabel("Alpha Erode"))
        alpha_refine_layout.addWidget(self.spin_alpha_erode)
        alpha_refine_layout.addSpacing(8)
        alpha_refine_layout.addWidget(QLabel("Blur"))
        alpha_refine_layout.addWidget(self.spin_alpha_blur)
        alpha_refine_layout.addStretch()

        self.sprite_controls_wrap = QWidget()
        sprite_controls_layout = QGridLayout(self.sprite_controls_wrap)
        sprite_controls_layout.setContentsMargins(0, 0, 0, 0)
        sprite_controls_layout.setHorizontalSpacing(10)
        sprite_controls_layout.setVerticalSpacing(4)
        sprite_controls_layout.addWidget(self.chk_drop_shadow, 0, 0)
        sprite_controls_layout.addWidget(self.chk_add_bg, 0, 1)
        sprite_controls_layout.addWidget(shadow_color_wrap, 1, 0)
        sprite_controls_layout.addWidget(bg_picker_wrap, 1, 1)
        sprite_controls_layout.addWidget(shadow_params_wrap, 2, 0)
        sprite_controls_layout.addWidget(alpha_refine_wrap, 2, 1)
        sprite_controls_layout.setColumnStretch(0, 1)
        sprite_controls_layout.setColumnStretch(1, 1)

        controls_layout.addWidget(self.chk_keying)
        controls_layout.addWidget(self.chk_mask)
        # В Atlas Key Color прижат вправо; в Sprites растягивается sprite_controls_wrap
        controls_layout.addStretch()
        controls_layout.addWidget(self.sprite_controls_wrap, 1)
        controls_layout.addWidget(self.btn_key_color)

        # Transform стоит там, где раньше были кнопки воспроизведения
        controls_row = QHBoxLayout()
        controls_row.setSpacing(8)
        controls_row.addWidget(self.transform_group)
        controls_row.addWidget(controls_group, 1)
        left_layout.addLayout(controls_row)
        self.update_bg_preset_buttons()
        self.update_shadow_controls()
        self.update_bg_controls()

        self.atlas_only_controls = [
            self.transform_group,
            self.chk_keying,
            self.chk_mask,
            self.btn_key_color,
        ]
        self.sprite_only_controls = [self.sprite_controls_wrap]

        keying_group = QGroupBox("Keying Settings")
        self.keying_group = keying_group
        keying_layout = QGridLayout(keying_group)

        self.slider_gain = QSlider(Qt.Horizontal)
        self.slider_gain.setRange(0, 100)
        self.slider_gain.setValue(20)

        self.slider_soften = QSlider(Qt.Horizontal)
        self.slider_soften.setRange(0, 100)
        self.slider_soften.setValue(10)

        self.slider_clip = QSlider(Qt.Horizontal)
        self.slider_clip.setRange(-50, 50)
        self.slider_clip.setValue(0)

        self.slider_sat = QSlider(Qt.Horizontal)
        self.slider_sat.setRange(0, 100)
        self.slider_sat.setValue(15)

        self.slider_val = QSlider(Qt.Horizontal)
        self.slider_val.setRange(0, 100)
        self.slider_val.setValue(15)

        self.lbl_gain_value = QLabel("20%")
        self.lbl_gain_value.setMinimumWidth(50)
        self.lbl_soften_value = QLabel("10%")
        self.lbl_soften_value.setMinimumWidth(50)
        self.lbl_clip_value = QLabel("0")
        self.lbl_clip_value.setMinimumWidth(50)
        self.lbl_sat_value = QLabel("15%")
        self.lbl_sat_value.setMinimumWidth(50)
        self.lbl_val_value = QLabel("15%")
        self.lbl_val_value.setMinimumWidth(50)

        keying_layout.addWidget(QLabel("Gain"), 0, 0)
        keying_layout.addWidget(self.slider_gain, 0, 1)
        keying_layout.addWidget(self.lbl_gain_value, 0, 2)

        keying_layout.addWidget(QLabel("Soften"), 1, 0)
        keying_layout.addWidget(self.slider_soften, 1, 1)
        keying_layout.addWidget(self.lbl_soften_value, 1, 2)
        keying_layout.addWidget(QLabel("Clip"), 1, 3)
        keying_layout.addWidget(self.slider_clip, 1, 4)
        keying_layout.addWidget(self.lbl_clip_value, 1, 5)

        keying_layout.addWidget(QLabel("Protect Sat"), 2, 0)
        keying_layout.addWidget(self.slider_sat, 2, 1)
        keying_layout.addWidget(self.lbl_sat_value, 2, 2)
        keying_layout.addWidget(QLabel("Protect Val"), 2, 3)
        keying_layout.addWidget(self.slider_val, 2, 4)
        keying_layout.addWidget(self.lbl_val_value, 2, 5)

        left_layout.addWidget(keying_group)

        atlas_group = QGroupBox("Export Settings")
        atlas_layout = QHBoxLayout(atlas_group)

        settings_container = QWidget()
        settings_layout = QVBoxLayout(settings_container)
        settings_layout.setContentsMargins(0, 0, 0, 0)

        row1 = QHBoxLayout()

        self.spin_width = QSpinBox()
        self.spin_width.setRange(1, 4096)
        self.spin_width.setValue(512)
        self.spin_height = QSpinBox()
        self.spin_height.setRange(1, 4096)
        self.spin_height.setValue(512)

        self.spin_cols = QSpinBox()
        self.spin_cols.setRange(1, 100)
        self.spin_cols.setValue(4)
        self.spin_rows = QSpinBox()
        self.spin_rows.setRange(1, 100)
        self.spin_rows.setValue(4)

        self.spin_frames = QSpinBox()
        self.spin_frames.setRange(1, 1000)
        self.spin_frames.setValue(16)

        row1.addWidget(QLabel("Frame Size"))
        row1.addWidget(QLabel("W:"))
        row1.addWidget(self.spin_width)
        row1.addWidget(QLabel("H:"))
        row1.addWidget(self.spin_height)

        row1.addSpacing(15)

        self.lbl_grid = QLabel("Atlas Grid")
        self.lbl_cols = QLabel("C:")
        self.lbl_rows = QLabel("R:")
        self.lbl_frames = QLabel("Frames:")

        row1.addWidget(self.lbl_grid)
        row1.addWidget(self.lbl_cols)
        row1.addWidget(self.spin_cols)
        row1.addWidget(self.lbl_rows)
        row1.addWidget(self.spin_rows)

        row1.addSpacing(15)

        row1.addWidget(self.lbl_frames)
        row1.addWidget(self.spin_frames)

        row1.addStretch()
        settings_layout.addLayout(row1)

        row2 = QHBoxLayout()
        self.btn_load_settings = QPushButton("Load settings")
        self.btn_save_settings = QPushButton("Save settings")

        row2.addWidget(self.btn_load_settings)
        row2.addWidget(self.btn_save_settings)
        row2.addStretch()
        settings_layout.addLayout(row2)

        atlas_layout.addWidget(settings_container, 1)

        self.btn_export_video = QPushButton("Export Video")
        self.btn_export_video.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding)
        self.btn_export_video.setMinimumWidth(120)
        self.btn_calc_atlas = QPushButton("Refresh Atlas")
        self.btn_calc_atlas.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding)
        self.btn_calc_atlas.setMinimumWidth(120)
        self.chk_rename = QCheckBox("Rename")
        self.chk_rename.setChecked(False)
        self.edit_prefix = QLineEdit()
        self.edit_prefix.setPlaceholderText("Prefix")

        export_right = QWidget()
        export_right_layout = QVBoxLayout(export_right)
        export_right_layout.setContentsMargins(0, 0, 0, 0)

        atlas_buttons_row = QHBoxLayout()
        atlas_buttons_row.setContentsMargins(0, 0, 0, 0)
        atlas_buttons_row.addWidget(self.btn_export_video)
        atlas_buttons_row.addWidget(self.btn_calc_atlas)
        export_right_layout.addLayout(atlas_buttons_row, 1)
        export_right_layout.addWidget(self.chk_rename)
        export_right_layout.addWidget(self.edit_prefix)

        atlas_layout.addWidget(export_right)

        self.atlas_settings_only_widgets = [
            self.lbl_grid,
            self.lbl_cols,
            self.spin_cols,
            self.lbl_rows,
            self.spin_rows,
            self.lbl_frames,
            self.spin_frames,
            self.btn_export_video,
            self.btn_calc_atlas,
        ]

        self.rename_widgets = [self.chk_rename, self.edit_prefix]

        left_layout.addWidget(atlas_group)

        splitter.addWidget(left_panel)

        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(6, 10, 10, 10)
        right_layout.setSpacing(8)

        self.preview_stack = QStackedWidget()
        self.atlas_viewer = AtlasViewer("Atlas Preview")
        self.sprites_viewer = AtlasViewer("Sprites Preview")
        self.preview_stack.addWidget(self.atlas_viewer)
        self.preview_stack.addWidget(self.sprites_viewer)
        right_layout.addWidget(self.preview_stack, 1)

        self.btn_export = QPushButton("Export Images")
        self.btn_export.setObjectName("primaryButton")
        self.btn_export.setMinimumHeight(32)
        right_layout.addWidget(self.btn_export)

        splitter.addWidget(right_panel)
        splitter.setSizes([800, 800])
        # Штатный курсор сплиттера на Windows вдвое крупнее остальных — ставим свой
        splitter.handle(1).setCursor(theme.split_h_cursor())

        self.set_mode(0)

    def setup_connections(self):
        self.mode_tabs.currentChanged.connect(self.set_mode)
        self.btn_load_atlas.clicked.connect(self.load_video_dialog)
        self.btn_load_sprites.clicked.connect(self.load_sprites_dialog)
        self.btn_play.clicked.connect(self.toggle_play)
        self.btn_play_frames.clicked.connect(self.toggle_play_frames)
        self.btn_stop.clicked.connect(self.stop_play)
        self.spin_fps_frames.valueChanged.connect(self.on_frames_fps_changed)

        self.timeline.frameChanged.connect(self.seek_frame)
        self.timeline.cropChanged.connect(self.on_crop_changed)
        self.timeline.markersChanged.connect(self.on_markers_edited)
        self.timeline.editStarted.connect(self.begin_atlas_edit)
        self.timeline.editFinished.connect(self.end_atlas_edit)
        self.atlas_viewer.cellClicked.connect(self.on_atlas_cell_clicked)

        self.spin_pos_x.valueChanged.connect(lambda _: self.on_transform_edited())
        self.spin_pos_y.valueChanged.connect(lambda _: self.on_transform_edited())
        self.spin_scale_x.valueChanged.connect(lambda _: self.on_transform_edited("x"))
        self.spin_scale_y.valueChanged.connect(lambda _: self.on_transform_edited("y"))
        self.btn_transform_reset.clicked.connect(self.reset_atlas_transform)
        self.btn_scale_link.toggled.connect(self.viewport.set_transform_linked)
        self.viewport.transformEditStarted.connect(self.begin_transform_drag)
        self.viewport.transformDragged.connect(self.on_transform_dragged)
        self.viewport.transformEditFinished.connect(self.end_transform_drag)

        self.viewport.loadRequested.connect(self.on_viewport_load_requested)
        self.viewport.filesDropped.connect(self.on_files_dropped)

        self.btn_sprite_prev.clicked.connect(self.prev_sprite)
        self.btn_sprite_next.clicked.connect(self.next_sprite_manual)
        self.spin_sprite_index.valueChanged.connect(self.on_sprite_index_changed)
        self.chk_transform_all.stateChanged.connect(self.on_transform_mode_changed)
        self.chk_rename.stateChanged.connect(self.on_rename_changed)

        self.btn_bg_color.colorChanged.connect(self.set_bg_color)
        self.btn_key_color.colorChanged.connect(self.set_key_color)
        self.chk_drop_shadow.stateChanged.connect(self.on_shadow_settings_changed)
        self.spin_shadow_x.valueChanged.connect(self.on_shadow_settings_changed)
        self.spin_shadow_y.valueChanged.connect(self.on_shadow_settings_changed)
        self.spin_shadow_softness.valueChanged.connect(self.on_shadow_settings_changed)
        self.spin_shadow_opacity.valueChanged.connect(self.on_shadow_settings_changed)
        self.btn_shadow_color.colorChanged.connect(self.on_shadow_settings_changed)
        self.chk_add_bg.stateChanged.connect(self.on_bg_settings_changed)
        self.spin_alpha_erode.valueChanged.connect(self.on_alpha_refine_changed)
        self.spin_alpha_blur.valueChanged.connect(self.on_alpha_refine_changed)

        self.viewport.colorPicked.connect(self.btn_key_color.set_color)
        self.viewport.spriteMoveRequested.connect(self.on_sprite_move)
        self.viewport.spriteScaleRequested.connect(self.on_sprite_scale)
        self.viewport.spriteEditStarted.connect(self.begin_sprite_edit)
        self.viewport.spriteEditFinished.connect(self.end_sprite_edit)

        self.slider_gain.valueChanged.connect(self.update_gain_label)
        self.slider_gain.valueChanged.connect(self.refresh_viewport)
        self.slider_soften.valueChanged.connect(self.update_soften_label)
        self.slider_soften.valueChanged.connect(self.refresh_viewport)
        self.slider_clip.valueChanged.connect(self.update_clip_label)
        self.slider_clip.valueChanged.connect(self.refresh_viewport)
        self.slider_sat.valueChanged.connect(self.update_sat_label)
        self.slider_sat.valueChanged.connect(self.refresh_viewport)
        self.slider_val.valueChanged.connect(self.update_val_label)
        self.slider_val.valueChanged.connect(self.refresh_viewport)
        self.chk_keying.stateChanged.connect(self.refresh_viewport)
        self.chk_mask.stateChanged.connect(self.refresh_viewport)

        # Refresh Atlas только перерисовывает превью по текущим ключам, не трогая их
        self.btn_calc_atlas.clicked.connect(self.refresh_atlas)
        self.btn_export_video.clicked.connect(self.export_video)

        self.spin_cols.valueChanged.connect(self.on_atlas_grid_changed)
        self.spin_rows.valueChanged.connect(self.on_atlas_grid_changed)
        self.spin_frames.valueChanged.connect(self.on_frames_count_changed)

        self.spin_width.valueChanged.connect(self.on_frame_size_changed)
        self.spin_height.valueChanged.connect(self.on_frame_size_changed)

        self.btn_export.clicked.connect(self.export_current)
        self.btn_save_settings.clicked.connect(self.save_settings)
        self.btn_load_settings.clicked.connect(self.load_settings)

    def setup_shortcuts(self):
        self.shortcut_undo = QShortcut(self)
        self.shortcut_undo.setKeys(QKeySequence.StandardKey.Undo)
        self.shortcut_undo.activated.connect(self.undo)

        # Redo: Ctrl+Y и Ctrl+Shift+Z на Windows, Cmd+Shift+Z на macOS
        self.shortcut_redo = QShortcut(self)
        self.shortcut_redo.setKeys(QKeySequence.StandardKey.Redo)
        self.shortcut_redo.activated.connect(self.redo)

        # Спинбокс в фокусе забирает Ctrl+Z себе (отмена правки текста числа),
        # и глобальный undo не срабатывает. Отдаём сочетание окну.
        for spin in self.findChildren(QAbstractSpinBox):
            spin.installEventFilter(self)

    def eventFilter(self, obj, event):
        if (event.type() == QEvent.ShortcutOverride
                and isinstance(obj, QAbstractSpinBox)
                and (event.matches(QKeySequence.StandardKey.Undo)
                     or event.matches(QKeySequence.StandardKey.Redo))):
            # Не принимаем событие — тогда Qt отдаст сочетание QShortcut окна
            return True
        return super().eventFilter(obj, event)

    # --- undo / redo -------------------------------------------------------
    def atlas_state(self):
        timeline = self.timeline
        return (timeline.crop_start, timeline.crop_end, tuple(timeline.markers),
                timeline.start_key, self.atlas_transform())

    def apply_atlas_state(self, state):
        crop_start, crop_end, markers, start_key, transform = state
        timeline = self.timeline
        timeline.crop_start = crop_start
        timeline.crop_end = crop_end
        timeline.set_markers(list(markers), start_key)
        self.set_atlas_transform(transform)
        self.sync_frames_spin()

        if timeline.markers:
            self.current_marker_index = min(self.current_marker_index, len(timeline.markers) - 1)
        self.preview_timer.stop()
        self.refresh_viewport()
        self.update_atlas_preview()

    def sync_frames_spin(self):
        """Frames должен совпадать с числом ключей, иначе следующий пересчёт их собьёт."""
        self.spin_frames.blockSignals(True)
        self.spin_frames.setValue(max(1, len(self.timeline.markers)))
        self.spin_frames.blockSignals(False)

    def sprites_state(self):
        return (
            self.sprite_offset_x,
            self.sprite_offset_y,
            self.sprite_scale,
            tuple(sorted(self.sprite_per_file_transform.items())),
        )

    def apply_sprites_state(self, state):
        offset_x, offset_y, scale, per_file = state
        self.sprite_offset_x = offset_x
        self.sprite_offset_y = offset_y
        self.sprite_scale = scale
        self.sprite_per_file_transform = dict(per_file)
        self.refresh_viewport()
        self.update_sprites_preview()

    def begin_atlas_edit(self):
        self._atlas_edit_before = self.atlas_state()

    def end_atlas_edit(self):
        before = self._atlas_edit_before
        self._atlas_edit_before = None
        if before is not None and before != self.atlas_state():
            self.atlas_undo.push(before)

    def begin_sprite_edit(self):
        if self.mode == "sprites":
            self._sprite_edit_before = self.sprites_state()

    def end_sprite_edit(self):
        before = self._sprite_edit_before
        self._sprite_edit_before = None
        if before is not None and before != self.sprites_state():
            self.sprites_undo.push(before)

    def _edit_in_progress(self):
        # Отмена посреди перетаскивания разъехалась бы с жестом мыши
        return (self.timeline.dragging is not None or self.viewport.sprite_dragging
                or self.viewport.transform_drag is not None)

    def undo(self):
        if self._edit_in_progress():
            return
        if self.mode == "atlas":
            if self.atlas_loader.total_frames <= 0:
                return
            state = self.atlas_undo.undo(self.atlas_state())
            if state is not None:
                self.apply_atlas_state(state)
        else:
            if self.sprites_loader.total_frames <= 0:
                return
            state = self.sprites_undo.undo(self.sprites_state())
            if state is not None:
                self.apply_sprites_state(state)

    def redo(self):
        if self._edit_in_progress():
            return
        if self.mode == "atlas":
            if self.atlas_loader.total_frames <= 0:
                return
            state = self.atlas_undo.redo(self.atlas_state())
            if state is not None:
                self.apply_atlas_state(state)
        else:
            if self.sprites_loader.total_frames <= 0:
                return
            state = self.sprites_undo.redo(self.sprites_state())
            if state is not None:
                self.apply_sprites_state(state)

    def set_mode(self, tab_index):
        self.mode = "atlas" if tab_index == 0 else "sprites"
        is_atlas = self.mode == "atlas"

        self.import_stack.setCurrentIndex(tab_index)
        self.nav_stack.setCurrentIndex(tab_index)
        self.preview_stack.setCurrentIndex(tab_index)

        for widget in self.atlas_only_controls:
            widget.setVisible(is_atlas)
        for widget in self.sprite_only_controls:
            widget.setVisible(not is_atlas)
        self.update_bg_controls()

        self.keying_group.setVisible(is_atlas)
        for widget in self.atlas_settings_only_widgets:
            widget.setVisible(is_atlas)
        for widget in self.rename_widgets:
            widget.setVisible(not is_atlas)
        self.edit_prefix.setEnabled(self.chk_rename.isChecked())

        self.btn_export.setText("Export Atlas" if is_atlas else "Export Images")

        if not is_atlas:
            self.stop_play()
            self.viewport.set_sprite_interaction(True, None)
            self.viewport.set_background_color(theme.VIEWPORT_BG_SPRITES)
            self.viewport.set_image_background_color(theme.VIEWPORT_IMAGE_BG_SPRITES)
            self.viewport.set_placeholder(
                "Drop a folder or image files here",
                "Click to browse PNG files, or drag & drop a folder",
            )
        else:
            self.viewport.set_sprite_interaction(False, None)
            self.viewport.set_background_color(theme.VIEWPORT_BG_ATLAS)
            self.viewport.set_image_background_color(None)
            self.viewport.set_placeholder(
                "Drop a video with animation here",
                "Click to browse, or drag & drop a file",
            )

        self.update_playback_bar()
        self.refresh_viewport()
        if is_atlas:
            self.update_atlas_preview()
        else:
            self.update_sprites_preview()

    def update_playback_bar(self):
        """Панель воспроизведения видна только в Atlas и только с загруженным источником."""
        self.playback_bar.setVisible(self.mode == "atlas" and self.atlas_loader.total_frames > 0)

    def load_video_dialog(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self,
            "Open Media",
            "",
            "Media Files (*.mp4 *.avi *.mov *.mkv *.webm *.png);;Video Files (*.mp4 *.avi *.mov *.mkv *.webm);;PNG Sequence (*.png)",
        )
        if not paths:
            return
        if len(paths) == 1:
            self.load_video(paths[0])
            return
        if all(path.lower().endswith(".png") for path in paths):
            self.load_png_sequence(paths)
        else:
            QMessageBox.warning(self, "Open Media", "Failed to open PNG sequence.")

    def load_sprites_dialog(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "Open PNG Files", "", "PNG Files (*.png)")
        if paths:
            self.load_sprites(paths)

    def on_viewport_load_requested(self):
        if self.mode == "atlas":
            self.load_video_dialog()
        else:
            self.load_sprites_dialog()

    def expand_dropped_paths(self, paths):
        """Разворачивает папки в список поддерживаемых файлов внутри них."""
        supported = VIDEO_EXTENSIONS + IMAGE_EXTENSIONS
        result = []
        for path in paths:
            if os.path.isdir(path):
                folder_files = [
                    os.path.join(path, name)
                    for name in os.listdir(path)
                    if name.lower().endswith(supported)
                ]
                result.extend(sorted(folder_files, key=natural_path_key))
            elif os.path.isfile(path) and path.lower().endswith(supported):
                result.append(path)
        return result

    def on_files_dropped(self, paths):
        files = self.expand_dropped_paths(paths)
        pngs = sorted([p for p in files if p.lower().endswith(IMAGE_EXTENSIONS)], key=natural_path_key)

        if self.mode == "atlas":
            videos = [p for p in files if p.lower().endswith(VIDEO_EXTENSIONS)]
            if videos:
                self.load_video(videos[0])
            elif len(pngs) == 1:
                # Один PNG — загрузчик сам подтянет остальные кадры секвенции
                self.load_video(pngs[0])
            elif pngs:
                self.load_png_sequence(pngs)
            else:
                QMessageBox.warning(
                    self, "Open Media",
                    "Drop a video (mp4, avi, mov, mkv, webm), a PNG sequence or a folder with them.")
            return

        if pngs:
            self.load_sprites(pngs)
        else:
            QMessageBox.warning(self, "Open Files", "Drop PNG files or a folder with them.")

    def load_video(self, path):
        if self.atlas_loader.load_video(path):
            self.finish_atlas_load()
        else:
            QMessageBox.warning(self, "Open Media", f"Failed to open file:\n{path}")

    def load_png_sequence(self, paths):
        if self.atlas_loader.load_image_list(sorted(paths, key=natural_path_key), is_image_list=False):
            self.finish_atlas_load()
        else:
            QMessageBox.warning(self, "Open Media", "Failed to open PNG sequence.")

    def finish_atlas_load(self):
        meta = self.atlas_loader.get_metadata()
        if meta["source_type"] == "video":
            info = f"{meta['filename']} | {meta['width']}x{meta['height']} | {meta['duration']:.2f}s | {meta['fps']} fps"
        else:
            info = f"PNG sequence | {meta['total_frames']} frames | {meta['width']}x{meta['height']} | {meta['fps']} fps"
        self.lbl_info.setText(info)
        self.timeline.set_range(meta['total_frames'])
        self.sprite_index = 0
        self.update_sprite_nav()
        self.seek_frame(0)
        self.update_atlas_markers()
        # История правок относится к прошлому файлу
        self.atlas_undo.clear()
        self.viewport.reset_view()
        self.atlas_viewer.reset_view()
        self.timer.setInterval(int(1000 / max(meta['fps'], 1)))
        self.update_playback_bar()

    def load_sprites(self, paths):
        if self.sprites_loader.load_image_list(paths):
            meta = self.sprites_loader.get_metadata()
            self.lbl_info.setText(f"{meta['total_frames']} files | {meta['width']}x{meta['height']}")
            self.sprite_index = 0
            self.sprite_offset_x = 0.0
            self.sprite_offset_y = 0.0
            self.sprite_scale = 1.0
            self.sprite_per_file_transform = {}
            self.sprites_undo.clear()
            self.update_sprite_nav()
            self.seek_sprite(self.sprite_index)
            self.update_sprites_preview()
            self.viewport.reset_view()
            self.sprites_viewer.reset_view()

            interval = int(1000 / max(meta['fps'], 1))
            self.timer.setInterval(interval)
        else:
            QMessageBox.warning(self, "Open Files", "Failed to open the selected PNG files.")

    def update_sprite_nav(self):
        total = max(self.sprites_loader.total_frames, 1)
        self.spin_sprite_index.blockSignals(True)
        self.spin_sprite_index.setRange(1, total)
        self.spin_sprite_index.setValue(min(self.sprite_index + 1, total))
        self.spin_sprite_index.blockSignals(False)
        self.lbl_sprite_total.setText(f"/ {self.sprites_loader.total_frames}")

    def prev_sprite(self):
        if self.sprites_loader.total_frames <= 0:
            return
        self.sprite_index = (self.sprite_index - 1) % self.sprites_loader.total_frames
        self.update_sprite_nav()
        self.seek_sprite(self.sprite_index)

    def next_sprite_manual(self):
        if self.sprites_loader.total_frames <= 0:
            return
        self.sprite_index = (self.sprite_index + 1) % self.sprites_loader.total_frames
        self.update_sprite_nav()
        self.seek_sprite(self.sprite_index)

    def on_sprite_index_changed(self, value):
        if self.sprites_loader.total_frames <= 0:
            return
        self.sprite_index = max(0, min(value - 1, self.sprites_loader.total_frames - 1))
        self.seek_sprite(self.sprite_index)

    def get_sprite_transform(self, idx):
        if self.chk_transform_all.isChecked():
            return self.sprite_offset_x, self.sprite_offset_y, self.sprite_scale
        return self.sprite_per_file_transform.get(idx, (0.0, 0.0, 1.0))

    def set_sprite_transform(self, idx, offset_x, offset_y, scale):
        scale = max(0.1, min(scale, 10.0))
        if self.chk_transform_all.isChecked():
            self.sprite_offset_x = offset_x
            self.sprite_offset_y = offset_y
            self.sprite_scale = scale
            return
        self.sprite_per_file_transform[idx] = (offset_x, offset_y, scale)

    def on_sprite_move(self, dx, dy):
        if self.mode != "sprites":
            return
        offset_x, offset_y, scale = self.get_sprite_transform(self.sprite_index)
        self.set_sprite_transform(self.sprite_index, offset_x + dx, offset_y + dy, scale)
        self.refresh_viewport()
        self.update_sprites_preview()

    def on_sprite_scale(self, step):
        if self.mode != "sprites":
            return
        # Серия щелчков колеса — один шаг отмены, а не двадцать
        now = time.monotonic()
        if now - self._last_sprite_wheel > 0.6:
            self.sprites_undo.push(self.sprites_state())
        self._last_sprite_wheel = now
        offset_x, offset_y, scale = self.get_sprite_transform(self.sprite_index)
        self.set_sprite_transform(self.sprite_index, offset_x, offset_y, scale * step)
        self.refresh_viewport()
        self.update_sprites_preview()

    def on_transform_mode_changed(self, state):
        if self.mode != "sprites":
            return
        self.refresh_viewport()
        self.update_sprites_preview()

    def on_rename_changed(self, state):
        self.edit_prefix.setEnabled(self.chk_rename.isChecked())

    def update_playback_buttons(self):
        playing = self.timer.isActive()
        self.btn_play.setIcon(self.playback_bar.icon_pause if playing else self.playback_bar.icon_play)
        self.btn_play.setToolTip("Pause" if playing else "Play the range")
        self.btn_play_frames.setChecked(self.frames_timer.isActive())

    def toggle_play(self):
        if self.mode != "atlas":
            self.update_playback_buttons()
            return

        if self.frames_timer.isActive():
            self.toggle_play_frames()

        if self.timer.isActive():
            self.timer.stop()
        else:
            self.timer.start()
        self.update_playback_buttons()

    def toggle_play_frames(self):
        if self.mode != "atlas":
            self.update_playback_buttons()
            return

        if self.frames_timer.isActive():
            self.frames_timer.stop()
        else:
            if self.timer.isActive():
                self.toggle_play()

            fps = self.spin_fps_frames.value()
            if fps > 0:
                self.frames_timer.setInterval(int(1000 / fps))
            self.frames_timer.start()

            # Кадры идут в порядке атласа (с учётом Start Here) — начинаем с ближайшего к курсору
            order = self.timeline.atlas_order()
            if order:
                curr = self.timeline.current_frame
                self.current_marker_index = min(range(len(order)), key=lambda i: abs(order[i] - curr))
        self.update_playback_buttons()

    def on_frames_fps_changed(self, fps):
        if self.frames_timer.isActive() and fps > 0:
            self.frames_timer.setInterval(int(1000 / fps))

    def stop_play(self):
        self.timer.stop()
        self.frames_timer.stop()
        self.update_playback_buttons()
        if self.mode == "atlas":
            # С Start Here секвенция начинается со стыка, а не с границы диапазона
            start = self.timeline.start_frame()
            self.seek_frame(self.timeline.crop_start if start is None else start)

    def next_atlas_frame(self):
        if self.mode != "atlas":
            return

        order = self.timeline.atlas_order()
        if not order:
            self.stop_play()
            return

        self.current_marker_index = (self.current_marker_index + 1) % len(order)
        self.seek_frame(order[self.current_marker_index])

    def next_frame(self):
        if self.mode != "atlas":
            return

        current = self.timeline.current_frame
        next_f = current + 1
        if next_f > self.timeline.crop_end:
            next_f = self.timeline.crop_start
        self.timeline.set_current(next_f)
        self.seek_frame(next_f, update_timeline=False)

    def seek_frame(self, frame, update_timeline=True):
        img = self.atlas_loader.get_frame(frame)
        if img is not None:
            self.process_and_display(img)
            if update_timeline and self.mode == "atlas":
                self.timeline.current_frame = frame
                self.timeline.update()

    def seek_sprite(self, index):
        img = self.sprites_loader.get_frame(index)
        if img is not None:
            self.process_and_display(img)

    def get_keying_params(self):
        return (
            self.slider_gain.value() / 100.0,
            self.slider_soften.value() / 100.0,
            self.slider_sat.value() / 100.0,
            self.slider_val.value() / 100.0,
            self.slider_clip.value() / 50.0,
        )

    def frame_to_rgba(self, cv_image):
        if cv_image is None:
            return None
        if self.chk_keying.isChecked() and self.mode == "atlas":
            gain, soften, sat, val, clip = self.get_keying_params()
            source = ImageProcessor.to_bgr(cv_image)
            rgba = ImageProcessor.apply_chromakey(source, self.btn_key_color.color, gain, soften, sat, val, clip)
        else:
            rgba = ImageProcessor.source_to_rgba(cv_image)
        return self.transform_atlas_content(rgba)

    # --- Transform (Atlas) ------------------------------------------------
    def atlas_transform(self):
        """(сдвиг X, сдвиг Y в пикселях исходника, масштаб X, масштаб Y в процентах)."""
        return (self.spin_pos_x.value(), self.spin_pos_y.value(),
                self.spin_scale_x.value(), self.spin_scale_y.value())

    def has_atlas_transform(self):
        return self.atlas_transform() != (0, 0, 100.0, 100.0)

    def transform_atlas_content(self, image, border=(0, 0, 0, 0)):
        """Сдвиг и масштаб содержимого кадра вокруг центра; область кадра (crop) остаётся на месте."""
        pos_x, pos_y, scale_x, scale_y = self.atlas_transform()
        return ImageProcessor.transform_content(image, offset=(pos_x, pos_y),
                                                scale=(scale_x / 100.0, scale_y / 100.0), border=border)

    def _set_transform_spins(self, transform):
        pos_x, pos_y, scale_x, scale_y = transform
        for spin, value in ((self.spin_pos_x, int(round(pos_x))), (self.spin_pos_y, int(round(pos_y))),
                            (self.spin_scale_x, float(scale_x)), (self.spin_scale_y, float(scale_y))):
            spin.blockSignals(True)
            spin.setValue(value)
            spin.blockSignals(False)

    def set_atlas_transform(self, transform):
        """Выставляет Transform без сигналов — для undo и загрузки настроек."""
        self._set_transform_spins(transform)
        self._transform_prev = self.atlas_transform()
        self._last_transform_edit = 0.0

    def begin_transform_drag(self):
        self._transform_drag_before = self.atlas_transform()

    def on_transform_dragged(self, transform):
        """Рамку тянут во вьюпорте: поля и вьюпорт сразу, undo и превью атласа — когда отпустят."""
        if self._transform_drag_before is None:
            return
        self._set_transform_spins(transform)
        self.refresh_viewport()

    def end_transform_drag(self):
        before = self._transform_drag_before
        self._transform_drag_before = None
        if before is not None:
            # Весь жест — один шаг отмены
            self._apply_transform_change(before, group=False)

    def on_transform_edited(self, scale_axis=None):
        previous = self._transform_prev
        if scale_axis and self.btn_scale_link.isChecked():
            # Связанный масштаб: второе поле меняется в той же пропорции
            old_x, old_y = previous[2], previous[3]
            if scale_axis == "x":
                follower, value = self.spin_scale_y, old_y * self.spin_scale_x.value() / old_x
            else:
                follower, value = self.spin_scale_x, old_x * self.spin_scale_y.value() / old_y
            follower.blockSignals(True)
            follower.setValue(value)
            follower.blockSignals(False)
        self._apply_transform_change(previous)

    def reset_atlas_transform(self):
        previous = self._transform_prev
        self.set_atlas_transform((0, 0, 100.0, 100.0))
        self._apply_transform_change(previous, group=False)

    def _apply_transform_change(self, previous, group=True):
        current = self.atlas_transform()
        if current == previous:
            return
        self._transform_prev = current
        if self.atlas_loader.total_frames <= 0:
            return
        now = time.monotonic()
        # Серия правок подряд (колесо, стрелки спинбокса) — один шаг отмены
        if not group or now - self._last_transform_edit > 0.6:
            crop_start, crop_end, markers, start_key, _ = self.atlas_state()
            self.atlas_undo.push((crop_start, crop_end, markers, start_key, previous))
        self._last_transform_edit = now if group else 0.0
        self.refresh_viewport()
        self.preview_timer.start()

    def build_sprite_rgba(self, cv_image, idx=None):
        if cv_image is None:
            return None
        if idx is None:
            idx = self.sprite_index
        rgba = ImageProcessor.source_to_rgba(cv_image)
        fw = self.spin_width.value()
        fh = self.spin_height.value()
        resized = ImageProcessor.resize_frame(rgba, (fw, fh), crop_target=(fw, fh))
        offset_x, offset_y, scale = self.get_sprite_transform(idx)
        transformed = ImageProcessor.transform_rgba(
            resized,
            scale=scale,
            offset=(offset_x, offset_y),
        )
        return ImageProcessor.refine_alpha(
            transformed,
            erode_px=self.sprite_alpha_erode,
            blur_px=self.sprite_alpha_blur,
        )

    def on_alpha_refine_changed(self, *_):
        self.sprite_alpha_erode = float(self.spin_alpha_erode.value())
        self.sprite_alpha_blur = float(self.spin_alpha_blur.value())
        if self.mode != "sprites":
            return
        self.refresh_viewport()
        self.update_sprites_preview()

    def build_sprite_shadow_alpha(self, image_rgba):
        if not self.chk_drop_shadow.isChecked():
            return None
        if image_rgba is None or len(image_rgba.shape) < 3 or image_rgba.shape[2] != 4:
            return None

        alpha = image_rgba[:, :, 3]
        if not np.any(alpha):
            return None

        shadow = np.zeros_like(image_rgba)
        shadow[:, :, 3] = alpha
        shadow = ImageProcessor.transform_rgba(
            shadow,
            offset=(self.spin_shadow_x.value(), self.spin_shadow_y.value()),
        )
        shadow_alpha = shadow[:, :, 3]
        softness = self.spin_shadow_softness.value()
        if softness > 0:
            kernel = softness * 2 + 1
            shadow_alpha = cv2.GaussianBlur(shadow_alpha, (kernel, kernel), 0)
        opacity = self.spin_shadow_opacity.value() / 100.0
        if opacity <= 0:
            return None
        return shadow_alpha.astype(np.float32) * opacity / 255.0

    def composite_sprite_image(self, image_rgba):
        if image_rgba is None:
            return None
        if len(image_rgba.shape) < 3 or image_rgba.shape[2] != 4:
            return image_rgba

        add_bg = self.chk_add_bg.isChecked()
        shadow_alpha = self.build_sprite_shadow_alpha(image_rgba)
        sprite_rgb = image_rgba[:, :, :3].astype(np.float32)
        sprite_alpha = image_rgba[:, :, 3].astype(np.float32)[:, :, np.newaxis] / 255.0
        if shadow_alpha is None and not add_bg:
            return image_rgba

        if shadow_alpha is None:
            return ImageProcessor.composite_on_background(image_rgba, self.btn_bg_color.color)

        shadow_alpha = shadow_alpha[:, :, np.newaxis]
        shadow_rgb = np.empty_like(sprite_rgb)
        shadow_rgb[:] = self.btn_shadow_color.color

        if add_bg:
            bg_rgb = np.empty_like(sprite_rgb)
            bg_rgb[:] = self.btn_bg_color.color
            under_rgb = shadow_rgb * shadow_alpha + bg_rgb * (1.0 - shadow_alpha)
            out_rgb = sprite_rgb * sprite_alpha + under_rgb * (1.0 - sprite_alpha)
            return out_rgb.astype(np.uint8)

        shadow_under_alpha = shadow_alpha * (1.0 - sprite_alpha)
        out_alpha = sprite_alpha + shadow_under_alpha
        out_rgb = sprite_rgb * sprite_alpha + shadow_rgb * shadow_under_alpha
        safe_alpha = np.where(out_alpha > 0, out_alpha, 1.0)
        out_rgb = np.where(out_alpha > 0, out_rgb / safe_alpha, 0.0)
        return np.dstack((out_rgb, out_alpha * 255.0)).astype(np.uint8)

    def process_sprite_image(self, cv_image, idx=None):
        if cv_image is None:
            return None
        transformed = self.build_sprite_rgba(cv_image, idx)
        return self.composite_sprite_image(transformed)

    def viewport_keyed_frame(self, cv_image, source):
        """Keying и композит на фон для вьюпорта, с кэшем на текущий кадр.

        Пока тянут рамку Transform, кадр и параметры не меняются — дорогой keying считается один раз.
        """
        gain, soften, sat, val, clip = self.get_keying_params()
        key = (self.btn_key_color.color, gain, soften, sat, val, clip, self.btn_bg_color.color)
        cache = self._keyed_cache
        if cache is None or cache[0] is not cv_image or cache[1] != key:
            keyed = ImageProcessor.apply_chromakey(source, self.btn_key_color.color, gain, soften, sat, val, clip)
            composite = ImageProcessor.composite_on_background(keyed, self.btn_bg_color.color)
            # Держим ссылку на сам кадр: проверка через `is` не спутает его с новым массивом
            cache = self._keyed_cache = (cv_image, key, keyed, composite)
        return cache[2], cache[3]

    def process_and_display(self, cv_image):
        self.update_crop_rect(cv_image)

        if self.mode == "sprites":
            self.viewport.set_transform_frame(None)
            transformed = self.build_sprite_rgba(cv_image, self.sprite_index)
            sprite_rgb = self.composite_sprite_image(transformed)
            if sprite_rgb is not None:
                self.viewport.set_image(sprite_rgb)
                mask = None
                if transformed is not None and len(transformed.shape) > 2 and transformed.shape[2] == 4:
                    mask = transformed[:, :, 3]
                self.viewport.set_sprite_interaction(True, mask)
            return

        source = ImageProcessor.to_bgr(cv_image)
        if source is None:
            return

        # Рамка Transform во вьюпорте показывает текущие сдвиг и масштаб
        self.viewport.set_transform_frame(self.atlas_transform())

        if self.chk_keying.isChecked():
            keyed, composite = self.viewport_keyed_frame(cv_image, source)
            if self.chk_mask.isChecked():
                a = self.transform_atlas_content(cv2.extractChannel(keyed, 3))
                self.viewport.set_image(cv2.merge([a, a, a]))
            else:
                # Композит на фон уже готов — двигаем его, освободившиеся поля заливаем цветом фона
                self.viewport.set_image(self.transform_atlas_content(composite, border=self.btn_bg_color.color))
            return

        if self.has_atlas_transform():
            # Освободившиеся после Transform поля прозрачные — вьюпорт покажет их шахматкой
            rgba = cv2.cvtColor(source, cv2.COLOR_BGR2RGBA)
            self.viewport.set_image(self.transform_atlas_content(rgba))
            return

        self.viewport.set_image(cv2.cvtColor(source, cv2.COLOR_BGR2RGB))

    def update_crop_rect(self, image):
        if image is None:
            return
        h, w = image.shape[:2]

        target_w = self.spin_width.value()
        target_h = self.spin_height.value()

        img_aspect = w / h
        target_aspect = target_w / target_h

        if abs(img_aspect - target_aspect) < 0.01:
            # Кадр целиком; с Transform рамку всё равно рисуем — видно, куда двигается содержимое
            show_frame = self.mode == "atlas" and self.has_atlas_transform()
            self.viewport.set_crop_rect((0, 0, w, h) if show_frame else None)
            return

        if img_aspect > target_aspect:
            ch = h
            cw = int(h * target_aspect)
        else:
            cw = w
            ch = int(w / target_aspect)

        cx = (w - cw) // 2
        cy = (h - ch) // 2

        self.viewport.set_crop_rect((cx, cy, cw, ch))

    def refresh_viewport(self):
        if self.mode == "atlas":
            if self.atlas_loader.total_frames <= 0:
                self.clear_viewport()
                return
            self.seek_frame(self.timeline.current_frame)
        else:
            if self.sprites_loader.total_frames <= 0:
                self.clear_viewport()
                return
            self.seek_sprite(self.sprite_index)

    def clear_viewport(self):
        """Пустой вьюпорт показывает подсказку с зоной перетаскивания."""
        self.viewport.set_crop_rect(None)
        self.viewport.set_transform_frame(None)
        self.viewport.set_image(None)

    def on_frame_size_changed(self):
        self.refresh_viewport()
        if self.mode == "atlas":
            self.update_atlas_preview()
        else:
            self.update_sprites_preview()

    def normalize_rgb(self, color):
        if not isinstance(color, (list, tuple)) or len(color) < 3:
            return None
        try:
            r = int(color[0])
            g = int(color[1])
            b = int(color[2])
        except (TypeError, ValueError):
            return None
        r = max(0, min(r, 255))
        g = max(0, min(g, 255))
        b = max(0, min(b, 255))
        return (r, g, b)

    def add_bg_preset(self, color):
        rgb = self.normalize_rgb(color)
        if rgb is None:
            return
        filtered = [c for c in self.bg_color_history if c != rgb]
        self.bg_color_history = [rgb] + filtered
        self.bg_color_history = self.bg_color_history[:6]
        self.update_bg_preset_buttons()

    def set_bg_presets(self, presets):
        colors = []
        for color in presets:
            rgb = self.normalize_rgb(color)
            if rgb is None or rgb in colors:
                continue
            colors.append(rgb)
            if len(colors) >= 6:
                break
        if not colors:
            colors = [self.btn_bg_color.color]
        self.bg_color_history = colors
        self.update_bg_preset_buttons()

    def update_bg_preset_buttons(self):
        for i, btn in enumerate(self.bg_preset_buttons):
            if i < len(self.bg_color_history):
                r, g, b = self.bg_color_history[i]
                btn.setStyleSheet(
                    f"background-color: rgb({r}, {g}, {b}); border: 1px solid #222;"
                )
                btn.setEnabled(True)
            else:
                btn.setStyleSheet("background-color: #666; border: 1px solid #222;")
                btn.setEnabled(False)

    def apply_bg_preset(self, index):
        if index < 0 or index >= len(self.bg_color_history):
            return
        self.btn_bg_color.set_color(self.bg_color_history[index])

    def set_bg_color(self, color):
        self.add_bg_preset(color)
        self.refresh_viewport()
        if self.mode == "atlas":
            self.update_atlas_preview()
        else:
            self.update_sprites_preview()

    def set_key_color(self, color):
        self.refresh_viewport()
        if self.mode == "atlas":
            self.update_atlas_preview()

    def update_shadow_controls(self):
        enabled = self.chk_drop_shadow.isChecked()
        for widget in (
            self.spin_shadow_x,
            self.spin_shadow_y,
            self.spin_shadow_softness,
            self.spin_shadow_opacity,
            self.btn_shadow_color,
        ):
            widget.setEnabled(enabled)

    def update_bg_controls(self):
        enabled = self.mode == "atlas" or self.chk_add_bg.isChecked()
        self.btn_bg_color.setEnabled(enabled)
        self.update_bg_preset_buttons()
        if not enabled:
            for btn in self.bg_preset_buttons:
                btn.setEnabled(False)

    def on_shadow_settings_changed(self, *_):
        self.sprite_shadow_enabled = self.chk_drop_shadow.isChecked()
        self.sprite_shadow_x = self.spin_shadow_x.value()
        self.sprite_shadow_y = self.spin_shadow_y.value()
        self.sprite_shadow_softness = self.spin_shadow_softness.value()
        self.sprite_shadow_opacity = self.spin_shadow_opacity.value()
        self.sprite_shadow_color = self.btn_shadow_color.color
        self.update_shadow_controls()
        if self.mode != "sprites":
            return
        self.refresh_viewport()
        self.update_sprites_preview()

    def on_bg_settings_changed(self, *_):
        self.sprite_add_bg = self.chk_add_bg.isChecked()
        self.update_bg_controls()
        if self.mode != "sprites":
            return
        self.refresh_viewport()
        self.update_sprites_preview()

    def start_color_picking(self):
        self.viewport.picking_mode = True
        self.viewport.setCursor(Qt.CrossCursor)

    def on_crop_changed(self, start, end):
        """Границы тянут мышью — таймлайн сам масштабирует ключи вместе с диапазоном, превью с задержкой."""
        if self.atlas_loader.total_frames > 0:
            self.preview_timer.start()

    def on_atlas_grid_changed(self, *_):
        """C/R поменялись: Frames становится C × R, чтобы сетка сразу была заполнена."""
        frames = self.spin_cols.value() * self.spin_rows.value()
        if self.spin_frames.value() != frames:
            # Дальше через on_frames_count_changed(): новая раскладка ключей и шаг undo
            self.spin_frames.setValue(frames)
        if self.atlas_loader.total_frames <= 0:
            return
        self.preview_timer.start()

    def on_frames_count_changed(self, *_):
        """Frames поменялся: число ключей другое, раскладываем их заново (с undo)."""
        if self.atlas_loader.total_frames <= 0:
            return
        before = self.atlas_state()
        self.rebuild_markers()
        if self.atlas_state() != before:
            self.atlas_undo.push(before)
        self.preview_timer.start()

    def refresh_atlas(self):
        """Кнопка Refresh Atlas: перерисовать превью по текущим ключам и настройкам keying."""
        self.preview_timer.stop()
        self.update_atlas_preview()

    def update_atlas_markers(self):
        self.rebuild_markers()
        self.update_atlas_preview()

    def rebuild_markers(self):
        if self.atlas_loader.total_frames <= 0:
            return

        start = self.timeline.crop_start
        end = self.timeline.crop_end
        count = self.spin_frames.value()

        if count < 2:
            markers = [start]
        else:
            duration = end - start
            if duration <= 0:
                duration = 1
            step = duration / (count - 1) if count > 1 else 0
            markers = [int(start + i * step) for i in range(count)]

        # Стык Start Here переживает пересчёт: переезжает на ближайший новый ключ
        start_frame = self.timeline.start_frame()
        self.timeline.set_markers(markers)
        if start_frame is not None:
            self.timeline.set_start_near(start_frame)

    def on_markers_edited(self, markers):
        """Ключи поправили вручную (перетаскивание, меню, Start Here) — пересобираем превью атласа."""
        if self.atlas_loader.total_frames <= 0:
            return
        # Delete/Duplicate меняют число ключей
        self.sync_frames_spin()
        if self.timeline.markers:
            self.current_marker_index = min(self.current_marker_index, len(self.timeline.markers) - 1)
        self.update_atlas_preview()

    def on_atlas_cell_clicked(self, index, frame):
        """Клик по ячейке превью атласа переносит курсор таймлайна на её кадр."""
        if self.mode != "atlas" or self.atlas_loader.total_frames <= 0:
            return
        # Play Frames продолжит с этой ячейки
        self.current_marker_index = index
        self.seek_frame(frame)

    def update_atlas_preview(self):
        if self.atlas_loader.total_frames <= 0:
            return

        markers = self.timeline.atlas_order()
        cols = self.spin_cols.value()
        rows = self.spin_rows.value()

        target_w = self.spin_width.value()
        target_h = self.spin_height.value()

        pfw = target_w
        pfh = target_h

        frames = []

        for idx in markers:
            if len(frames) >= cols * rows:
                break
            raw = self.atlas_loader.get_frame(idx)
            if raw is not None:
                rgba = self.frame_to_rgba(raw)
                resized = ImageProcessor.resize_frame(rgba, (pfw, pfh), crop_target=(target_w, target_h))
                frames.append(resized)
            else:
                frames.append(np.zeros((pfh, pfw, 4), dtype=np.uint8))

        atlas = AtlasBuilder.create_atlas(frames, cols, rows, pfw, pfh)

        h, w, ch = atlas.shape
        qimg = QImage(atlas.data, w, h, ch * w, QImage.Format_RGBA8888)

        self.atlas_viewer.set_image(qimg)
        self.atlas_viewer.set_cells(cols, rows, pfw, pfh, markers[:cols * rows])

    def update_sprites_preview(self):
        if self.sprites_loader.total_frames <= 0:
            return

        target_w = self.spin_width.value()
        target_h = self.spin_height.value()

        count = self.sprites_loader.total_frames
        cols = int(np.ceil(np.sqrt(count)))
        rows = int(np.ceil(count / cols))

        frames = []
        for idx in range(count):
            raw = self.sprites_loader.get_frame(idx)
            if raw is None:
                frames.append(np.zeros((target_h, target_w, 4), dtype=np.uint8))
                continue

            image = self.process_sprite_image(raw, idx)
            if image is None:
                frames.append(np.zeros((target_h, target_w, 4), dtype=np.uint8))
                continue

            if len(image.shape) > 2 and image.shape[2] == 4:
                rgba = image
            else:
                rgba = cv2.cvtColor(image, cv2.COLOR_RGB2RGBA)
            frames.append(rgba)

        grid = AtlasBuilder.create_atlas(frames, cols, rows, target_w, target_h)

        # Preview separators between files
        line_size = 2
        if cols > 1:
            for col in range(1, cols):
                x = col * target_w
                grid[:, max(0, x - line_size // 2):min(grid.shape[1], x + (line_size - line_size // 2)), :] = [0, 0, 0, 255]
        if rows > 1:
            for row in range(1, rows):
                y = row * target_h
                grid[max(0, y - line_size // 2):min(grid.shape[0], y + (line_size - line_size // 2)), :, :] = [0, 0, 0, 255]

        h, w, ch = grid.shape
        qimg = QImage(grid.data, w, h, ch * w, QImage.Format_RGBA8888)
        self.sprites_viewer.set_image(qimg)

    def export_atlas(self):
        if self.atlas_loader.total_frames <= 0:
            return

        path, _ = QFileDialog.getSaveFileName(self, "Export Atlas", "atlas.png", "PNG Image (*.png)")
        if not path:
            return

        markers = self.timeline.atlas_order()
        frames = []

        fw = self.spin_width.value()
        fh = self.spin_height.value()

        for idx in markers:
            raw = self.atlas_loader.get_frame(idx)
            if raw is not None:
                rgba = self.frame_to_rgba(raw)
                resized = ImageProcessor.resize_frame(rgba, (fw, fh), crop_target=(fw, fh))
                frames.append(resized)
            else:
                frames.append(np.zeros((fh, fw, 4), dtype=np.uint8))

        cols = self.spin_cols.value()
        rows = self.spin_rows.value()

        atlas = AtlasBuilder.create_atlas(frames, cols, rows, fw, fh)

        atlas_bgra = cv2.cvtColor(atlas, cv2.COLOR_RGBA2BGRA)
        source_type = self.atlas_loader.get_metadata()["source_type"]

        meta = {
            "videoFile": self.atlas_loader.file_path,
            "sourceFile": self.atlas_loader.file_path,
            "sourceType": source_type,
            "frameWidth": fw,
            "frameHeight": fh,
            "columns": cols,
            "rows": rows,
            "framesCount": len(markers),
            "fps": self.atlas_loader.fps,
            "cropStart": self.timeline.crop_start,
            "cropEnd": self.timeline.crop_end,
            "frameIndices": markers,
        }

        json_path = os.path.splitext(path)[0] + ".json"
        try:
            # PNG первым: не записался — json не создаём и об успехе не рапортуем
            write_image(path, atlas_bgra)
            with open(json_path, "w") as f:
                json.dump(meta, f, indent=4)
        except Exception as e:
            QMessageBox.critical(self, "Export Error", f"Failed to export atlas:\n{e}")
            return

        QMessageBox.information(self, "Export", "Atlas exported successfully!")

    def export_video(self):
        if self.atlas_loader.total_frames <= 0:
            return

        fps = self.atlas_loader.fps if self.atlas_loader.fps > 0 else 30.0
        dlg = ExportVideoDialog(fps, self)
        if dlg.exec() != QDialog.Accepted:
            return

        settings = dlg.get_settings()
        markers = self.timeline.atlas_order()
        fw = self.spin_width.value()
        fh = self.spin_height.value()

        if dlg.is_sequence():
            folder = QFileDialog.getExistingDirectory(self, "Export PNG Sequence Folder")
            if not folder:
                return
            try:
                exported = 0
                for i, idx in enumerate(markers):
                    raw = self.atlas_loader.get_frame(idx)
                    if raw is None:
                        continue
                    rgba = self.frame_to_rgba(raw)
                    resized = ImageProcessor.resize_frame(rgba, (fw, fh), crop_target=(fw, fh))
                    bgra = cv2.cvtColor(resized, cv2.COLOR_RGBA2BGRA)
                    out_path = os.path.join(folder, f"frame_{i:04d}.png")
                    write_image(out_path, bgra)
                    exported += 1
                QMessageBox.information(self, "Export", f"Exported {exported} frames as PNG sequence.")
            except Exception as e:
                QMessageBox.critical(self, "Export Error", str(e))
            return

        path, _ = QFileDialog.getSaveFileName(
            self, "Export Video", f"output{settings['ext']}", settings["filter"])
        if not path:
            return

        try:
            container = av.open(path, mode="w")
            stream = container.add_stream(settings["codec"], rate=settings["fps"])
            stream.width = fw
            stream.height = fh
            stream.pix_fmt = settings["pix_fmt"]
            stream.bit_rate = settings["bitrate"]

            for idx in markers:
                raw = self.atlas_loader.get_frame(idx)
                if raw is None:
                    continue

                rgba = self.frame_to_rgba(raw)
                resized = ImageProcessor.resize_frame(rgba, (fw, fh), crop_target=(fw, fh))

                if resized.shape[2] == 4:
                    rgb = cv2.cvtColor(resized, cv2.COLOR_RGBA2RGB)
                else:
                    rgb = resized

                frame = av.VideoFrame.from_ndarray(rgb, format="rgb24")
                for packet in stream.encode(frame):
                    container.mux(packet)

            for packet in stream.encode():
                container.mux(packet)

            container.close()
            QMessageBox.information(self, "Export", "Video exported successfully!")
        except Exception as e:
            QMessageBox.critical(self, "Export Error", str(e))

    def export_sprites(self):
        if self.sprites_loader.total_frames <= 0:
            return

        folder = QFileDialog.getExistingDirectory(self, "Export Images Folder")
        if not folder:
            return

        exported = 0
        for idx, src in enumerate(self.sprites_loader.image_sequence):
            raw = self.sprites_loader.get_frame(idx)
            if raw is None:
                continue

            image = self.process_sprite_image(raw, idx)
            if image is None:
                continue

            if len(image.shape) > 2 and image.shape[2] == 4:
                bgr = cv2.cvtColor(image, cv2.COLOR_RGBA2BGRA)
            else:
                bgr = cv2.cvtColor(image, cv2.COLOR_RGB2BGR)
            number = f"{idx + 1:04d}"
            if self.chk_rename.isChecked():
                prefix = self.edit_prefix.text()
                base = f"{prefix}{number}"
            else:
                source_name = os.path.splitext(os.path.basename(src))[0]
                base = f"{source_name}_{number}"
            out_path = os.path.join(folder, f"{base}.png")
            try:
                write_image(out_path, bgr)
            except Exception as e:
                QMessageBox.critical(self, "Export Error", f"Exported {exported} images, then failed:\n{e}")
                return
            exported += 1

        QMessageBox.information(self, "Export", f"Exported {exported} images.")

    def export_current(self):
        if self.mode == "atlas":
            self.export_atlas()
        else:
            self.export_sprites()

    def save_settings(self):
        path, _ = QFileDialog.getSaveFileName(self, "Save Settings", "settings.json", "JSON Files (*.json)")
        if not path:
            return

        settings = {
            "keying": {
                "gain": self.slider_gain.value(),
                "soften": self.slider_soften.value(),
                "clip": self.slider_clip.value(),
                "sat": self.slider_sat.value(),
                "val": self.slider_val.value()
            },
            "colors": {
                "bg": self.btn_bg_color.color,
                "key": self.btn_key_color.color,
                "bgPresets": self.bg_color_history,
            },
            "atlas": {
                "width": self.spin_width.value(),
                "height": self.spin_height.value(),
                "cols": self.spin_cols.value(),
                "rows": self.spin_rows.value(),
                "frames": self.spin_frames.value(),
                "transform": {
                    "x": self.spin_pos_x.value(),
                    "y": self.spin_pos_y.value(),
                    "scaleX": self.spin_scale_x.value(),
                    "scaleY": self.spin_scale_y.value(),
                    "linkScale": self.btn_scale_link.isChecked(),
                },
            },
            "sprites": {
                "suffix": self.sprite_suffix,
                "rename": self.chk_rename.isChecked(),
                "prefix": self.edit_prefix.text(),
                "addBg": self.chk_add_bg.isChecked(),
                "dropShadow": {
                    "enabled": self.chk_drop_shadow.isChecked(),
                    "x": self.spin_shadow_x.value(),
                    "y": self.spin_shadow_y.value(),
                    "softness": self.spin_shadow_softness.value(),
                    "opacity": self.spin_shadow_opacity.value(),
                    "color": self.btn_shadow_color.color,
                },
                "alphaRefine": {
                    "erode": self.spin_alpha_erode.value(),
                    "blur": self.spin_alpha_blur.value(),
                },
            },
        }

        try:
            with open(path, "w") as f:
                json.dump(settings, f, indent=4)
            QMessageBox.information(self, "Save Settings", "Settings saved successfully!")
        except Exception as e:
            QMessageBox.warning(self, "Error", f"Failed to save settings: {e}")

    def load_settings(self):
        path, _ = QFileDialog.getOpenFileName(self, "Load Settings", "", "JSON Files (*.json)")
        if not path:
            return

        try:
            with open(path, "r") as f:
                settings = json.load(f)

            if "keying" in settings:
                k = settings["keying"]
                self.slider_gain.setValue(k.get("gain", 20))
                self.slider_soften.setValue(k.get("soften", 10))
                self.slider_clip.setValue(k.get("clip", 0))
                self.slider_sat.setValue(k.get("sat", 15))
                self.slider_val.setValue(k.get("val", 15))

            if "colors" in settings:
                c = settings["colors"]
                if "bg" in c:
                    self.btn_bg_color.set_color(tuple(c["bg"]))
                if "key" in c:
                    self.btn_key_color.set_color(tuple(c["key"]))
                if "bgPresets" in c:
                    self.set_bg_presets(c["bgPresets"])
                else:
                    self.add_bg_preset(self.btn_bg_color.color)

            if "atlas" in settings:
                a = settings["atlas"]
                self.spin_width.setValue(a.get("width", 512))
                self.spin_height.setValue(a.get("height", 512))
                self.spin_cols.setValue(a.get("cols", 4))
                self.spin_rows.setValue(a.get("rows", 4))
                self.spin_frames.setValue(a.get("frames", 16))
                t = a.get("transform", {})
                self.btn_scale_link.setChecked(bool(t.get("linkScale", True)))
                self.set_atlas_transform((
                    int(t.get("x", 0)),
                    int(t.get("y", 0)),
                    float(t.get("scaleX", 100.0)),
                    float(t.get("scaleY", 100.0)),
                ))

            if "sprites" in settings:
                s = settings["sprites"]
                self.sprite_suffix = s.get("suffix", "_bg")
                self.chk_rename.setChecked(s.get("rename", False))
                self.edit_prefix.setText(s.get("prefix", ""))
                self.chk_add_bg.setChecked(s.get("addBg", False))
                shadow = s.get("dropShadow", {})
                self.chk_drop_shadow.setChecked(shadow.get("enabled", False))
                self.spin_shadow_x.setValue(shadow.get("x", self.sprite_shadow_x))
                self.spin_shadow_y.setValue(shadow.get("y", self.sprite_shadow_y))
                self.spin_shadow_softness.setValue(shadow.get("softness", self.sprite_shadow_softness))
                self.spin_shadow_opacity.setValue(shadow.get("opacity", self.sprite_shadow_opacity))
                color = shadow.get("color", self.sprite_shadow_color)
                if isinstance(color, (list, tuple)) and len(color) >= 3:
                    self.btn_shadow_color.set_color(tuple(color[:3]))
                alpha_refine = s.get("alphaRefine", {})
                self.spin_alpha_erode.setValue(float(alpha_refine.get("erode", 0.0)))
                self.spin_alpha_blur.setValue(float(alpha_refine.get("blur", 0.0)))
                self.update_shadow_controls()
                self.update_bg_controls()

            QMessageBox.information(self, "Load Settings", "Settings loaded successfully!")
            self.refresh_viewport()
            if self.mode == "atlas":
                self.update_atlas_preview()
            else:
                self.update_sprites_preview()
        except Exception as e:
            QMessageBox.warning(self, "Error", f"Failed to load settings: {e}")

    def update_gain_label(self):
        self.lbl_gain_value.setText(f"{self.slider_gain.value()}%")

    def update_soften_label(self):
        self.lbl_soften_value.setText(f"{self.slider_soften.value()}%")

    def update_clip_label(self):
        self.lbl_clip_value.setText(f"{self.slider_clip.value()}")

    def update_sat_label(self):
        self.lbl_sat_value.setText(f"{self.slider_sat.value()}%")

    def update_val_label(self):
        self.lbl_val_value.setText(f"{self.slider_val.value()}%")
