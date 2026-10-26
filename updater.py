from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import List, Optional, Tuple

import httpx

from config import APP_DIR, get_current_version, load_config


@dataclass
class UpdateInfo:
    has_update: bool
    local_version: str
    remote_version: str
    changelog: str = ""
    exe_url: str = ""
    files: List[str] = field(default_factory=list)
    error: str = ""


def _parse_version_tuple(v: str) -> Tuple[int, ...]:
    cleaned = v.strip().lstrip("vV")
    parts = []
    for p in cleaned.split("."):
        digits = "".join(ch for ch in p if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts or [0])


def is_newer_version(remote_ver: str, local_ver: str) -> bool:
    return _parse_version_tuple(remote_ver) > _parse_version_tuple(local_ver)


def _fetch_url_bytes(urls: List[str], timeout: float = 15.0) -> bytes:
    """依次尝试直连与镜像地址下载字节流。"""
    last_err: Optional[Exception] = None
    headers = {"User-Agent": "WTranslator-Updater/1.0"}
    with httpx.Client(timeout=timeout, follow_redirects=True, headers=headers) as client:
        for url in urls:
            try:
                resp = client.get(url)
                if resp.status_code == 200:
                    return resp.content
            except Exception as e:
                last_err = e
    raise RuntimeError(f"无法连接 GitHub 更新源：{last_err or 'HTTP 请求失败'}")


def check_for_updates(
    repo: Optional[str] = None, branch: Optional[str] = None
) -> UpdateInfo:
    """检查 GitHub 仓库的 version.json 与 Releases 最新版本。"""
    cfg = load_config()
    repo = (repo or cfg.get("github_repo") or "Zhuohangithub/WTranslator").strip("/")
    branch = (branch or cfg.get("github_branch") or "main").strip()
    local_ver = get_current_version()

    raw_urls = [
        f"https://raw.githubusercontent.com/{repo}/{branch}/version.json",
        f"https://fastly.jsdelivr.net/gh/{repo}@{branch}/version.json",
    ]

    try:
        raw_bytes = _fetch_url_bytes(raw_urls, timeout=10.0)
        data = json.loads(raw_bytes.decode("utf-8"))
        remote_ver = str(data.get("version") or local_ver)
        changelog = str(data.get("changelog") or "")
        files = list(data.get("files") or [])

        exe_url = (
            f"https://github.com/{repo}/releases/latest/download/WTranslator.exe"
        )
        has_upd = is_newer_version(remote_ver, local_ver)
        return UpdateInfo(
            has_update=has_upd,
            local_version=local_ver,
            remote_version=remote_ver,
            changelog=changelog,
            exe_url=exe_url,
            files=files,
        )
    except Exception as exc:
        return UpdateInfo(
            has_update=False,
            local_version=local_ver,
            remote_version=local_ver,
            error=str(exc),
        )


def apply_online_update(
    info: Optional[UpdateInfo] = None,
    repo: Optional[str] = None,
    branch: Optional[str] = None,
) -> Tuple[bool, str]:
    """
    执行在线更新：
    - 若当前为 PyInstaller 打包后的 .exe 环境：下载最新 WTranslator.exe 并通过自替换脚本更新重启；
    - 若当前为源码/便携环境：优先通过 git pull 或 GitHub Raw 拉取最新代码文件并重启（保留数据库与配置）。
    """
    cfg = load_config()
    repo = (repo or cfg.get("github_repo") or "Zhuohangithub/WTranslator").strip("/")
    branch = (branch or cfg.get("github_branch") or "main").strip()

    if info is None:
        info = check_for_updates(repo, branch)
        if info.error:
            return False, info.error

    # 模式 1：独立 .exe 自更新
    if getattr(sys, "frozen", False):
        exe_urls = [
            f"https://github.com/{repo}/releases/latest/download/WTranslator.exe",
            f"https://mirror.ghproxy.com/https://github.com/{repo}/releases/latest/download/WTranslator.exe",
        ]
        try:
            exe_bytes = _fetch_url_bytes(exe_urls, timeout=60.0)
            new_exe = APP_DIR / "WTranslator_new.exe"
            new_exe.write_bytes(exe_bytes)

            cur_exe = Path(sys.executable).resolve()
            bat_path = APP_DIR / "_update_runner.bat"
            bat_content = (
                "@echo off\r\n"
                "timeout /t 2 /nobreak >nul\r\n"
                f'move /y "{new_exe}" "{cur_exe}" >nul\r\n'
                f'start "" "{cur_exe}"\r\n'
                'del "%~f0"\r\n'
            )
            bat_path.write_text(bat_content, encoding="mbcs", errors="ignore")
            subprocess.Popen(
                ["cmd.exe", "/c", str(bat_path)],
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            return True, "新版本 EXE 下载完成，即将自动替换并重启！"
        except Exception as exc:
            return False, f"下载或替换 EXE 失败：{exc}"

    # 模式 2：源码/轻量桌面端热更新（秒级同步最新修复）
    try:
        files_to_sync = info.files or [
            "config.py",
            "extractor.py",
            "llm_service.py",
            "storage.py",
            "ui_floating.py",
            "ui_review.py",
            "updater.py",
            "main.py",
            "version.json",
        ]
        for fname in files_to_sync:
            urls = [
                f"https://raw.githubusercontent.com/{repo}/{branch}/{fname}",
                f"https://fastly.jsdelivr.net/gh/{repo}@{branch}/{fname}",
            ]
            content = _fetch_url_bytes(urls, timeout=15.0)
            target = APP_DIR / fname
            target.write_bytes(content)

        return True, f"已成功从 GitHub ({repo}) 同步最新代码 (v{info.remote_version})！"
    except Exception as exc:
        return False, f"在线热更新失败：{exc}"


def restart_application() -> None:
    """重启当前桌面端进程。"""
    if getattr(sys, "frozen", False):
        subprocess.Popen([sys.executable])
    else:
        main_script = str(APP_DIR / "main.py")
        subprocess.Popen([sys.executable, main_script])
    os._exit(0)
