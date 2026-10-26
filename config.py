import json
from pathlib import Path
import shutil
import sys
from typing import Any, Dict, Optional, Tuple


def get_app_dir() -> Path:
    """获取程序安装目录（兼容 PyInstaller 打包环境与源码运行环境）。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def get_resource_dir() -> Path:
    """获取内置资源目录。"""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(getattr(sys, "_MEIPASS")).resolve()
    return Path(__file__).resolve().parent


APP_DIR = get_app_dir()
CONFIG_PATH = APP_DIR / "config.json"

DEFAULT_CONFIG: Dict[str, Any] = {
    "api_key": "",
    "base_url": "https://api.deepseek.com/v1",
    "model": "deepseek-chat",
    "sentence_word_limit": 50,
    "global_mode": False,  # False: 仅在 Microsoft Word 中生效; True: 全局划词生效
    "icon_opacity": 0.78,
    "github_repo": "Zhuohangithub/WTranslator",
    "github_branch": "main",
    "data_dir": "",  # 为空代表默认存储在程序安装目录 (APP_DIR)
}

PROVIDER_PRESETS = {
    "DeepSeek (推荐)": {
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
    },
    "硅基流动 (SiliconFlow)": {
        "base_url": "https://api.siliconflow.cn/v1",
        "model": "Qwen/Qwen2.5-7B-Instruct",
    },
    "通义千问 (DashScope)": {
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen-plus",
    },
    "智谱清言 (GLM)": {
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "model": "glm-4-flash",
    },
    "OpenAI Official": {
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
    },
}

# 内存级配置缓存，避免高频读取磁盘影响响应速度
_CONFIG_CACHE: Optional[Dict[str, Any]] = None


def get_current_version() -> str:
    """读取本地当前版本号。"""
    for base in (APP_DIR, get_resource_dir()):
        v_file = base / "version.json"
        if v_file.exists():
            try:
                data = json.loads(v_file.read_text(encoding="utf-8"))
                return str(data.get("version", "1.0.2"))
            except Exception:
                pass
    return "1.0.2"


def load_config(force_reload: bool = False) -> Dict[str, Any]:
    """加载配置（带内存缓存，零磁盘延迟）。"""
    global _CONFIG_CACHE
    if _CONFIG_CACHE is not None and not force_reload:
        return _CONFIG_CACHE

    cfg = DEFAULT_CONFIG.copy()
    if CONFIG_PATH.exists():
        try:
            user_cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(user_cfg, dict):
                cfg.update(user_cfg)
        except Exception:
            pass
    else:
        save_config(cfg)
    _CONFIG_CACHE = cfg
    return cfg


def save_config(cfg: Dict[str, Any]) -> None:
    """保存配置到安装目录下的 config.json 并更新内存缓存。"""
    global _CONFIG_CACHE
    merged = DEFAULT_CONFIG.copy()
    merged.update(cfg)
    _CONFIG_CACHE = merged
    CONFIG_PATH.write_text(
        json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def get_data_dir() -> Path:
    """
    获取生词数据库存储目录：
    - 若用户在设置中指定了自定义目录，则使用该目录；
    - 否则默认使用程序安装目录 (APP_DIR)。
    """
    cfg = load_config()
    custom = (cfg.get("data_dir") or "").strip()
    if custom:
        p = Path(custom)
        try:
            p.mkdir(parents=True, exist_ok=True)
            return p.resolve()
        except Exception:
            pass
    return APP_DIR


def get_db_path() -> Path:
    """获取 SQLite 数据库文件 (wtranslator.db) 的完整路径。"""
    return get_data_dir() / "wtranslator.db"


def migrate_data_dir(new_dir_str: str) -> Path:
    """
    将数据库安全迁移至新的存储目录（使用 SQLite 原生 Online Backup API 完整拷贝主库与 WAL 日志）。
    """
    import sqlite3

    old_db = get_db_path()
    target_dir = Path(new_dir_str.strip()) if new_dir_str.strip() else APP_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    new_db = target_dir.resolve() / "wtranslator.db"

    if old_db.exists() and old_db.resolve() != new_db.resolve() and not new_db.exists():
        src_conn = sqlite3.connect(old_db)
        dst_conn = sqlite3.connect(new_db)
        try:
            with dst_conn:
                src_conn.backup(dst_conn)
        finally:
            dst_conn.close()
            src_conn.close()

    cfg = load_config().copy()
    cfg["data_dir"] = "" if target_dir.resolve() == APP_DIR.resolve() else str(target_dir.resolve())
    save_config(cfg)
    return new_db


def ensure_app_ico() -> Path:
    """确保安装目录下存在高清 Windows 图标文件 app.ico（若不存在则用 Qt 矢量绘制生成）。"""
    import struct
    from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QRect, Qt
    from PySide6.QtGui import QColor, QFont, QGuiApplication, QImage, QPainter, QPainterPath, QPen

    ico_path = APP_DIR / "app.ico"
    if ico_path.exists() and ico_path.stat().st_size > 100:
        return ico_path

    if QGuiApplication.instance() is None:
        _app = QGuiApplication(sys.argv[:1])

    size = 256
    img = QImage(size, size, QImage.Format.Format_ARGB32)
    img.fill(Qt.GlobalColor.transparent)

    painter = QPainter(img)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)

    rect = QRect(16, 16, 224, 224)
    path = QPainterPath()
    path.addRoundedRect(rect, 52, 52)
    painter.fillPath(path, QColor(72, 99, 128))

    pen = QPen(QColor(156, 176, 198, 200), 6)
    painter.setPen(pen)
    painter.drawPath(path)

    painter.setPen(QColor(248, 250, 252))
    font = QFont("Georgia", 136, QFont.Weight.Bold)
    painter.setFont(font)
    painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, "T")
    painter.end()

    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    img.save(buf, "PNG")
    png_bytes = bytes(ba.data())

    # 封装为标准 Windows Vista+ PNG-ICO 格式（22字节头 + PNG 数据）
    ico_header = struct.pack("<HHH", 0, 1, 1)
    ico_entry = struct.pack(
        "<BBBBHHII",
        0,  # 0 表示 256px 宽度
        0,  # 0 表示 256px 高度
        0,
        0,
        1,
        32,
        len(png_bytes),
        22,
    )
    ico_path.write_bytes(ico_header + ico_entry + png_bytes)
    return ico_path


def create_desktop_shortcut() -> Tuple[bool, str]:
    """在系统桌面创建带专属 T 字图标的 WTranslator 快捷方式 (.lnk)。"""
    try:
        import ctypes
        import pythoncom
        import win32com.client

        ico_path = ensure_app_ico()
        pythoncom.CoInitialize()
        shell = win32com.client.Dispatch("WScript.Shell")
        desktop = Path(shell.SpecialFolders("Desktop"))
        lnk_path = desktop / "WTranslator.lnk"

        # 先删除旧的无图标快捷方式，确保 Windows 重新加载图标缓存
        if lnk_path.exists():
            try:
                lnk_path.unlink()
            except Exception:
                pass

        shortcut = shell.CreateShortCut(str(lnk_path))
        if getattr(sys, "frozen", False):
            shortcut.TargetPath = str(Path(sys.executable).resolve())
        else:
            pyw = Path(sys.executable).with_name("pythonw.exe")
            target_py = pyw if pyw.exists() else Path(sys.executable)
            shortcut.TargetPath = str(target_py)
            shortcut.Arguments = f'"{APP_DIR / "main.py"}"'
        shortcut.IconLocation = f"{ico_path.resolve()},0"
        shortcut.WorkingDirectory = str(APP_DIR)
        shortcut.Description = "WTranslator - Word 划词翻译与英语复习助手"
        shortcut.save()

        # 通知 Windows 资源管理器立即刷新桌面图标缓存
        ctypes.windll.shell32.SHChangeNotify(0x08000000, 0x0000, None, None)
        return True, str(lnk_path)
    except Exception as exc:
        return False, str(exc)
