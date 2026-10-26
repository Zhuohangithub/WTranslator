import sys

from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtGui import QAction, QColor, QCursor, QFont, QIcon, QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from config import load_config, save_config
from extractor import SelectionContext
from storage import VocabStorage
from ui_floating import SelectionCoordinator, TranslationWorker
from ui_review import ReviewMainWindow


def create_app_icon() -> QIcon:
    """动态绘制应用与托盘的 'T' 字图标，无需外部图片资源。"""
    pix = QPixmap(64, 64)
    pix.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

    path = QPainterPath()
    path.addRoundedRect(QRect(4, 4, 56, 56), 14, 14)
    painter.fillPath(path, QColor(37, 99, 235))

    painter.setPen(QColor(255, 255, 255))
    font = QFont("Georgia", 34, QFont.Weight.Bold)
    painter.setFont(font)
    painter.drawText(QRect(4, 4, 56, 56), Qt.AlignmentFlag.AlignCenter, "T")
    painter.end()
    return QIcon(pix)


class WTranslatorApp:
    """组装划词监听、悬浮 T 图标、翻译气泡弹窗、复习主窗口与系统托盘。"""

    def __init__(self) -> None:
        self.app = QApplication(sys.argv)
        self.app.setQuitOnLastWindowClosed(False)
        self.icon = create_app_icon()
        self.app.setWindowIcon(self.icon)

        self.storage = VocabStorage()
        self.coordinator = SelectionCoordinator(
            storage=self.storage,
            on_open_review=self.show_review_window,
        )
        self.review_window = ReviewMainWindow(
            storage=self.storage,
            on_manual_test=self.trigger_context_popup,
        )

        # 当悬浮窗收录新词或增量更新句子时，实时刷新复习面板
        self.coordinator.popup.vocab_updated.connect(self.review_window.reload_cards)

        self._setup_tray()

    def _setup_tray(self) -> None:
        self.tray = QSystemTrayIcon(self.icon, self.app)
        self.tray.setToolTip("WTranslator - Word 划词翻译与英语复习助手")

        menu = QMenu()
        open_act = QAction("📚 打开英语复习卡片库", menu)
        open_act.triggered.connect(self.show_review_window)
        menu.addAction(open_act)

        self.global_act = QAction("🌐 开启全局划词模式", menu)
        self.global_act.setCheckable(True)
        self.global_act.setChecked(bool(load_config().get("global_mode", False)))
        self.global_act.triggered.connect(self._toggle_global_mode)
        menu.addAction(self.global_act)

        settings_act = QAction("⚙️ 接口与词上限设置", menu)
        settings_act.triggered.connect(self.review_window.open_settings)
        menu.addAction(settings_act)

        update_act = QAction("🔄 检查在线更新", menu)
        update_act.triggered.connect(self.review_window.open_update_dialog)
        menu.addAction(update_act)

        menu.addSeparator()
        quit_act = QAction("退出程序", menu)
        quit_act.triggered.connect(self.app.quit)
        menu.addAction(quit_act)

        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._on_tray_activated)
        self.tray.show()

    def _toggle_global_mode(self, checked: bool) -> None:
        cfg = load_config()
        cfg["global_mode"] = checked
        save_config(cfg)

    def _on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self.show_review_window()

    def show_review_window(self) -> None:
        self.review_window.reload_cards()
        self.review_window.showNormal()
        self.review_window.raise_()
        self.review_window.activateWindow()

    def trigger_context_popup(self, ctx: SelectionContext) -> None:
        """供『模拟划词』直接调用悬浮翻译弹窗。"""
        cfg = load_config()
        pos = QCursor.pos() - QPoint(180, 40)
        self.coordinator.popup.show_loading(pos, ctx)
        self.coordinator._worker = TranslationWorker(ctx, self.storage, cfg)
        self.coordinator._worker.finished_result.connect(self.coordinator.popup.render_result)
        self.coordinator._worker.start()

    def run(self) -> int:
        self.show_review_window()
        return self.app.exec()


if __name__ == "__main__":
    instance = WTranslatorApp()
    sys.exit(instance.run())
