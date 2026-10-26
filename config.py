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
    将数据库迁移至新的存储目录（若原目录存在 wtranslator.db 且新目录尚无，则自动复制过去）。
    """
    old_db = get_db_path()
    target_dir = Path(new_dir_str.strip()) if new_dir_str.strip() else APP_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    new_db = target_dir.resolve() / "wtranslator.db"

    if old_db.exists() and old_db.resolve() != new_db.resolve() and not new_db.exists():
        shutil.copy2(old_db, new_db)

    cfg = load_config().copy()
    cfg["data_dir"] = "" if target_dir.resolve() == APP_DIR.resolve() else str(target_dir.resolve())
    save_config(cfg)
    return new_db


def create_desktop_shortcut() -> Tuple[bool, str]:
    """在系统桌面创建 WTranslator 快捷方式 (.lnk)。"""
    try:
        import pythoncom
        import win32com.client

        pythoncom.CoInitialize()
        shell = win32com.client.Dispatch("WScript.Shell")
        desktop = Path(shell.SpecialFolders("Desktop"))
        lnk_path = desktop / "WTranslator.lnk"

        shortcut = shell.CreateShortCut(str(lnk_path))
        if getattr(sys, "frozen", False):
            shortcut.TargetPath = str(Path(sys.executable).resolve())
            shortcut.IconLocation = str(Path(sys.executable).resolve())
        else:
            pyw = Path(sys.executable).with_name("pythonw.exe")
            target_py = pyw if pyw.exists() else Path(sys.executable)
            shortcut.TargetPath = str(target_py)
            shortcut.Arguments = f'"{APP_DIR / "main.py"}"'
        shortcut.WorkingDirectory = str(APP_DIR)
        shortcut.Description = "WTranslator - Word 划词翻译与英语复习助手"
        shortcut.save()
        return True, str(lnk_path)
    except Exception as exc:
        return False, str(exc)
