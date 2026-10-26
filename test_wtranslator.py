from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from extractor import (
    SelectionContext,
    classify_selection,
    count_words,
    extract_enclosing_sentence,
)
from llm_service import LLMService, process_selection
from storage import VocabStorage


def test_classify_selection():
    assert classify_selection("ubiquitous") == ("word", 1)
    assert classify_selection("take into account") == ("phrase", 3)
    assert classify_selection("make up") == ("phrase", 2)
    assert classify_selection(
        "The quick brown fox jumps over the lazy dog."
    )[0] == "sentence"


def test_invisible_sentence_boundary_extraction():
    paragraph = (
        "First sentence ends here. Dr. Smith argued, e.g. in the U.S. report, "
        "that ephemeral phenomena require careful observation! Next sentence starts."
    )
    target = "ephemeral"
    idx = paragraph.index(target)
    sentence = extract_enclosing_sentence(paragraph, idx, idx + len(target))
    assert sentence == (
        "Dr. Smith argued, e.g. in the U.S. report, "
        "that ephemeral phenomena require careful observation!"
    )


def test_word_flow_and_incremental_update_and_card_states(tmp_path: Path):
    db_file = tmp_path / "test.db"
    storage = VocabStorage(db_path=db_file)
    cfg = {"api_key": "test-key", "base_url": "https://api.example.com/v1", "model": "test"}

    with patch.object(LLMService, "query_word") as mock_q_word, patch.object(
        LLMService, "query_incremental_sentence"
    ) as mock_q_inc:
        mock_q_word.return_value = {
            "phonetic": "/ɪˈfem.ər.əl/",
            "meanings": [
                {"pos": "adj.", "meaning": "短暂的；朝生暮死的"},
                {"pos": "n.", "meaning": "短暂存在的事物"},
            ],
            "context_usage": "此处作表语形容词，修饰网络时代的名气转瞬即逝。",
            "sentence_translation": "网络时代的名气往往是短暂的。",
        }

        # 1. 首次划词：调用 query_word 并入库，卡片应为蓝框 ('blue')
        ctx1 = SelectionContext(
            selected_text="ephemeral",
            selection_type="word",
            word_count=1,
            sentence="Fame in the internet age is often ephemeral.",
            sentence_word_count=8,
            sentence_over_limit=False,
            sentence_limit=50,
            source_app="word",
        )
        res1 = process_selection(ctx1, storage, cfg)
        assert res1.mode == "word"
        assert res1.stored is True
        assert res1.is_incremental is False
        assert mock_q_word.call_count == 1

        cards = storage.list_active_cards()
        assert len(cards) == 1
        card = cards[0]
        assert card["box_color"] == "blue"
        assert len(card["sentences"]) == 1

        # 2. 重复划词（新句子）：走增量更新接口（只发句子，不调 query_word）
        mock_q_inc.return_value = {
            "context_usage": "作定语修饰 fashion trends，指潮流变化极快。",
            "sentence_translation": "她从不追逐短暂的时尚潮流。",
        }
        ctx2 = SelectionContext(
            selected_text="Ephemeral",
            selection_type="word",
            word_count=1,
            sentence="She never chases ephemeral fashion trends.",
            sentence_word_count=6,
            sentence_over_limit=False,
            sentence_limit=50,
            source_app="word",
        )
        res2 = process_selection(ctx2, storage, cfg)
        assert res2.is_incremental is True
        assert mock_q_word.call_count == 1  # 未再次调用完整字典接口
        assert mock_q_inc.call_count == 1

        cards = storage.list_active_cards()
        assert len(cards[0]["sentences"]) == 2

        # 3. 超过 7 天未复习 -> 自动变为红色卡片框 ('red')
        vocab_id = cards[0]["id"]
        eight_days_ago = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
        with storage._connect() as conn:
            conn.execute(
                "UPDATE vocab SET last_reviewed_at = ? WHERE id = ?",
                (eight_days_ago, vocab_id),
            )
            conn.commit()

        overdue_card = storage.list_active_cards()[0]
        assert overdue_card["box_color"] == "red"

        # 4. 点击卡片打卡复习 -> 复习次数 +1，记录间隔，恢复为蓝色卡片框 ('blue')
        reviewed_card = storage.mark_reviewed(vocab_id)
        assert reviewed_card["review_count"] == 1
        assert reviewed_card["box_color"] == "blue"
        assert reviewed_card["last_interval_sec"] > 7 * 86400

        # 5. 删除卡片（软删除，不删底层存储）
        storage.soft_delete_card(vocab_id)
        assert len(storage.list_active_cards()) == 0
        assert storage.get_by_term("ephemeral") is not None

        # 6. 再次划到被删除的词 -> 重新激活为黄色卡片框 ('yellow' - “又回来啦”)，并累加新句子
        mock_q_inc.return_value = {
            "context_usage": "形容沙漠花朵的花期极短。",
            "sentence_translation": "沙漠中的花朵美丽却短暂。",
        }
        ctx3 = SelectionContext(
            selected_text="ephemeral",
            selection_type="word",
            word_count=1,
            sentence="Desert flowers are beautiful but ephemeral.",
            sentence_word_count=6,
            sentence_over_limit=False,
            sentence_limit=50,
            source_app="word",
        )
        res3 = process_selection(ctx3, storage, cfg)
        assert res3.just_resurrected is True
        active_cards = storage.list_active_cards()
        assert len(active_cards) == 1
        assert active_cards[0]["box_color"] == "yellow"
        assert len(active_cards[0]["sentences"]) == 3

        # 7. 点击黄色卡片打卡复习 -> 恢复蓝框且保留 ever_returned=1
        after_yellow_review = storage.mark_reviewed(vocab_id)
        assert after_yellow_review["box_color"] == "blue"
        assert after_yellow_review["ever_returned"] == 1
        assert after_yellow_review["review_count"] == 2


def test_phrase_judgment_and_sentence_limit(tmp_path: Path):
    db_file = tmp_path / "test_phrase.db"
    storage = VocabStorage(db_path=db_file)
    cfg = {"api_key": "test-key", "base_url": "https://api.example.com/v1", "model": "test"}

    with patch.object(LLMService, "query_phrase") as mock_q_phrase, patch.object(
        LLMService, "translate_text"
    ) as mock_trans:
        # A. 有效短语 (is_phrase=True) 且句子超限 (sentence_over_limit=True)
        mock_q_phrase.return_value = {
            "is_phrase": True,
            "canonical_form": "take into account",
            "phonetic": "/teɪk ˈɪntuː əˈkaʊnt/",
            "meaning": "考虑到；把…计算在内",
            "context_usage": "",
            "sentence_translation": "",
            "direct_translation": "",
        }
        ctx_phrase = SelectionContext(
            selected_text="take into account",
            selection_type="phrase",
            word_count=3,
            sentence="",  # 因超出 50 词上限已被清空
            sentence_word_count=68,
            sentence_over_limit=True,
            sentence_limit=50,
            source_app="word",
        )
        res_p = process_selection(ctx_phrase, storage, cfg)
        assert res_p.mode == "phrase"
        assert res_p.stored is True
        assert res_p.sentence_over_limit is True
        # 验证发送给 AI 的 sentence 为空字符串（不发送超限句子）
        mock_q_phrase.assert_called_once_with("take into account", "")

        # 验证短语入库且无词性 pos
        saved_p = storage.get_by_term("take into account")
        assert saved_p is not None
        assert saved_p["term_type"] == "phrase"
        assert saved_p["meanings"] == [{"pos": "", "meaning": "考虑到；把…计算在内"}]

        # B. 非固定短语 (is_phrase=False) -> 不入库
        mock_q_phrase.reset_mock()
        mock_q_phrase.return_value = {
            "is_phrase": False,
            "canonical_form": "",
            "phonetic": "",
            "meaning": "",
            "context_usage": "",
            "sentence_translation": "这是唯一的办法。",
            "direct_translation": "是唯一的",
        }
        ctx_non_phrase = SelectionContext(
            selected_text="is the only",
            selection_type="phrase",
            word_count=3,
            sentence="This is the only way.",
            sentence_word_count=5,
            sentence_over_limit=False,
            sentence_limit=50,
            source_app="word",
        )
        res_np = process_selection(ctx_non_phrase, storage, cfg)
        assert res_np.mode == "phrase_non_fixed"
        assert res_np.stored is False
        assert storage.get_by_term("is the only") is None

        # C. 句子/段落接口 -> 直接翻译，不入库
        mock_trans.return_value = "人工智能正在深刻改变软件工程的未来。"
        ctx_sent = SelectionContext(
            selected_text="Artificial intelligence is profoundly transforming the future of software engineering.",
            selection_type="sentence",
            word_count=10,
            sentence="",
            sentence_word_count=10,
            sentence_over_limit=False,
            sentence_limit=50,
            source_app="word",
        )
        res_s = process_selection(ctx_sent, storage, cfg)
        assert res_s.mode == "sentence"
        assert res_s.stored is False
        assert res_s.direct_translation == "人工智能正在深刻改变软件工程的未来。"


def test_ui_components_smoke(tmp_path: Path):
    import sys
    from PySide6.QtCore import QPoint
    from PySide6.QtWidgets import QApplication
    from ui_floating import FloatingTIcon, TranslationPopup
    from ui_review import ReviewMainWindow

    app = QApplication.instance() or QApplication(sys.argv)
    db_file = tmp_path / "test_ui.db"
    storage = VocabStorage(db_path=db_file)

    # 插入蓝框、红框、黄框三条测试数据
    c1 = storage.save_new_vocab(
        term="serendipity",
        term_type="word",
        phonetic="/ˌser.ənˈdɪp.ə.ti/",
        meanings=[{"pos": "n.", "meaning": "意外发现珍奇事物的本领；机缘巧合"}],
        sentence="Finding that rare book was pure serendipity.",
        sentence_translation="找到那本珍本纯属机缘巧合。",
        usage_note="作不可数名词，表示意外的好运。",
    )
    c2 = storage.save_new_vocab(
        term="by leaps and bounds",
        term_type="phrase",
        phonetic="/baɪ liːps ænd baʊndz/",
        meanings=[{"pos": "", "meaning": "突飞猛进；极其迅速地"}],
        sentence="Her English has improved by leaps and bounds.",
        sentence_translation="她的英语突飞猛进。",
        usage_note="作状语修饰动词 improved。",
    )
    # 将 c2 设为 9 天前（验证红色卡片框）
    nine_days_ago = (datetime.now(timezone.utc) - timedelta(days=9)).isoformat()
    with storage._connect() as conn:
        conn.execute(
            "UPDATE vocab SET last_reviewed_at = ? WHERE id = ?",
            (nine_days_ago, c2["id"]),
        )
        conn.commit()

    # 将 c1 软删除再增量更新（验证黄色卡片框 "又回来啦"）
    storage.soft_delete_card(c1["id"])
    storage.incremental_update(
        term="serendipity",
        sentence="It was serendipity that we met at the airport.",
        sentence_translation="我们在机场相遇真是巧合。",
        usage_note="it was serendipity that... 句型。",
    )

    win = ReviewMainWindow(storage=storage)
    assert len(win._card_widgets) == 2
    assert win._card_widgets[c1["id"]].data["box_color"] == "yellow"
    assert win._card_widgets[c2["id"]].data["box_color"] == "red"

    # 展开例句
    card_w1 = win._card_widgets[c1["id"]]
    card_w1._toggle_sentences()
    assert card_w1._expanded is True

    # 模拟点击黄框卡片打卡复习 -> 变回蓝框
    win._on_card_reviewed(c1["id"])
    assert win._card_widgets[c1["id"]].data["box_color"] == "blue"
    assert win._card_widgets[c1["id"]].data["review_count"] == 1

    # 测试悬浮 T 图标与翻译弹窗渲染
    t_icon = FloatingTIcon()
    t_icon.show_near(QPoint(200, 200), 0.78)
    t_icon.hide()

    popup = TranslationPopup()
    ctx = SelectionContext(
        selected_text="serendipity",
        selection_type="word",
        word_count=1,
        sentence="",
        sentence_word_count=65,
        sentence_over_limit=True,
        sentence_limit=50,
        source_app="word",
    )
    popup.show_loading(QPoint(220, 220), ctx)
    assert not popup.warning_lbl.isHidden()
    popup.hide()


def test_updater_version_comparison():
    from updater import is_newer_version

    assert is_newer_version("1.0.1", "1.0.0") is True
    assert is_newer_version("v1.2.0", "1.1.9") is True
    assert is_newer_version("1.0.0", "1.0.0") is False
    assert is_newer_version("0.9.9", "1.0.0") is False


