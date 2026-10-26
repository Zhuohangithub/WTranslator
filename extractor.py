import ctypes
import re
import time
from dataclasses import dataclass
from typing import List, Optional, Tuple

import pythoncom
import win32clipboard
import win32com.client
import win32gui
import win32process

WORD_REGEX = re.compile(r"[A-Za-z]+(?:['’-][A-Za-z]+)*")

# 常见缩写词集合（带句号但不代表句尾）
ABBREVIATIONS = {
    "mr.", "mrs.", "ms.", "dr.", "prof.", "sr.", "jr.", "st.", "mt.",
    "vs.", "etc.", "e.g.", "i.e.", "fig.", "figs.", "eq.", "al.",
    "no.", "vol.", "pp.", "ch.", "sec.", "inc.", "ltd.", "co.", "corp.",
    "u.s.", "u.k.", "a.m.", "p.m.", "jan.", "feb.", "mar.", "apr.",
    "jun.", "jul.", "aug.", "sep.", "sept.", "oct.", "nov.", "dec.",
}


@dataclass
class SelectionContext:
    selected_text: str
    selection_type: str  # 'word' | 'phrase' | 'sentence'
    word_count: int
    sentence: str
    sentence_word_count: int
    sentence_over_limit: bool
    sentence_limit: int
    source_app: str  # 'word' | 'global_uia' | 'global_clipboard'


def clean_text(raw: str) -> str:
    """清理 Word 控制字符（如表格单元格尾符 \r\x07、软回车等）。"""
    if not raw:
        return ""
    text = raw.replace("\r\x07", " ").replace("\x07", " ").replace("\x0b", "\n")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text.strip()


def extract_words(text: str) -> List[str]:
    """提取文本中的所有英文单词。"""
    return WORD_REGEX.findall(text or "")


def count_words(text: str) -> int:
    """统计英文单词数量。"""
    return len(extract_words(text))


def classify_selection(text: str) -> Tuple[str, int]:
    """
    根据选中内容分流：
    - 1 个英文单词 -> 'word'
    - 2 ~ 5 个英文单词（且无换行或句内终止标点） -> 'phrase'
    - 6 个词及以上（或包含多句/非纯词组） -> 'sentence'
    """
    cleaned = clean_text(text)
    words = extract_words(cleaned)
    n_words = len(words)

    if n_words == 0:
        return "sentence", 0

    # 如果包含换行符或明显的多句标点，直接归为句子/段落
    if "\n" in cleaned or re.search(r"[.?!;。？！；]\s+\S", cleaned):
        return "sentence", n_words

    if n_words == 1:
        return "word", 1
    if 2 <= n_words <= 5:
        return "phrase", n_words
    return "sentence", n_words


def _is_sentence_boundary(text: str, dot_idx: int) -> bool:
    """判断 text[dot_idx] 处的标点符号是否为真正的句子结束边界。"""
    ch = text[dot_idx]
    if ch in "?!？！。":
        return True
    if ch != ".":
        return False

    # 小数点判断，例如 3.14
    if (
        dot_idx > 0
        and dot_idx + 1 < len(text)
        and text[dot_idx - 1].isdigit()
        and text[dot_idx + 1].isdigit()
    ):
        return False

    # 提取句号前的词检查是否为常见缩写（如 Mr. / e.g. / U.S.）
    before = text[: dot_idx + 1].rstrip()
    token_match = re.search(r"([A-Za-z.]+)$", before)
    if token_match:
        token = token_match.group(1).lower()
        if token in ABBREVIATIONS:
            return False
        # 单字母缩写如 "J. K. Rowling"
        if re.fullmatch(r"[a-z]\.", token):
            return False

    # 句号后面如果紧跟小写字母（无空格或有空格但小写），通常不是句末
    after = text[dot_idx + 1 :].lstrip()
    if after and after[0].islower():
        return False

    return True


def extract_enclosing_sentence(
    paragraph: str, sel_start: int, sel_end: int
) -> str:
    """
    在段落文本中，围绕 [sel_start, sel_end] 隐形向前后搜索句首和句尾。
    """
    if not paragraph:
        return ""

    n = len(paragraph)
    sel_start = max(0, min(sel_start, n))
    sel_end = max(sel_start, min(sel_end, n))

    # 1. 向前搜索句首
    sent_start = 0
    for i in range(sel_start - 1, -1, -1):
        if paragraph[i] == "\n":
            sent_start = i + 1
            break
        if paragraph[i] in ".?!。？！" and _is_sentence_boundary(paragraph, i):
            sent_start = i + 1
            break

    # 2. 向后搜索句尾
    sent_end = n
    for i in range(sel_end, n):
        if paragraph[i] == "\n":
            sent_end = i
            break
        if paragraph[i] in ".?!。？！" and _is_sentence_boundary(paragraph, i):
            sent_end = i + 1
            break

    sentence = paragraph[sent_start:sent_end].strip()
    return re.sub(r"\s+", " ", sentence)


def is_word_foreground() -> bool:
    """
    微秒级检查当前活动窗口是否为 Microsoft Word（或 WPS 文字）。
    优先通过 Win32 窗口类名 'OpusApp' 零开销判定，回退到原生进程名查询。
    """
    try:
        hwnd = win32gui.GetForegroundWindow()
        if not hwnd:
            return False
        cls_name = win32gui.GetClassName(hwnd)
        if cls_name in ("OpusApp", "KWPSFrame"):
            return True

        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        if not pid:
            return False
        h_proc = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if not h_proc:
            return False
        try:
            buf = ctypes.create_unicode_buffer(260)
            size = ctypes.c_ulong(260)
            if ctypes.windll.kernel32.QueryFullProcessImageNameW(
                h_proc, 0, buf, ctypes.byref(size)
            ):
                exe_name = buf.value.rsplit("\\", 1)[-1].lower()
                return exe_name in ("winword.exe", "wps.exe")
        finally:
            ctypes.windll.kernel32.CloseHandle(h_proc)
    except Exception:
        pass
    return False


def has_word_selection_fast() -> bool:
    """
    在 Word 中快速、隐形地检查当前是否有选中的非空文本（不修改任何 UI 状态）。
    用于决定松开鼠标后是否弹出半透明 'T' 图标。
    """
    try:
        pythoncom.CoInitialize()
        app = win32com.client.GetActiveObject("Word.Application")
        sel = app.Selection
        if sel is None or sel.Range is None:
            return False
        if sel.Range.Start == sel.Range.End:
            return False
        txt = clean_text(sel.Range.Text)
        return len(txt) > 0
    except Exception:
        return False


def _extract_from_word_com() -> Optional[Tuple[str, str]]:
    """
    第一级通道：通过 Word COM 接口隐形提取【选中文本】与【所在整句】。
    使用 Range.Duplicate 在内存中操作，Word 界面选区绝不闪烁或移动。
    """
    try:
        pythoncom.CoInitialize()
        app = win32com.client.GetActiveObject("Word.Application")
        sel_range = app.Selection.Range
        if sel_range is None or sel_range.Start == sel_range.End:
            return None

        selected_text = clean_text(sel_range.Text)
        if not selected_text:
            return None

        # 在内存中克隆所在段落 Range，隐形计算句首与句尾
        sentence = ""
        try:
            para_range = sel_range.Paragraphs(1).Range.Duplicate
            raw_para = para_range.Text or ""
            rel_start = max(0, sel_range.Start - para_range.Start)
            rel_end = rel_start + len(sel_range.Text or "")

            # 若偏移位置与选中内容吻合，用缩写感知切句器提取整句
            extracted = extract_enclosing_sentence(raw_para, rel_start, rel_end)
            if extracted and selected_text.lower() in extracted.lower():
                sentence = clean_text(extracted)
        except Exception:
            pass

        # 若段落级切分未命中（如跨段落或特殊域），回退到克隆 Range.Expand(wdSentence=3)
        if not sentence:
            try:
                dup_range = sel_range.Duplicate
                dup_range.Expand(3)  # wdSentence = 3
                sentence = clean_text(dup_range.Text)
            except Exception:
                sentence = selected_text

        return selected_text, sentence
    except Exception:
        return None


def _get_clipboard_text() -> str:
    try:
        win32clipboard.OpenClipboard()
        try:
            if win32clipboard.IsClipboardFormatAvailable(win32clipboard.CF_UNICODETEXT):
                return win32clipboard.GetClipboardData(win32clipboard.CF_UNICODETEXT) or ""
        finally:
            win32clipboard.CloseClipboard()
    except Exception:
        pass
    return ""


def _set_clipboard_text(text: str) -> None:
    try:
        win32clipboard.OpenClipboard()
        try:
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardData(win32clipboard.CF_UNICODETEXT, text)
        finally:
            win32clipboard.CloseClipboard()
    except Exception:
        pass


def _extract_from_clipboard() -> Optional[Tuple[str, str]]:
    """
    全局模式回退通道：备份原剪贴板 -> 发送 Ctrl+C -> 读取选中词 -> 还原原剪贴板。
    仅在用户点击 'T' 字图标时触发，平时绝不干扰剪贴板。
    """
    old_clip = _get_clipboard_text()
    sentinel = f"__WTRANSLATOR_EMPTY_{time.time()}__"
    _set_clipboard_text(sentinel)

    # 确保所有修饰键处于释放状态并模拟 Ctrl+C
    VK_CONTROL = 0x11
    VK_C = 0x43
    KEYEVENTF_KEYUP = 0x0002
    user32 = ctypes.windll.user32

    user32.keybd_event(VK_CONTROL, 0, 0, 0)
    user32.keybd_event(VK_C, 0, 0, 0)
    time.sleep(0.02)
    user32.keybd_event(VK_C, 0, KEYEVENTF_KEYUP, 0)
    user32.keybd_event(VK_CONTROL, 0, KEYEVENTF_KEYUP, 0)

    copied = sentinel
    for _ in range(8):
        time.sleep(0.025)
        copied = _get_clipboard_text()
        if copied != sentinel:
            break

    # 恢复原剪贴板
    _set_clipboard_text(old_clip)

    if copied == sentinel or not copied.strip():
        return None

    cleaned = clean_text(copied)
    return (cleaned, "") if cleaned else None


def capture_selection(
    sentence_word_limit: int = 50, global_mode: bool = False
) -> Optional[SelectionContext]:
    """
    统一取词与隐形搜句入口：
    1. 若当前活动窗口是 Word，通过 Word COM 隐形获取选中词与整句；
    2. 若开启全局模式且当前活动窗口不是 Word，使用剪贴板安全取词；
    3. 检查所在句子是否超出词数上限（sentence_word_limit）。
    """
    extracted = None
    source_app = "word"

    if is_word_foreground():
        extracted = _extract_from_word_com()
        source_app = "word"

    if extracted is None:
        if not global_mode:
            return None
        extracted = _extract_from_clipboard()
        source_app = "global_clipboard"
        if extracted is None:
            return None

    selected_text, full_sentence = extracted
    sel_type, word_cnt = classify_selection(selected_text)

    if sel_type == "sentence":
        # 句子或段落模式直接翻译选中内容本身
        return SelectionContext(
            selected_text=selected_text,
            selection_type="sentence",
            word_count=word_cnt,
            sentence="",
            sentence_word_count=word_cnt,
            sentence_over_limit=False,
            sentence_limit=sentence_word_limit,
            source_app=source_app,
        )

    sent_word_cnt = count_words(full_sentence) if full_sentence else 0
    over_limit = sent_word_cnt > sentence_word_limit

    # 超过词数上限则不向 AI 发送该句子
    effective_sentence = "" if over_limit else full_sentence

    return SelectionContext(
        selected_text=selected_text,
        selection_type=sel_type,
        word_count=word_cnt,
        sentence=effective_sentence,
        sentence_word_count=sent_word_cnt,
        sentence_over_limit=over_limit,
        sentence_limit=sentence_word_limit,
        source_app=source_app,
    )
