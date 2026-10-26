import html
import re
from typing import Any, Callable, Dict, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QCursor
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from config import PROVIDER_PRESETS, get_current_version, load_config, save_config
from extractor import SelectionContext, classify_selection, count_words
from llm_service import fetch_available_models
from storage import VocabStorage
from updater import apply_online_update, check_for_updates, restart_application


def _highlight_term_html(sentence: str, term: str) -> str:
    """在英文例句中高亮显示目标单词或短语（采用灰感暗调蓝）。"""
    escaped_sent = html.escape(sentence)
    escaped_term = html.escape(term.strip())
    if not escaped_term:
        return escaped_sent
    pattern = re.compile(re.escape(escaped_term), re.IGNORECASE)
    return pattern.sub(
        lambda m: f"<b style='color:#3A526E; background:#DDE5EE; padding:0 4px; border-radius:3px;'>{m.group(0)}</b>",
        escaped_sent,
    )


class VocabCardWidget(QFrame):
    """
    英语复习词语/短语卡片组件（采用现代审美灰感暗色调红/蓝/黄）：
    - 蓝色卡片框 (blue)：雾霾灰蓝（正常状态）
    - 红色卡片框 (red)：灰调暗砖红（超过一周 >7天 未复习）
    - 黄色卡片框 (yellow)：灰调暗琥珀黄（软删除后再次划到，“又回来啦”）
    - 点击卡片主体：打卡已复习（记录复习次数与距上次时间，红/黄框恢复蓝框）
    - 点击展开按钮：展开/折叠用过该词的所有历史句子与用法
    - 点击删除按钮：软删除卡片（不删底层存储）
    """

    reviewed = Signal(int)
    deleted = Signal(int)

    STYLE_MAP = {
        "blue": {
            "border": "#5B7A9C",
            "bg": "#F3F5F8",
            "hover_bg": "#EAEFF5",
            "tag_bg": "#DEE6F0",
            "tag_fg": "#3A526E",
            "status_text": "● 正常复习中",
        },
        "red": {
            "border": "#9E5E5E",
            "bg": "#F8F3F3",
            "hover_bg": "#F2EAEA",
            "tag_bg": "#ECDDDD",
            "tag_fg": "#7A4040",
            "status_text": "● 超过一周未复习",
        },
        "yellow": {
            "border": "#A68B56",
            "bg": "#F8F6F1",
            "hover_bg": "#F2EEE3",
            "tag_bg": "#ECE4D2",
            "tag_fg": "#735C2F",
            "status_text": "● 又回来啦",
        },
    }

    def __init__(
        self, data: Dict[str, Any], parent: Optional[QWidget] = None
    ) -> None:
        super().__init__(parent)
        self.data = data
        self.vocab_id: int = data["id"]
        self._expanded = False
        self._just_clicked_review = False
        self.setObjectName("VocabCard")
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        self.main_layout = QVBoxLayout(self)
        self.main_layout.setContentsMargins(16, 14, 16, 14)
        self.main_layout.setSpacing(8)

        # 1. 顶部栏：词条 + 音标 + 状态标签 + 删除按钮
        header = QHBoxLayout()
        header.setSpacing(10)

        self.term_lbl = QLabel()
        self.term_lbl.setStyleSheet("font-size: 19px; font-weight: bold; color: #1E2530;")
        header.addWidget(self.term_lbl)

        self.phonetic_lbl = QLabel()
        self.phonetic_lbl.setStyleSheet("font-size: 13px; color: #526071;")
        header.addWidget(self.phonetic_lbl)

        self.type_lbl = QLabel()
        self.type_lbl.setStyleSheet(
            "font-size: 11px; padding: 2px 7px; border-radius: 4px; background: #E3E7ED; color: #3D4856;"
        )
        header.addWidget(self.type_lbl)

        header.addStretch(1)

        self.returned_badge = QLabel("曾回归")
        self.returned_badge.setStyleSheet(
            "font-size: 11px; padding: 2px 7px; border-radius: 4px; "
            "background: #ECE4D2; color: #735C2F; border: 1px solid #C8B48A;"
        )
        header.addWidget(self.returned_badge)

        self.status_lbl = QLabel()
        header.addWidget(self.status_lbl)

        self.del_btn = QPushButton("删除卡片")
        self.del_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.del_btn.setToolTip("仅从复习列表隐藏，不删除底层数据；下次划到该词会显示『又回来啦』黄框")
        self.del_btn.setStyleSheet(
            """
            QPushButton {
                background: #F8FAFC;
                color: #526071;
                border: 1px solid #CBD5E1;
                border-radius: 5px;
                padding: 3px 9px;
                font-size: 12px;
            }
            QPushButton:hover {
                background: #ECDDDD;
                color: #7A4040;
                border-color: #B88484;
            }
            """
        )
        self.del_btn.clicked.connect(self._on_delete_clicked)
        header.addWidget(self.del_btn)

        self.main_layout.addLayout(header)

        # 2. 词性与释义区
        self.meanings_lbl = QLabel()
        self.meanings_lbl.setWordWrap(True)
        self.meanings_lbl.setStyleSheet("font-size: 14px; color: #27303F;")
        self.main_layout.addWidget(self.meanings_lbl)

        # 3. 底部栏：复习统计信息 + 展开历史句子按钮
        footer = QHBoxLayout()
        self.stats_lbl = QLabel()
        self.stats_lbl.setStyleSheet("font-size: 12px; color: #526071;")
        footer.addWidget(self.stats_lbl)

        footer.addStretch(1)

        self.click_hint_lbl = QLabel("点击卡片空白处打卡已复习")
        self.click_hint_lbl.setStyleSheet("font-size: 11px; color: #6E7C8C;")
        footer.addWidget(self.click_hint_lbl)

        self.expand_btn = QPushButton()
        self.expand_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.expand_btn.setStyleSheet(
            """
            QPushButton {
                background: #F8FAFC;
                color: #3A526E;
                border: 1px solid #9CB0C6;
                border-radius: 5px;
                padding: 3px 10px;
                font-size: 12px;
                font-weight: 500;
            }
            QPushButton:hover {
                background: #DEE6F0;
            }
            """
        )
        self.expand_btn.clicked.connect(self._toggle_sentences)
        footer.addWidget(self.expand_btn)

        self.main_layout.addLayout(footer)

        # 4. 可展开的历史句子容器
        self.sentences_frame = QFrame()
        self.sentences_frame.setStyleSheet(
            "QFrame { background: #FAFBFD; border: 1px solid #D8E0EA; border-radius: 8px; }"
        )
        self.sentences_layout = QVBoxLayout(self.sentences_frame)
        self.sentences_layout.setContentsMargins(12, 10, 12, 10)
        self.sentences_layout.setSpacing(8)
        self.sentences_frame.hide()
        self.main_layout.addWidget(self.sentences_frame)

        self.refresh_ui(data)

    def refresh_ui(self, data: Dict[str, Any]) -> None:
        self.data = data
        color_key = data.get("box_color", "blue")
        theme = self.STYLE_MAP.get(color_key, self.STYLE_MAP["blue"])

        self.setStyleSheet(
            f"""
            QFrame#VocabCard {{
                background-color: {theme['bg']};
                border: 2px solid {theme['border']};
                border-radius: 10px;
            }}
            QFrame#VocabCard:hover {{
                background-color: {theme['hover_bg']};
            }}
            """
        )

        self.term_lbl.setText(data["term"])
        phonetic = data.get("phonetic") or ""
        self.phonetic_lbl.setText(phonetic if phonetic else "")
        self.phonetic_lbl.setVisible(bool(phonetic))

        is_word = data.get("term_type") == "word"
        self.type_lbl.setText("单词" if is_word else "短语")

        # 若曾触发过“又回来啦”且当前已打卡变回蓝框，保留小标记
        show_ever_returned = bool(data.get("ever_returned")) and color_key != "yellow"
        self.returned_badge.setVisible(show_ever_returned)

        self.status_lbl.setText(theme["status_text"])
        self.status_lbl.setStyleSheet(
            f"font-size: 12px; font-weight: bold; padding: 3px 9px; border-radius: 6px; "
            f"background: {theme['tag_bg']}; color: {theme['tag_fg']};"
        )

        # 渲染词性与词义（单词带词性，短语不带词性）
        meanings = data.get("meanings") or []
        m_lines = []
        for item in meanings:
            pos = (item.get("pos") or "").strip()
            meaning = html.escape(str(item.get("meaning") or ""))
            if pos and is_word:
                m_lines.append(
                    f"<span style='background:#DDE5EE; color:#3A526E; padding:1px 6px; "
                    f"border-radius:4px; font-weight:bold; font-size:12px;'>{html.escape(pos)}</span> "
                    f"<span>{meaning}</span>"
                )
            else:
                m_lines.append(f"<span>{meaning}</span>")
        self.meanings_lbl.setText("<br/>".join(m_lines) if m_lines else "（暂无释义）")

        # 渲染复习次数与距离上次复习时间
        rev_cnt = data.get("review_count", 0)
        if self._just_clicked_review:
            stats_text = (
                f"刚刚已打卡复习 · 累计复习 <b>{rev_cnt}</b> 次 · "
                f"距上次间隔：<b>{data.get('last_interval_text', '首次复习')}</b>"
            )
        else:
            if rev_cnt == 0:
                stats_text = f"尚未复习（收录于 <b>{data.get('elapsed_text', '刚刚')}</b> 前）"
            else:
                stats_text = (
                    f"已复习 <b>{rev_cnt}</b> 次 · "
                    f"距上次复习：<b>{data.get('elapsed_text', '刚刚')}</b> "
                    f"（上次间隔：{data.get('last_interval_text', '-')}）"
                )
        self.stats_lbl.setText(stats_text)

        # 渲染历史例句列表
        sents = data.get("sentences") or []
        sent_cnt = len(sents)
        if sent_cnt > 0:
            self.expand_btn.show()
            self.expand_btn.setText(
                f"收起句子 ({sent_cnt})" if self._expanded else f"展开句子 ({sent_cnt})"
            )
        else:
            self.expand_btn.hide()
            self.sentences_frame.hide()

        # 重建句子子项
        while self.sentences_layout.count():
            child = self.sentences_layout.takeAt(0)
            if child.widget():
                child.widget().deleteLater()

        for idx, s in enumerate(sents, 1):
            hl_sent = _highlight_term_html(s.get("sentence", ""), data["term"])
            trans = html.escape(s.get("translation") or "")
            usage = html.escape(s.get("usage_note") or "")

            block = QLabel()
            block.setWordWrap(True)
            block.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            block.setStyleSheet(
                "border: none; background: #F2F4F8; padding: 8px 10px; border-radius: 6px; font-size: 13px;"
            )
            html_parts = [f"<div><b>[{idx}]</b> {hl_sent}</div>"]
            if usage:
                html_parts.append(
                    f"<div style='color:#3A526E; margin-top:3px;'><b>句中用法：</b>{usage}</div>"
                )
            if trans:
                html_parts.append(
                    f"<div style='color:#526071; margin-top:2px;'><b>译文：</b>{trans}</div>"
                )
            block.setText("".join(html_parts))
            self.sentences_layout.addWidget(block)

    def _toggle_sentences(self) -> None:
        self._expanded = not self._expanded
        self.sentences_frame.setVisible(self._expanded)
        sent_cnt = len(self.data.get("sentences") or [])
        self.expand_btn.setText(
            f"收起句子 ({sent_cnt})" if self._expanded else f"展开句子 ({sent_cnt})"
        )

    def _on_delete_clicked(self) -> None:
        self.deleted.emit(self.vocab_id)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._just_clicked_review = True
            self.reviewed.emit(self.vocab_id)
            event.accept()
            return
        super().mousePressEvent(event)


class SettingsDialog(QDialog):
    """API Key、在线拉取模型列表、句子词数上限及全局/仅Word模式配置窗口。"""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("⚙ WTranslator 设置")
        self.setMinimumWidth(480)
        cfg = load_config()

        layout = QVBoxLayout(self)
        form = QFormLayout()
        form.setSpacing(12)

        self.preset_combo = QComboBox()
        self.preset_combo.addItem("自定义 / 当前配置")
        for name in PROVIDER_PRESETS:
            self.preset_combo.addItem(name)
        self.preset_combo.currentTextChanged.connect(self._on_preset_changed)
        form.addRow("服务商预设：", self.preset_combo)

        self.api_key_edit = QLineEdit(cfg.get("api_key", ""))
        self.api_key_edit.setEchoMode(QLineEdit.EchoMode.PasswordEchoOnEdit)
        self.api_key_edit.setPlaceholderText("填入你的 API Key (如 sk-...)")
        form.addRow("API Key：", self.api_key_edit)

        self.base_url_edit = QLineEdit(cfg.get("base_url", ""))
        form.addRow("Base URL：", self.base_url_edit)

        # 模型下拉选择框 + 在线拉取模型按钮
        model_row = QHBoxLayout()
        model_row.setSpacing(8)
        self.model_combo = QComboBox()
        self.model_combo.setEditable(True)
        current_model = cfg.get("model", "deepseek-chat")
        default_models = [
            current_model,
            "deepseek-chat",
            "deepseek-reasoner",
            "Qwen/Qwen2.5-7B-Instruct",
            "qwen-plus",
            "glm-4-flash",
            "gpt-4o-mini",
        ]
        seen = set()
        for m in default_models:
            if m and m not in seen:
                seen.add(m)
                self.model_combo.addItem(m)
        self.model_combo.setCurrentText(current_model)
        model_row.addWidget(self.model_combo, 1)

        self.fetch_models_btn = QPushButton("拉取模型列表")
        self.fetch_models_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.fetch_models_btn.setStyleSheet(
            "background: #E6ECF2; color: #3A526E; border: 1px solid #9CB0C6; "
            "padding: 4px 10px; border-radius: 5px; font-size: 12px;"
        )
        self.fetch_models_btn.clicked.connect(self._on_fetch_models)
        model_row.addWidget(self.fetch_models_btn)
        form.addRow("模型名称：", model_row)

        self.fetch_status_lbl = QLabel("")
        self.fetch_status_lbl.setStyleSheet("font-size: 11px; color: #526071;")
        self.fetch_status_lbl.hide()
        form.addRow("", self.fetch_status_lbl)

        self.limit_spin = QSpinBox()
        self.limit_spin.setRange(10, 300)
        self.limit_spin.setValue(int(cfg.get("sentence_word_limit", 50)))
        self.limit_spin.setSuffix(" 词")
        form.addRow("句子词数上限：", self.limit_spin)

        self.global_chk = QCheckBox("开启全局划词模式（除 Word 外，在浏览器/阅读器中也弹出 T 图标）")
        self.global_chk.setChecked(bool(cfg.get("global_mode", False)))
        form.addRow("取词范围：", self.global_chk)

        self.repo_edit = QLineEdit(cfg.get("github_repo", "Zhuohangithub/WTranslator"))
        self.repo_edit.setPlaceholderText("用户名/仓库名，如 Zhuohangithub/WTranslator")
        form.addRow("GitHub 更新源：", self.repo_edit)

        layout.addLayout(form)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        cancel_btn = QPushButton("取消")
        cancel_btn.clicked.connect(self.reject)
        save_btn = QPushButton("保存配置")
        save_btn.setStyleSheet(
            "background: #486380; color: white; font-weight: bold; padding: 6px 16px; border-radius: 6px;"
        )
        save_btn.clicked.connect(self._on_save)
        btn_row.addWidget(cancel_btn)
        btn_row.addWidget(save_btn)
        layout.addLayout(btn_row)

    def _on_preset_changed(self, name: str) -> None:
        preset = PROVIDER_PRESETS.get(name)
        if preset:
            self.base_url_edit.setText(preset["base_url"])
            self.model_combo.setCurrentText(preset["model"])

    def _on_fetch_models(self) -> None:
        api_key = self.api_key_edit.text().strip()
        base_url = self.base_url_edit.text().strip()
        if not base_url:
            QMessageBox.warning(self, "提示", "请先填写 Base URL。")
            return

        self.fetch_models_btn.setEnabled(False)
        self.fetch_models_btn.setText("拉取中...")
        self.fetch_status_lbl.setText("正在从服务端拉取模型列表...")
        self.fetch_status_lbl.show()
        self.repaint()

        try:
            models = fetch_available_models(api_key=api_key, base_url=base_url)
            if not models:
                self.fetch_status_lbl.setText("服务端返回列表为空，请手动输入或检查接口地址。")
            else:
                prev_text = self.model_combo.currentText().strip()
                self.model_combo.clear()
                for m in models:
                    self.model_combo.addItem(m)
                if prev_text in models:
                    self.model_combo.setCurrentText(prev_text)
                else:
                    self.model_combo.setCurrentIndex(0)
                self.fetch_status_lbl.setText(f"已成功拉取 {len(models)} 个可用模型，可在下拉框中选择。")
                self.model_combo.showPopup()
        except Exception as exc:
            self.fetch_status_lbl.setText(f"拉取失败：{exc}")
        finally:
            self.fetch_models_btn.setEnabled(True)
            self.fetch_models_btn.setText("拉取模型列表")

    def _on_save(self) -> None:
        cfg = load_config()
        cfg["api_key"] = self.api_key_edit.text().strip()
        cfg["base_url"] = self.base_url_edit.text().strip()
        cfg["model"] = self.model_combo.currentText().strip()
        cfg["sentence_word_limit"] = self.limit_spin.value()
        cfg["global_mode"] = self.global_chk.isChecked()
        cfg["github_repo"] = self.repo_edit.text().strip() or "Zhuohangithub/WTranslator"
        save_config(cfg)
        self.accept()


class UpdateDialog(QDialog):
    """GitHub 在线检查更新与热修复一键同步窗口。"""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("在线更新 (GitHub OTA)")
        self.setMinimumWidth(460)
        self._info = None

        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        cfg = load_config()
        repo = cfg.get("github_repo", "Zhuohangithub/WTranslator")
        local_ver = get_current_version()

        self.status_lbl = QLabel(
            f"<b>当前版本：</b>v{local_ver}<br/>"
            f"<b>GitHub 仓库：</b><code>{html.escape(repo)}</code><br/><br/>"
            "正在连接 GitHub 检查最新版本..."
        )
        self.status_lbl.setWordWrap(True)
        self.status_lbl.setStyleSheet(
            "background: #F4F6F9; border: 1px solid #D8E0EA; border-radius: 8px; padding: 12px; font-size: 13px;"
        )
        layout.addWidget(self.status_lbl)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)

        self.sync_btn = QPushButton("同步最新代码修复并重启")
        self.sync_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.sync_btn.setStyleSheet(
            "background: #486380; color: white; font-weight: bold; padding: 6px 14px; border-radius: 6px;"
        )
        self.sync_btn.clicked.connect(self._do_update)
        btn_row.addWidget(self.sync_btn)

        close_btn = QPushButton("关闭")
        close_btn.clicked.connect(self.reject)
        btn_row.addWidget(close_btn)

        layout.addLayout(btn_row)
        self._check_now()

    def _check_now(self) -> None:
        info = check_for_updates()
        self._info = info
        cfg = load_config()
        repo = cfg.get("github_repo", "Zhuohangithub/WTranslator")

        if info.error:
            self.status_lbl.setText(
                f"<b>当前版本：</b>v{info.local_version}<br/>"
                f"<b>GitHub 仓库：</b><code>{html.escape(repo)}</code><br/><br/>"
                f"暂未读取到远端版本信息：{html.escape(info.error)}<br/>"
                "<span style='color:#64748B; font-size:12px;'>（请确认仓库已推送到 GitHub 且设为 Public）</span>"
            )
            return

        if info.has_update:
            self.sync_btn.setText(f"立即升级到 v{info.remote_version} 并重启")
            self.status_lbl.setText(
                f"<b>发现新版本：v{html.escape(info.remote_version)}</b>（当前 v{html.escape(info.local_version)}）<br/><br/>"
                f"<b>更新说明：</b><br/>{html.escape(info.changelog or '包含功能优化与问题修复。')}"
            )
        else:
            self.status_lbl.setText(
                f"<b>当前已是最新版本 (v{html.escape(info.local_version)})</b><br/><br/>"
                f"<b>最近版本说明：</b>{html.escape(info.changelog or '无')}<br/><br/>"
                "<span style='color:#526071; font-size:12px;'>提示：如果刚刚推送了代码修复但未修改版本号，"
                "仍可点击下方按钮强制拉取 GitHub 最新代码并重启桌面端（本地词库与配置不会丢失）。</span>"
            )

    def _do_update(self) -> None:
        self.sync_btn.setEnabled(False)
        self.sync_btn.setText("正在下载更新...")
        ok, msg = apply_online_update(self._info)
        if ok:
            QMessageBox.information(self, "更新成功", f"{msg}\n点击确定后将立即重启桌面端。")
            restart_application()
        else:
            QMessageBox.warning(self, "更新失败", msg)
            self.sync_btn.setEnabled(True)
            self.sync_btn.setText("重试同步更新")


class ManualTestDialog(QDialog):
    """快速模拟划词与上下文句子测试窗口（方便无需打开 Word 也能直接测试全部翻译与入库流程）。"""

    def __init__(
        self,
        on_trigger_context: Callable[[SelectionContext], None],
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("模拟划词翻译测试")
        self.setMinimumWidth(460)
        self.on_trigger_context = on_trigger_context

        layout = QVBoxLayout(self)
        form = QFormLayout()
        form.setSpacing(10)

        self.sel_edit = QLineEdit("ephemeral")
        self.sel_edit.setPlaceholderText("输入模拟划选的单词、短语或长句")
        form.addRow("模拟划选文本：", self.sel_edit)

        self.sent_edit = QLineEdit(
            "Fame in the internet age is often ephemeral, fading away within a few days."
        )
        self.sent_edit.setPlaceholderText("输入该词所在的完整英文句子（可留空或输入超50词长句测试上限拦截）")
        form.addRow("隐形捕获原句：", self.sent_edit)

        layout.addLayout(form)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        run_btn = QPushButton("立即触发 T 图标翻译弹窗")
        run_btn.setStyleSheet(
            "background: #486380; color: white; font-weight: bold; padding: 6px 16px; border-radius: 6px;"
        )
        run_btn.clicked.connect(self._on_run)
        btn_row.addWidget(run_btn)
        layout.addLayout(btn_row)

    def _on_run(self) -> None:
        sel = self.sel_edit.text().strip()
        sent = self.sent_edit.text().strip()
        if not sel:
            return
        cfg = load_config()
        limit = int(cfg.get("sentence_word_limit", 50))
        sel_type, w_cnt = classify_selection(sel)
        s_cnt = count_words(sent) if sent else 0
        over = s_cnt > limit

        ctx = SelectionContext(
            selected_text=sel,
            selection_type=sel_type,
            word_count=w_cnt,
            sentence="" if over else sent,
            sentence_word_count=s_cnt,
            sentence_over_limit=over,
            sentence_limit=limit,
            source_app="word",
        )
        self.accept()
        self.on_trigger_context(ctx)


class ReviewMainWindow(QMainWindow):
    """英语复习与卡片管理主窗口。"""

    FILTER_THEMES = {
        "all": {
            "active": "background: #3B4756; color: #F8FAFC; border: 1px solid #3B4756; border-radius: 6px; padding: 5px 11px; font-weight: bold;",
            "inactive": "background: #ECEFF3; color: #3B4756; border: 1px solid #CBD2DC; border-radius: 6px; padding: 5px 11px;",
        },
        "red": {
            "active": "background: #7A4040; color: #FAF5F5; border: 1px solid #7A4040; border-radius: 6px; padding: 5px 11px; font-weight: bold;",
            "inactive": "background: #F4ECEC; color: #7A4040; border: 1px solid #C8A6A6; border-radius: 6px; padding: 5px 11px;",
        },
        "yellow": {
            "active": "background: #735C2F; color: #FAF8F2; border: 1px solid #735C2F; border-radius: 6px; padding: 5px 11px; font-weight: bold;",
            "inactive": "background: #F4F0E6; color: #735C2F; border: 1px solid #C7B693; border-radius: 6px; padding: 5px 11px;",
        },
        "blue": {
            "active": "background: #3A526E; color: #F4F7FA; border: 1px solid #3A526E; border-radius: 6px; padding: 5px 11px; font-weight: bold;",
            "inactive": "background: #EBF0F5; color: #3A526E; border: 1px solid #9CB0C6; border-radius: 6px; padding: 5px 11px;",
        },
    }

    def __init__(
        self,
        storage: VocabStorage,
        on_manual_test: Optional[Callable[[SelectionContext], None]] = None,
    ) -> None:
        super().__init__()
        self.storage = storage
        self.on_manual_test = on_manual_test
        self._filter_color = "all"  # 'all' | 'blue' | 'red' | 'yellow'
        self._search_query = ""
        self._card_widgets: Dict[int, VocabCardWidget] = {}

        self.setWindowTitle(f"WTranslator v{get_current_version()} - 英语词句复习卡片库")
        self.resize(900, 660)
        self.setStyleSheet("QMainWindow { background-color: #EBEEF2; }")

        central = QWidget()
        self.setCentralWidget(central)
        root_layout = QVBoxLayout(central)
        root_layout.setContentsMargins(20, 16, 20, 16)
        root_layout.setSpacing(14)

        # 顶部导航与过滤工具栏
        top_bar = QHBoxLayout()
        top_bar.setSpacing(8)

        title = QLabel("英语复习卡片库")
        title.setStyleSheet("font-size: 20px; font-weight: bold; color: #1E2530;")
        top_bar.addWidget(title)

        top_bar.addSpacing(12)

        self.filter_btns: Dict[str, QPushButton] = {}
        filters = [
            ("all", "全部卡片"),
            ("red", "● 超一周未复习"),
            ("yellow", "● 又回来啦"),
            ("blue", "● 正常卡片"),
        ]
        for key, label in filters:
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda checked, k=key: self._set_filter(k))
            self.filter_btns[key] = btn
            top_bar.addWidget(btn)

        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("🔍 搜索单词、短语或中文释义...")
        self.search_edit.setFixedWidth(210)
        self.search_edit.setStyleSheet(
            "background: #F7F9FB; color: #1E2530; padding: 5px 10px; border: 1px solid #BCC6D4; border-radius: 6px;"
        )
        self.search_edit.textChanged.connect(self._on_search_changed)
        top_bar.addWidget(self.search_edit)

        top_bar.addStretch(1)

        if self.on_manual_test:
            test_btn = QPushButton("模拟划词")
            test_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            test_btn.setStyleSheet(
                "background: #E6ECF2; color: #3A526E; border: 1px solid #9CB0C6; "
                "padding: 5px 12px; border-radius: 6px; font-weight: 500;"
            )
            test_btn.clicked.connect(self._open_manual_test)
            top_bar.addWidget(test_btn)

        update_btn = QPushButton("在线更新")
        update_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        update_btn.setStyleSheet(
            "background: #E8ECE9; color: #3B5249; border: 1px solid #A8B8B0; "
            "padding: 5px 12px; border-radius: 6px; font-weight: 500;"
        )
        update_btn.clicked.connect(self.open_update_dialog)
        top_bar.addWidget(update_btn)

        settings_btn = QPushButton("⚙ 设置")
        settings_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        settings_btn.setStyleSheet(
            "background: #E6E9EF; color: #2D3748; border: 1px solid #B8C2CC; "
            "padding: 5px 12px; border-radius: 6px; font-weight: 500;"
        )
        settings_btn.clicked.connect(self.open_settings)
        top_bar.addWidget(settings_btn)

        root_layout.addLayout(top_bar)

        # 状态说明副标题
        hint_bar = QLabel(
            "<b>使用说明：</b>在 Word 中划词后点击半透明 <b>T</b> 图标即可查词并自动收录。"
            "点击下方<b>卡片任意区域</b>代表已复习一次；超过 7 天未复习会变为<b>暗红色卡片框</b>；"
            "删除卡片后若再次划到该词，将以<b>暗黄色卡片框（又回来啦）</b>重新回归。"
        )
        hint_bar.setWordWrap(True)
        hint_bar.setStyleSheet(
            "background: #F4F6F8; color: #4A5568; border: 1px solid #D5DCE6; "
            "border-radius: 8px; padding: 8px 12px; font-size: 12px;"
        )
        root_layout.addWidget(hint_bar)

        # 滚动卡片列表区
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")

        self.cards_container = QWidget()
        self.cards_container.setStyleSheet("background: transparent;")
        self.cards_layout = QVBoxLayout(self.cards_container)
        self.cards_layout.setContentsMargins(2, 2, 6, 2)
        self.cards_layout.setSpacing(12)
        self.cards_layout.addStretch(1)

        self.scroll.setWidget(self.cards_container)
        root_layout.addWidget(self.scroll, 1)

        self._update_filter_styles()
        self.reload_cards()

    def _set_filter(self, key: str) -> None:
        self._filter_color = key
        self._update_filter_styles()
        self.reload_cards()

    def _on_search_changed(self, text: str) -> None:
        self._search_query = text.strip().lower()
        self.reload_cards()

    def _update_filter_styles(self) -> None:
        for key, btn in self.filter_btns.items():
            active = key == self._filter_color
            btn.setChecked(active)
            theme = self.FILTER_THEMES.get(key, self.FILTER_THEMES["all"])
            btn.setStyleSheet(theme["active"] if active else theme["inactive"])

    def reload_cards(self) -> None:
        all_cards = self.storage.list_active_cards()

        # 更新各分类计数
        counts = {"all": len(all_cards), "red": 0, "yellow": 0, "blue": 0}
        for c in all_cards:
            col = c.get("box_color", "blue")
            if col in counts:
                counts[col] += 1

        self.filter_btns["all"].setText(f"全部 ({counts['all']})")
        self.filter_btns["red"].setText(f"● 超一周未复习 ({counts['red']})")
        self.filter_btns["yellow"].setText(f"● 又回来啦 ({counts['yellow']})")
        self.filter_btns["blue"].setText(f"● 正常 ({counts['blue']})")

        # 过滤
        filtered = []
        for c in all_cards:
            if self._filter_color != "all" and c.get("box_color") != self._filter_color:
                continue
            if self._search_query:
                haystack = f"{c['term']} {c.get('meanings_json', '')}".lower()
                if self._search_query not in haystack:
                    continue
            filtered.append(c)

        # 清空旧组件
        self._card_widgets.clear()
        while self.cards_layout.count():
            item = self.cards_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        if not filtered:
            empty_lbl = QLabel(
                "当前列表暂无卡片。\n在 Word 中划选生词或短语并点击『T』图标，或者点击右上角『模拟划词』即可添加。"
            )
            empty_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            empty_lbl.setStyleSheet("color: #64748B; font-size: 14px; padding: 60px;")
            self.cards_layout.addWidget(empty_lbl)
            self.cards_layout.addStretch(1)
            return

        for card_data in filtered:
            w = VocabCardWidget(card_data)
            w.reviewed.connect(self._on_card_reviewed)
            w.deleted.connect(self._on_card_deleted)
            self.cards_layout.addWidget(w)
            self._card_widgets[card_data["id"]] = w

        self.cards_layout.addStretch(1)

    def _on_card_reviewed(self, vocab_id: int) -> None:
        updated = self.storage.mark_reviewed(vocab_id)
        if updated and vocab_id in self._card_widgets:
            self._card_widgets[vocab_id].refresh_ui(updated)
            all_cards = self.storage.list_active_cards()
            counts = {"all": len(all_cards), "red": 0, "yellow": 0, "blue": 0}
            for c in all_cards:
                col = c.get("box_color", "blue")
                if col in counts:
                    counts[col] += 1
            self.filter_btns["all"].setText(f"全部 ({counts['all']})")
            self.filter_btns["red"].setText(f"● 超一周未复习 ({counts['red']})")
            self.filter_btns["yellow"].setText(f"● 又回来啦 ({counts['yellow']})")
            self.filter_btns["blue"].setText(f"● 正常 ({counts['blue']})")

    def _on_card_deleted(self, vocab_id: int) -> None:
        self.storage.soft_delete_card(vocab_id)
        self.reload_cards()

    def open_settings(self) -> None:
        dlg = SettingsDialog(self)
        dlg.exec()

    def open_update_dialog(self) -> None:
        dlg = UpdateDialog(self)
        dlg.exec()

    def _open_manual_test(self) -> None:
        if not self.on_manual_test:
            return
        dlg = ManualTestDialog(self.on_manual_test, self)
        dlg.exec()
