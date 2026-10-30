import ctypes
from ctypes import wintypes
import html
import math
import re
import time
from typing import Callable, Optional

from PySide6.QtCore import QPoint, QRect, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QColor, QCursor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

import win32con
import win32gui

from config import load_config
from extractor import (
    SelectionContext,
    capture_selection,
    has_word_selection_fast,
    is_word_foreground,
)
from llm_service import TranslationResult, process_selection
from storage import VocabStorage


def _set_no_activate(hwnd: int) -> None:
    """设置窗口为 WS_EX_NOACTIVATE，点击悬浮图标时绝不抢夺原窗口（如 Word）的焦点与选区。"""
    try:
        ex_style = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
        win32gui.SetWindowLong(
            hwnd,
            win32con.GWL_EXSTYLE,
            ex_style | win32con.WS_EX_NOACTIVATE | win32con.WS_EX_TOOLWINDOW,
        )
    except Exception:
        pass


class TranslationWorker(QThread):
    """后台异步执行 AI 查询与数据库存储（或纠错分析），避免阻塞界面。"""

    finished_result = Signal(object)

    def __init__(
        self,
        ctx: SelectionContext,
        storage: VocabStorage,
        cfg: dict,
        parent: Optional[QWidget] = None,
        mode: str = "translate",
    ) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self.storage = storage
        self.cfg = cfg
        self.mode = mode

    def run(self) -> None:
        result = process_selection(self.ctx, self.storage, self.cfg, mode=self.mode)
        self.finished_result.emit(result)


class FloatingTIcon(QWidget):
    """
    划词后浮现的双按钮半透明悬浮框：
    - 左侧（原蓝灰半透明框 'T'）：点击触发查词翻译与生词入库；
    - 右侧（橙色半透明框 'C'）：点击触发纠错模式（自动识别所在句子、判断是否合适并给出可替代词，不入单词本）。
    """

    clicked = Signal(QPoint)
    correction_clicked = Signal(QPoint)

    def __init__(self) -> None:
        super().__init__(
            None,
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowDoesNotAcceptFocus,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setMouseTracking(True)
        self.setFixedSize(74, 36)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._hovered = False
        self._hovered_part: Optional[str] = None  # 'translate' | 'correction' | None
        self._base_opacity = 0.78

        self._left_rect = QRect(2, 2, 32, 32)
        self._right_rect = QRect(40, 2, 32, 32)

        self._auto_hide_timer = QTimer(self)
        self._auto_hide_timer.setSingleShot(True)
        self._auto_hide_timer.timeout.connect(self.hide)

    def show_near(self, global_pos: QPoint, opacity: float = 0.78) -> None:
        self._base_opacity = max(0.35, min(0.95, opacity))
        self._hovered_part = None
        self.setWindowOpacity(self._base_opacity)
        # 偏移到鼠标右下方 14px，不遮挡正选中的文字
        target = QPoint(global_pos.x() + 14, global_pos.y() + 14)
        self.move(target)
        self.show()
        _set_no_activate(int(self.winId()))
        self._auto_hide_timer.start(4500)
        self.update()

    def enterEvent(self, event) -> None:
        self._hovered = True
        self.setWindowOpacity(0.98)
        self._auto_hide_timer.stop()
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._hovered = False
        self._hovered_part = None
        self.setWindowOpacity(self._base_opacity)
        self._auto_hide_timer.start(2500)
        self.update()
        super().leaveEvent(event)

    def mouseMoveEvent(self, event) -> None:
        pt = event.position().toPoint()
        if self._right_rect.contains(pt):
            new_part = "correction"
        elif self._left_rect.contains(pt):
            new_part = "translate"
        else:
            new_part = None
        if new_part != self._hovered_part:
            self._hovered_part = new_part
            self.update()
        super().mouseMoveEvent(event)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            pt = event.position().toPoint()
            pos = self.pos()
            self.hide()
            if self._right_rect.contains(pt) or pt.x() >= 37:
                self.correction_clicked.emit(pos)
            else:
                self.clicked.emit(pos)
            event.accept()
            return
        super().mousePressEvent(event)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        font = QFont("Georgia", 15, QFont.Weight.Bold)
        painter.setFont(font)

        # 1. 左侧：原版蓝灰半透明框 ('T' 翻译模式)
        left_path = QPainterPath()
        left_path.addRoundedRect(self._left_rect, 9, 9)
        left_hover = self._hovered and (self._hovered_part in (None, "translate"))
        left_bg = QColor(74, 98, 124, 235) if left_hover else QColor(56, 74, 96, 210)
        painter.fillPath(left_path, left_bg)
        painter.setPen(QPen(QColor(156, 176, 198, 180), 1.4))
        painter.drawPath(left_path)
        painter.setPen(QColor(248, 250, 252))
        painter.drawText(self._left_rect, Qt.AlignmentFlag.AlignCenter, "T")

        # 2. 右侧：橙色半透明框 ('C' 纠错模式)
        right_path = QPainterPath()
        right_path.addRoundedRect(self._right_rect, 9, 9)
        right_hover = self._hovered and (self._hovered_part == "correction")
        right_bg = (
            QColor(235, 132, 52, 238) if right_hover else QColor(214, 112, 38, 210)
        )
        painter.fillPath(right_path, right_bg)
        painter.setPen(QPen(QColor(248, 194, 145, 190), 1.4))
        painter.drawPath(right_path)
        painter.setPen(QColor(255, 250, 245))
        painter.drawText(self._right_rect, Qt.AlignmentFlag.AlignCenter, "C")


class TranslationPopup(QWidget):
    """点击 'T' 图标后展示的翻译结果悬浮卡片。"""

    vocab_updated = Signal()

    def __init__(self, on_open_review: Optional[Callable[[], None]] = None) -> None:
        super().__init__(
            None,
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.on_open_review = on_open_review
        self._drag_offset: Optional[QPoint] = None

        self.setMinimumWidth(380)
        self.setMaximumWidth(480)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 8, 8, 8)

        self.card = QFrame(self)
        self.card.setObjectName("PopupCard")
        self.card.setStyleSheet(
            """
            QFrame#PopupCard {
                background-color: #F7F8FA;
                border: 1.5px solid #9CB0C6;
                border-radius: 12px;
            }
            QLabel {
                color: #27303F;
                font-family: 'Segoe UI', 'Microsoft YaHei UI', sans-serif;
            }
            QPushButton#ActionBtn {
                background-color: #E6ECF2;
                color: #3E5876;
                border: 1px solid #B8C7D9;
                border-radius: 6px;
                padding: 3px 10px;
                font-size: 12px;
            }
            QPushButton#ActionBtn:hover {
                background-color: #D8E2ED;
            }
            QPushButton#CloseBtn {
                background: transparent;
                color: #64748B;
                border: none;
                font-size: 15px;
                font-weight: bold;
                padding: 2px 6px;
            }
            QPushButton#CloseBtn:hover {
                color: #824646;
            }
            """
        )
        outer.addWidget(self.card)

        card_layout = QVBoxLayout(self.card)
        card_layout.setContentsMargins(16, 12, 16, 14)
        card_layout.setSpacing(8)

        # 顶部标题栏
        top_bar = QHBoxLayout()
        self.title_lbl = QLabel("WTranslator")
        self.title_lbl.setStyleSheet("font-size: 18px; font-weight: bold; color: #1E2530;")
        self.title_lbl.setWordWrap(True)
        top_bar.addWidget(self.title_lbl, 1)

        self.badge_lbl = QLabel("")
        self.badge_lbl.setStyleSheet(
            "font-size: 11px; padding: 2px 8px; border-radius: 5px; background: #E1E8F0; color: #3E5876;"
        )
        top_bar.addWidget(self.badge_lbl)

        if self.on_open_review:
            review_btn = QPushButton("复习本")
            review_btn.setObjectName("ActionBtn")
            review_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            review_btn.clicked.connect(self.on_open_review)
            top_bar.addWidget(review_btn)

        close_btn = QPushButton("×")
        close_btn.setObjectName("CloseBtn")
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.clicked.connect(self.hide)
        top_bar.addWidget(close_btn)

        card_layout.addLayout(top_bar)

        # 音标行
        self.phonetic_lbl = QLabel("")
        self.phonetic_lbl.setStyleSheet("font-size: 13px; color: #526071;")
        card_layout.addWidget(self.phonetic_lbl)

        # 超出句子词数上限提示框
        self.warning_lbl = QLabel("")
        self.warning_lbl.setWordWrap(True)
        self.warning_lbl.setStyleSheet(
            "background-color: #EFECE4; color: #6E5931; border: 1px solid #B0935A; "
            "border-radius: 6px; padding: 6px 10px; font-size: 12px;"
        )
        self.warning_lbl.hide()
        card_layout.addWidget(self.warning_lbl)

        # 释义与语境正文区
        self.body_lbl = QLabel("")
        self.body_lbl.setWordWrap(True)
        self.body_lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.body_lbl.setStyleSheet("font-size: 13px; line-height: 1.5;")
        card_layout.addWidget(self.body_lbl)

    def _apply_card_theme(self, is_correction: bool) -> None:
        border_col = "#D8965E" if is_correction else "#9CB0C6"
        bg_col = "#FDF9F5" if is_correction else "#F7F8FA"
        self.card.setStyleSheet(
            f"""
            QFrame#PopupCard {{
                background-color: {bg_col};
                border: 1.5px solid {border_col};
                border-radius: 12px;
            }}
            QLabel {{
                color: #27303F;
                font-family: 'Segoe UI', 'Microsoft YaHei UI', sans-serif;
            }}
            QPushButton#ActionBtn {{
                background-color: #E6ECF2;
                color: #3E5876;
                border: 1px solid #B8C7D9;
                border-radius: 6px;
                padding: 3px 10px;
                font-size: 12px;
            }}
            QPushButton#ActionBtn:hover {{
                background-color: #D8E2ED;
            }}
            QPushButton#CloseBtn {{
                background: transparent;
                color: #64748B;
                border: none;
                font-size: 15px;
                font-weight: bold;
                padding: 2px 6px;
            }}
            QPushButton#CloseBtn:hover {{
                color: #824646;
            }}
            """
        )

    def show_loading(
        self, pos: QPoint, ctx: SelectionContext, mode: str = "translate"
    ) -> None:
        is_corr = mode == "correction"
        self._apply_card_theme(is_corr)
        self.title_lbl.setText(ctx.selected_text[:60])

        if is_corr:
            self.badge_lbl.setText("纠错模式 (不入库)")
            self.badge_lbl.setStyleSheet(
                "font-size: 11px; font-weight: bold; padding: 2px 8px; border-radius: 5px; "
                "background: #F9E6D4; color: #9A4D18; border: 1px solid #DFB086;"
            )
            self.phonetic_lbl.hide()
            self.warning_lbl.hide()
            self.body_lbl.setText("正在识别所在句子并评估用词与可替代词，请稍候...")
        else:
            type_names = {"word": "单词查询", "phrase": "短语鉴别", "sentence": "句段直译"}
            self.badge_lbl.setText(type_names.get(ctx.selection_type, "翻译中"))
            self.badge_lbl.setStyleSheet(
                "font-size: 11px; padding: 2px 8px; border-radius: 5px; background: #E1E8F0; color: #3E5876;"
            )
            self.phonetic_lbl.hide()

            if ctx.sentence_over_limit:
                self.warning_lbl.setText(
                    f"所在句子超出词数上限（当前 {ctx.sentence_word_count} 词 > 上限 {ctx.sentence_limit} 词），"
                    "本次未向 AI 发送上下文句子。"
                )
                self.warning_lbl.show()
            else:
                self.warning_lbl.hide()

            self.body_lbl.setText("正在请求 AI 解析中，请稍候...")

        self.adjustSize()
        self._place_within_screen(pos)
        self.show()
        self.raise_()

    def render_result(self, res: TranslationResult) -> None:
        if res.mode == "error":
            self.badge_lbl.setText("请求异常")
            self.badge_lbl.setStyleSheet(
                "font-size: 11px; padding: 2px 8px; border-radius: 5px; background: #EDDEDE; color: #824646;"
            )
            self.body_lbl.setText(res.error_msg)
            self.adjustSize()
            return

        self.title_lbl.setText(res.term[:80])

        # 纠错模式专属渲染（不存入单词本）
        if res.mode == "correction":
            self._apply_card_theme(True)
            self.badge_lbl.setText("纠错模式 (不入库)")
            self.badge_lbl.setStyleSheet(
                "font-size: 11px; font-weight: bold; padding: 2px 8px; border-radius: 5px; "
                "background: #F9E6D4; color: #9A4D18; border: 1px solid #DFB086;"
            )
            self.phonetic_lbl.hide()
            self.warning_lbl.hide()

            parts = []
            # 1. 是否合适结论与详细分析
            verdict_text = html.escape(
                res.verdict or ("使用合适" if res.is_appropriate else "使用欠佳 / 建议替换")
            )
            analysis_text = html.escape(res.correction_analysis or "")
            if res.is_appropriate:
                v_bg, v_border, v_fg, v_icon = "#EBF5EE", "#4C8C68", "#2B6142", "✓"
            else:
                v_bg, v_border, v_fg, v_icon = "#FBECE4", "#C86D44", "#9A4220", "⚠"

            parts.append(
                f"<div style='margin-top:4px; padding:8px 10px; background:{v_bg}; "
                f"border-left:3.5px solid {v_border}; border-radius:5px;'>"
                f"<div style='font-weight:bold; color:{v_fg}; font-size:13px;'>"
                f"{v_icon} 用词评估：{verdict_text}</div>"
                + (
                    f"<div style='margin-top:4px; color:#27303F; font-size:12.5px;'>{analysis_text}</div>"
                    if analysis_text
                    else ""
                )
                + "</div>"
            )

            # 2. 可替代词推荐（无论合适与否均展示）
            alt_lines = []
            for item in res.alternatives or []:
                w = html.escape(str(item.get("word") or "").strip())
                m = html.escape(str(item.get("meaning") or "").strip())
                if not w:
                    continue
                if m:
                    alt_lines.append(
                        f"<div style='margin-top:4px;'>"
                        f"<span style='background:#F6E3D2; color:#8C4212; padding:1px 6px; "
                        f"border-radius:4px; font-weight:bold; font-size:12.5px;'>{w}</span> "
                        f"<span style='color:#334155; font-size:12.5px;'>{m}</span></div>"
                    )
                else:
                    alt_lines.append(
                        f"<div style='margin-top:4px;'>"
                        f"<span style='background:#F6E3D2; color:#8C4212; padding:1px 6px; "
                        f"border-radius:4px; font-weight:bold; font-size:12.5px;'>{w}</span></div>"
                    )
            if alt_lines:
                parts.append(
                    f"<div style='margin-top:8px; padding:7px 10px; background:#FAF1E8; "
                    f"border:1px solid #EBD4BE; border-radius:5px;'>"
                    f"<div style='font-weight:bold; color:#9A4D18; font-size:12.5px;'>可替代词推荐：</div>"
                    f"{''.join(alt_lines)}</div>"
                )

            # 3. 自动识别的所在句子与参考译文
            if res.sentence:
                escaped_sent = html.escape(res.sentence)
                escaped_term = html.escape(res.term.strip())
                if escaped_term:
                    pattern = re.compile(re.escape(escaped_term), re.IGNORECASE)
                    escaped_sent = pattern.sub(
                        lambda m: f"<b style='color:#8C4212; background:#F6E3D2; padding:0 3px; border-radius:3px;'>{m.group(0)}</b>",
                        escaped_sent,
                    )
                parts.append(
                    f"<div style='margin-top:8px; padding:6px 9px; background:#F5EFEA; "
                    f"border-radius:4px; font-size:12px; color:#334155;'>"
                    f"<div><b>识别原句：</b>{escaped_sent}</div>"
                    + (
                        f"<div style='margin-top:3px; color:#526071;'><b>参考译文：</b>{html.escape(res.sentence_translation)}</div>"
                        if res.sentence_translation
                        else ""
                    )
                    + "</div>"
                )

            self.body_lbl.setText("".join(parts) or "无返回内容")
            self.adjustSize()
            return

        self._apply_card_theme(False)

        # 状态徽章
        if res.just_resurrected:
            self.badge_lbl.setText("又回来啦 (已激活黄框)")
            self.badge_lbl.setStyleSheet(
                "font-size: 11px; font-weight: bold; padding: 2px 8px; border-radius: 5px; "
                "background: #EDE5D3; color: #7D6435; border: 1px solid #B0935A;"
            )
        elif res.is_incremental:
            self.badge_lbl.setText("增量更新句库")
            self.badge_lbl.setStyleSheet(
                "font-size: 11px; padding: 2px 8px; border-radius: 5px; background: #E1E8F0; color: #3E5876;"
            )
        elif res.mode == "word":
            self.badge_lbl.setText("单词已入库")
            self.badge_lbl.setStyleSheet(
                "font-size: 11px; padding: 2px 8px; border-radius: 5px; background: #E1E8F0; color: #3E5876;"
            )
        elif res.mode == "phrase":
            self.badge_lbl.setText("短语已入库")
            self.badge_lbl.setStyleSheet(
                "font-size: 11px; padding: 2px 8px; border-radius: 5px; background: #E1E8F0; color: #3E5876;"
            )
        elif res.mode == "phrase_non_fixed":
            self.badge_lbl.setText("非固定短语 (不入库)")
            self.badge_lbl.setStyleSheet(
                "font-size: 11px; padding: 2px 8px; border-radius: 5px; background: #E5E8EC; color: #526071;"
            )
        else:
            self.badge_lbl.setText("句段直译 (不入库)")
            self.badge_lbl.setStyleSheet(
                "font-size: 11px; padding: 2px 8px; border-radius: 5px; background: #E5E8EC; color: #526071;"
            )

        # 音标
        if res.phonetic:
            self.phonetic_lbl.setText(res.phonetic)
            self.phonetic_lbl.show()
        else:
            self.phonetic_lbl.hide()

        # 句子超出词上限提示
        if res.sentence_over_limit:
            self.warning_lbl.setText(
                f"所在句子超出词数上限（当前 {res.sentence_word_count} 词 > 上限 {res.sentence_limit} 词），"
                "未发送句子，仅展示词条释义。"
            )
            self.warning_lbl.show()
        else:
            self.warning_lbl.hide()

        # 组装 HTML 正文
        parts = []
        if res.mode in ("sentence", "phrase_non_fixed"):
            parts.append(
                f"<div style='font-size:14px; color:#1E2530; margin-top:4px;'>"
                f"{html.escape(res.direct_translation)}</div>"
            )
        else:
            # 单词展示【词性 + 释义】，短语仅展示【释义（无词性）】
            if res.meanings:
                meaning_lines = []
                for item in res.meanings:
                    pos = (item.get("pos") or "").strip()
                    m_text = html.escape(str(item.get("meaning") or ""))
                    if pos and res.mode == "word":
                        meaning_lines.append(
                            f"<div style='margin-bottom:3px;'>"
                            f"<span style='background:#DDE5EE; color:#3E5876; padding:1px 5px; "
                            f"border-radius:4px; font-weight:bold; font-size:12px;'>{html.escape(pos)}</span> "
                            f"<span>{m_text}</span></div>"
                        )
                    else:
                        meaning_lines.append(
                            f"<div style='margin-bottom:3px; font-weight:500;'>• {m_text}</div>"
                        )
                parts.append("".join(meaning_lines))

            # 句中用法与原句翻译
            if res.context_usage:
                parts.append(
                    f"<div style='margin-top:8px; padding:7px 9px; background:#EDF1F5; "
                    f"border-left:3px solid #5B7A9C; border-radius:4px;'>"
                    f"<b style='color:#3E5876;'>句中用法：</b>{html.escape(res.context_usage)}</div>"
                )
            if res.sentence:
                parts.append(
                    f"<div style='margin-top:6px; padding:6px 9px; background:#F1F3F6; "
                    f"border-radius:4px; font-size:12px; color:#334155;'>"
                    f"<div><b>原句：</b>{html.escape(res.sentence)}</div>"
                    + (
                        f"<div style='margin-top:3px; color:#526071;'><b>译文：</b>{html.escape(res.sentence_translation)}</div>"
                        if res.sentence_translation
                        else ""
                    )
                    + "</div>"
                )

        self.body_lbl.setText("".join(parts) or "无返回内容")
        self.adjustSize()

        if res.stored:
            self.vocab_updated.emit()

    def _place_within_screen(self, pos: QPoint) -> None:
        screen = QApplication.screenAt(pos) or QApplication.primaryScreen()
        avail = screen.availableGeometry()
        x = min(max(avail.left() + 10, pos.x()), avail.right() - self.width() - 10)
        y = min(max(avail.top() + 10, pos.y()), avail.bottom() - self.height() - 10)
        self.move(x, y)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._drag_offset is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_offset)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        self._drag_offset = None
        super().mouseReleaseEvent(event)


class SelectionCoordinator:
    """
    轻量级划词监听器：
    检测鼠标拖拽划词或双击选词 -> 弹出半透明 'T' 图标 -> 点击后调用取词与翻译弹窗。
    """

    def __init__(
        self,
        storage: VocabStorage,
        on_open_review: Optional[Callable[[], None]] = None,
    ) -> None:
        self.storage = storage
        self.t_icon = FloatingTIcon()
        self.popup = TranslationPopup(on_open_review=on_open_review)
        self.t_icon.clicked.connect(self._on_t_icon_clicked)
        self.t_icon.correction_clicked.connect(self._on_correction_icon_clicked)

        self._worker: Optional[TranslationWorker] = None
        self._was_lbutton_down = False
        self._down_pos = QPoint(0, 0)
        self._down_time = 0.0
        self._last_click_time = 0.0
        self._last_click_pos = QPoint(0, 0)

        self._poll_timer = QTimer()
        self._poll_timer.setInterval(35)
        self._poll_timer.timeout.connect(self._poll_mouse_state)
        self._poll_timer.start()

    def _is_point_in_widget(self, pt: QPoint, widget: QWidget) -> bool:
        return widget.isVisible() and widget.frameGeometry().contains(pt)

    def _poll_mouse_state(self) -> None:
        state = ctypes.windll.user32.GetAsyncKeyState(0x01)  # VK_LBUTTON
        is_down = (state & 0x8000) != 0
        cur_pos = QCursor.pos()
        now = time.time()

        # 鼠标刚按下
        if is_down and not self._was_lbutton_down:
            self._was_lbutton_down = True
            self._down_pos = cur_pos
            self._down_time = now

            # 如果点击在 T 图标或弹窗外部，自动收起
            if self.t_icon.isVisible() and not self._is_point_in_widget(cur_pos, self.t_icon):
                self.t_icon.hide()
            if (
                self.popup.isVisible()
                and not self._is_point_in_widget(cur_pos, self.popup)
                and not self._is_point_in_widget(cur_pos, self.t_icon)
            ):
                self.popup.hide()
            return

        # 鼠标刚松开
        if not is_down and self._was_lbutton_down:
            self._was_lbutton_down = False

            # 如果点击在自身悬浮窗上，不触发重新弹出 T 图标
            if self._is_point_in_widget(cur_pos, self.t_icon) or self._is_point_in_widget(
                cur_pos, self.popup
            ):
                return

            dx = cur_pos.x() - self._down_pos.x()
            dy = cur_pos.y() - self._down_pos.y()
            drag_dist = math.hypot(dx, dy)
            hold_dur = now - self._down_time

            is_drag_select = drag_dist >= 12 and hold_dur >= 0.06
            is_double_click = (
                (now - self._last_click_time) <= 0.42
                and math.hypot(
                    cur_pos.x() - self._last_click_pos.x(),
                    cur_pos.y() - self._last_click_pos.y(),
                )
                <= 10
            )

            self._last_click_time = now
            self._last_click_pos = cur_pos

            if not (is_drag_select or is_double_click):
                return

            QTimer.singleShot(45, lambda p=cur_pos: self._try_show_t_icon(p))

    def _try_show_t_icon(self, pos: QPoint) -> None:
        cfg = load_config()
        global_mode = bool(cfg.get("global_mode", False))

        if is_word_foreground():
            if has_word_selection_fast():
                self.t_icon.show_near(pos, float(cfg.get("icon_opacity", 0.78)))
        elif global_mode:
            self.t_icon.show_near(pos, float(cfg.get("icon_opacity", 0.78)))

    def _on_t_icon_clicked(self, icon_pos: QPoint) -> None:
        cfg = load_config()
        limit = int(cfg.get("sentence_word_limit", 50))
        global_mode = bool(cfg.get("global_mode", False))

        ctx = capture_selection(sentence_word_limit=limit, global_mode=global_mode)
        if ctx is None or not ctx.selected_text.strip():
            return

        self.popup.show_loading(icon_pos, ctx, mode="translate")
        self._worker = TranslationWorker(ctx, self.storage, cfg, mode="translate")
        self._worker.finished_result.connect(self.popup.render_result)
        self._worker.start()

    def _on_correction_icon_clicked(self, icon_pos: QPoint) -> None:
        cfg = load_config()
        limit = int(cfg.get("sentence_word_limit", 50))
        global_mode = bool(cfg.get("global_mode", False))

        ctx = capture_selection(
            sentence_word_limit=limit,
            global_mode=global_mode,
            for_correction=True,
        )
        if ctx is None or not ctx.selected_text.strip():
            return

        self.popup.show_loading(icon_pos, ctx, mode="correction")
        self._worker = TranslationWorker(ctx, self.storage, cfg, mode="correction")
        self._worker.finished_result.connect(self.popup.render_result)
        self._worker.start()
