import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from config import APP_DIR

DB_PATH = APP_DIR / "wtranslator.db"
OVERDUE_DAYS = 7.0  # 超过 7 天（一周）未复习标红


def normalize_term(text: str) -> str:
    """将词或短语归一化为小写单空格分隔的键。"""
    cleaned = re.sub(r"\s+", " ", text.strip())
    cleaned = cleaned.strip(".,;:!?\"'()[]{}“”‘’")
    return cleaned.lower()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def format_elapsed(seconds: float) -> str:
    """将秒数格式化为可读的中文时间跨度。"""
    if seconds < 0:
        seconds = 0
    if seconds < 60:
        return f"{int(seconds)}秒"
    minutes = seconds / 60
    if minutes < 60:
        return f"{int(minutes)}分钟"
    hours = minutes / 60
    if hours < 24:
        return f"{int(hours)}小时{int(minutes % 60)}分"
    days = int(hours // 24)
    rem_hours = int(hours % 24)
    return f"{days}天{rem_hours}小时" if rem_hours > 0 else f"{days}天"


class VocabStorage:
    """管理生词、短语、历史例句及复习卡片状态机的 SQLite 存储层。"""

    def __init__(self, db_path: Path = DB_PATH) -> None:
        self.db_path = db_path
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS vocab (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    term_key TEXT UNIQUE NOT NULL,
                    term TEXT NOT NULL,
                    term_type TEXT NOT NULL,          -- 'word' | 'phrase'
                    phonetic TEXT DEFAULT '',
                    meanings_json TEXT DEFAULT '[]',  -- [{"pos": "n.", "meaning": "..."}]
                    card_state TEXT DEFAULT 'normal', -- 'normal' | 'deleted' | 'returned'
                    ever_returned INTEGER DEFAULT 0,
                    review_count INTEGER DEFAULT 0,
                    last_interval_sec REAL DEFAULT 0,
                    last_reviewed_at TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS sentences (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    vocab_id INTEGER NOT NULL,
                    sentence TEXT NOT NULL,
                    translation TEXT DEFAULT '',
                    usage_note TEXT DEFAULT '',
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (vocab_id) REFERENCES vocab(id)
                );

                CREATE INDEX IF NOT EXISTS idx_sentences_vocab_id ON sentences(vocab_id);
                """
            )

    def get_by_term(self, term: str) -> Optional[Dict[str, Any]]:
        """根据单词或短语查询本地记录（含已软删除记录及全部历史例句）。"""
        key = normalize_term(term)
        if not key:
            return None
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM vocab WHERE term_key = ?", (key,)
            ).fetchone()
            if not row:
                return None
            return self._enrich_row(conn, dict(row))

    def save_new_vocab(
        self,
        term: str,
        term_type: str,
        phonetic: str,
        meanings: List[Dict[str, str]],
        sentence: str = "",
        sentence_translation: str = "",
        usage_note: str = "",
    ) -> Dict[str, Any]:
        """首次保存新单词或新短语，及可选的首条上下文句子。"""
        key = normalize_term(term)
        display_term = re.sub(r"\s+", " ", term.strip()).strip(".,;:!?\"'()[]{}“”‘’")
        ts = now_iso()
        meanings_str = json.dumps(meanings, ensure_ascii=False)

        with self._connect() as conn:
            existing = conn.execute(
                "SELECT id FROM vocab WHERE term_key = ?", (key,)
            ).fetchone()
            if existing:
                return self.incremental_update(
                    term=term,
                    sentence=sentence,
                    sentence_translation=sentence_translation,
                    usage_note=usage_note,
                )

            cur = conn.execute(
                """
                INSERT INTO vocab (
                    term_key, term, term_type, phonetic, meanings_json,
                    card_state, ever_returned, review_count, last_interval_sec,
                    last_reviewed_at, created_at
                ) VALUES (?, ?, ?, ?, ?, 'normal', 0, 0, 0, ?, ?)
                """,
                (key, display_term, term_type, phonetic, meanings_str, ts, ts),
            )
            vocab_id = cur.lastrowid

            if sentence and sentence.strip():
                conn.execute(
                    """
                    INSERT INTO sentences (vocab_id, sentence, translation, usage_note, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        vocab_id,
                        sentence.strip(),
                        sentence_translation.strip(),
                        usage_note.strip(),
                        ts,
                    ),
                )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM vocab WHERE id = ?", (vocab_id,)
            ).fetchone()
            return self._enrich_row(conn, dict(row))

    def incremental_update(
        self,
        term: str,
        sentence: str = "",
        sentence_translation: str = "",
        usage_note: str = "",
    ) -> Dict[str, Any]:
        """
        重复划词增量更新：
        1. 若卡片此前被软删除 ('deleted')，将其重新激活为 'returned'（黄框：“又回来啦”）；
        2. 若提供了新句子且未重复，则追加存入 sentences 表。
        """
        key = normalize_term(term)
        ts = now_iso()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM vocab WHERE term_key = ?", (key,)
            ).fetchone()
            if not row:
                raise ValueError(f"Term not found for incremental update: {term}")

            vocab_id = row["id"]
            was_deleted = row["card_state"] == "deleted"

            if was_deleted:
                conn.execute(
                    """
                    UPDATE vocab
                    SET card_state = 'returned',
                        ever_returned = 1,
                        last_reviewed_at = ?
                    WHERE id = ?
                    """,
                    (ts, vocab_id),
                )

            if sentence and sentence.strip():
                norm_sent = re.sub(r"\s+", " ", sentence.strip())
                dup = conn.execute(
                    "SELECT id FROM sentences WHERE vocab_id = ? AND sentence = ?",
                    (vocab_id, norm_sent),
                ).fetchone()
                if not dup:
                    conn.execute(
                        """
                        INSERT INTO sentences (vocab_id, sentence, translation, usage_note, created_at)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            vocab_id,
                            norm_sent,
                            sentence_translation.strip(),
                            usage_note.strip(),
                            ts,
                        ),
                    )
                elif usage_note or sentence_translation:
                    conn.execute(
                        """
                        UPDATE sentences
                        SET translation = COALESCE(NULLIF(?, ''), translation),
                            usage_note = COALESCE(NULLIF(?, ''), usage_note)
                        WHERE id = ?
                        """,
                        (sentence_translation.strip(), usage_note.strip(), dup["id"]),
                    )

            conn.commit()
            updated = conn.execute(
                "SELECT * FROM vocab WHERE id = ?", (vocab_id,)
            ).fetchone()
            res = self._enrich_row(conn, dict(updated))
            res["just_resurrected"] = was_deleted
            return res

    def mark_reviewed(self, vocab_id: int) -> Optional[Dict[str, Any]]:
        """
        点击卡片完成一次复习：
        - 复习次数 +1
        - 记录距离上次复习的时间跨度 (last_interval_sec)
        - 更新 last_reviewed_at 为当前时间
        - 无论此前是红框（超期）还是黄框（又回来啦），恢复为正常蓝色卡片框 ('normal')
        """
        now_dt = datetime.now(timezone.utc)
        ts = now_dt.isoformat()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM vocab WHERE id = ?", (vocab_id,)
            ).fetchone()
            if not row:
                return None

            last_dt = datetime.fromisoformat(row["last_reviewed_at"])
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=timezone.utc)
            interval_sec = max(0.0, (now_dt - last_dt).total_seconds())

            conn.execute(
                """
                UPDATE vocab
                SET review_count = review_count + 1,
                    last_interval_sec = ?,
                    last_reviewed_at = ?,
                    card_state = 'normal'
                WHERE id = ?
                """,
                (interval_sec, ts, vocab_id),
            )
            conn.commit()
            updated = conn.execute(
                "SELECT * FROM vocab WHERE id = ?", (vocab_id,)
            ).fetchone()
            return self._enrich_row(conn, dict(updated))

    def soft_delete_card(self, vocab_id: int) -> None:
        """软删除卡片（仅从复习面板隐藏，保留词条和例句，下次划到触发黄框“又回来啦”）。"""
        with self._connect() as conn:
            conn.execute(
                "UPDATE vocab SET card_state = 'deleted' WHERE id = ?", (vocab_id,)
            )
            conn.commit()

    def list_active_cards(self) -> List[Dict[str, Any]]:
        """获取所有未删除的复习卡片（按状态优先级与时间排序）。"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM vocab WHERE card_state != 'deleted' ORDER BY id DESC"
            ).fetchall()
            cards = [self._enrich_row(conn, dict(r)) for r in rows]
        # 排序优先级：红框(超期) -> 黄框(又回来啦) -> 蓝框(正常)
        priority = {"red": 0, "yellow": 1, "blue": 2}
        cards.sort(key=lambda c: (priority.get(c["box_color"], 3), -c["id"]))
        return cards

    def _enrich_row(
        self, conn: sqlite3.Connection, data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """解析 JSON 字段、关联例句列表，并计算当前卡片框颜色与距离上次复习时间。"""
        try:
            data["meanings"] = json.loads(data.get("meanings_json") or "[]")
        except Exception:
            data["meanings"] = []

        sents = conn.execute(
            "SELECT * FROM sentences WHERE vocab_id = ? ORDER BY id DESC",
            (data["id"],),
        ).fetchall()
        data["sentences"] = [dict(s) for s in sents]

        now_dt = datetime.now(timezone.utc)
        try:
            last_dt = datetime.fromisoformat(data["last_reviewed_at"])
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=timezone.utc)
        except Exception:
            last_dt = now_dt

        elapsed_sec = max(0.0, (now_dt - last_dt).total_seconds())
        days_since = elapsed_sec / 86400.0
        data["elapsed_sec"] = elapsed_sec
        data["days_since"] = days_since
        data["elapsed_text"] = format_elapsed(elapsed_sec)
        data["last_interval_text"] = (
            format_elapsed(data["last_interval_sec"])
            if data.get("last_interval_sec", 0) > 0
            else "首次复习"
        )

        # 颜色判定规则：
        # 1. 距离上次复习（或首次收录）超过 7 天 -> 红色卡片框 ('red')
        # 2. 曾被删除后再次划到 ('returned') -> 黄色卡片框 ('yellow')
        # 3. 其余正常状态 -> 蓝色卡片框 ('blue')
        if days_since > OVERDUE_DAYS:
            data["box_color"] = "red"
        elif data.get("card_state") == "returned":
            data["box_color"] = "yellow"
        else:
            data["box_color"] = "blue"

        return data
