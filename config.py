import json
from pathlib import Path
import sys
from typing import Any, Dict


def get_app_dir() -> Path:
    """获取程序持久化目录（兼容 PyInstaller 打包后的 .exe 与源码运行模式）。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def get_resource_dir() -> Path:
    """获取内置只读资源目录（兼容 PyInstaller _MEIPASS）。"""
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


def get_current_version() -> str:
    """读取本地当前版本号。"""
    for base in (APP_DIR, get_resource_dir()):
        v_file = base / "version.json"
        if v_file.exists():
            try:
                data = json.loads(v_file.read_text(encoding="utf-8"))
                return str(data.get("version", "1.0.0"))
            except Exception:
                pass
    return "1.0.0"


def load_config() -> Dict[str, Any]:
    """加载本地配置，若不存在则生成默认配置文件。"""
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
    return cfg


def save_config(cfg: Dict[str, Any]) -> None:
    """保存配置到 config.json。"""
    merged = DEFAULT_CONFIG.copy()
    merged.update(cfg)
    CONFIG_PATH.write_text(
        json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8"
    )
