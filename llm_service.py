from dataclasses import dataclass, field
import json
import re
from typing import Any, Dict, List

import httpx

from extractor import SelectionContext
from storage import VocabStorage


@dataclass
class TranslationResult:
    mode: str  # 'word' | 'phrase' | 'phrase_non_fixed' | 'sentence' | 'correction' | 'error'
    term: str
    phonetic: str = ""
    meanings: List[Dict[str, str]] = field(default_factory=list)
    sentence: str = ""
    sentence_translation: str = ""
    context_usage: str = ""
    direct_translation: str = ""
    sentence_over_limit: bool = False
    sentence_word_count: int = 0
    sentence_limit: int = 50
    is_incremental: bool = False
    just_resurrected: bool = False
    stored: bool = False
    error_msg: str = ""
    is_appropriate: bool = True
    verdict: str = ""
    correction_analysis: str = ""
    alternatives: List[Dict[str, str]] = field(default_factory=list)


def _parse_json_object(raw: str) -> Dict[str, Any]:
    """从大模型返回文本中稳健提取 JSON 对象。"""
    text = raw.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    else:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            text = text[start : end + 1]
    return json.loads(text)


def fetch_available_models(api_key: str, base_url: str) -> List[str]:
    """从 OpenAI 兼容服务端 (/models) 在线拉取可用模型名称列表。"""
    clean_url = (base_url or "https://api.deepseek.com/v1").strip().rstrip("/")
    url = f"{clean_url}/models"
    headers: Dict[str, str] = {"Accept": "application/json"}
    if api_key and api_key.strip():
        headers["Authorization"] = f"Bearer {api_key.strip()}"

    with httpx.Client(timeout=12.0, follow_redirects=True) as client:
        resp = client.get(url, headers=headers)
        resp.raise_for_status()
        payload = resp.json()

    items = payload.get("data", []) if isinstance(payload, dict) else []
    models: List[str] = []
    for item in items:
        if isinstance(item, dict):
            mid = str(item.get("id") or item.get("name") or "").strip()
            if mid and mid not in models:
                models.append(mid)
        elif isinstance(item, str) and item.strip() not in models:
            models.append(item.strip())

    models.sort()
    return models


class LLMService:
    """封装单词、短语判定、增量更新句子、长句翻译四个大模型接口。"""

    def __init__(self, api_key: str, base_url: str, model: str) -> None:
        self.api_key = (api_key or "").strip()
        self.base_url = (base_url or "https://api.deepseek.com/v1").rstrip("/")
        self.model = (model or "deepseek-chat").strip()

    def _chat(self, system_prompt: str, user_prompt: str, json_mode: bool = True) -> str:
        if not self.api_key:
            raise RuntimeError("尚未配置 API Key，请点击系统托盘或复习界面的【设置】填入 API Key。")

        url = f"{self.base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.2,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        with httpx.Client(timeout=25.0) as client:
            resp = client.post(url, headers=headers, json=payload)
            # 若部分兼容接口不支持 response_format，自动降级重试一次
            if resp.status_code == 400 and json_mode:
                payload.pop("response_format", None)
                resp = client.post(url, headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"]

    def query_word(self, word: str, sentence: str = "") -> Dict[str, Any]:
        """
        【单词首次查询接口】
        返回音标、多个常见词性与对应中文意思，以及在当前句子中的用法和句译。
        """
        sys_prompt = (
            "你是一部权威英汉词典兼语境解析助手。请严格输出 JSON 格式，不要输出任何额外解释。\n"
            "JSON 结构要求：\n"
            "{\n"
            '  "phonetic": "/音标/",\n'
            '  "meanings": [\n'
            '    {"pos": "词性缩写(如 n. / vt. / adj.)", "meaning": "该词性下的多个核心中文释义，用分号隔开"}\n'
            "  ],\n"
            '  "context_usage": "该词在所给句子中的具体词义、语法作用或搭配用法（若未提供句子则填 \\"\\"）",\n'
            '  "sentence_translation": "所给句子的自然中文翻译（若未提供句子则填 \\"\\"）"\n'
            "}"
        )
        if sentence:
            user_prompt = f"单词：{word}\n所在句子：{sentence}"
        else:
            user_prompt = f"单词：{word}\n（无上下文句子，仅返回音标与各词性中文释义）"

        raw = self._chat(sys_prompt, user_prompt, json_mode=True)
        parsed = _parse_json_object(raw)
        return {
            "phonetic": str(parsed.get("phonetic") or ""),
            "meanings": list(parsed.get("meanings") or []),
            "context_usage": str(parsed.get("context_usage") or ""),
            "sentence_translation": str(parsed.get("sentence_translation") or ""),
        }

    def query_phrase(self, phrase: str, sentence: str = "") -> Dict[str, Any]:
        """
        【短语接口（2~5词）】
        由 AI 判断是否构成英语短语/固定搭配/习语/词组：
        - 若是短语 (is_phrase=true)：返回音标、意思（不需要词性）、句中用法与句译；
        - 若不是短语 (is_phrase=false)：直接返回该片段的中文翻译（direct_translation）。
        """
        sys_prompt = (
            "你是英语短语与搭配鉴别专家。请判断用户选中的多词片段是否构成有意义的英语短语、"
            "固定搭配、动词词组、介词短语或习语（即便含冠词/代词或时态屈折变化，如 took into account 也算短语；"
            "但像 'is the only'、'and he went' 这类随机截断片段则不算短语）。\n"
            "请严格输出 JSON 格式（注意：短语不需要词性 pos）：\n"
            "{\n"
            '  "is_phrase": true 或 false,\n'
            '  "canonical_form": "短语原型（如 take into account）",\n'
            '  "phonetic": "/短语注音/",\n'
            '  "meaning": "短语的中文释义（不需要标注词性）",\n'
            '  "context_usage": "该短语在当前句子中的用法解析（若未提供句子或非短语则填 \\"\\"）",\n'
            '  "sentence_translation": "所在句子的中文译文（若未提供句子则填 \\"\\"）",\n'
            '  "direct_translation": "若 is_phrase 为 false，直接给出所选片段的中文翻译"\n'
            "}"
        )
        if sentence:
            user_prompt = f"选中文本：{phrase}\n所在句子：{sentence}"
        else:
            user_prompt = f"选中文本：{phrase}"

        raw = self._chat(sys_prompt, user_prompt, json_mode=True)
        parsed = _parse_json_object(raw)
        return {
            "is_phrase": bool(parsed.get("is_phrase", False)),
            "canonical_form": str(parsed.get("canonical_form") or phrase),
            "phonetic": str(parsed.get("phonetic") or ""),
            "meaning": str(parsed.get("meaning") or ""),
            "context_usage": str(parsed.get("context_usage") or ""),
            "sentence_translation": str(parsed.get("sentence_translation") or ""),
            "direct_translation": str(parsed.get("direct_translation") or parsed.get("meaning") or ""),
        }

    def query_incremental_sentence(self, term: str, sentence: str) -> Dict[str, str]:
        """
        【重复划词增量更新接口】
        只发送目标词/短语与新句子，仅获取句子翻译和该词在句中的用法，不重复请求字典音标和基础词义。
        """
        sys_prompt = (
            "你是英语语境解析助手。用户此前已学过该词/短语，现在在新句子中再次遇到它。\n"
            "请只针对当前句子严格输出 JSON 格式：\n"
            "{\n"
            '  "context_usage": "该词/短语在这句话中的具体含义与用法简析",\n'
            '  "sentence_translation": "整句话的自然流畅中文翻译"\n'
            "}"
        )
        user_prompt = f"目标词/短语：{term}\n当前句子：{sentence}"
        raw = self._chat(sys_prompt, user_prompt, json_mode=True)
        parsed = _parse_json_object(raw)
        return {
            "context_usage": str(parsed.get("context_usage") or ""),
            "sentence_translation": str(parsed.get("sentence_translation") or ""),
        }

    def translate_text(self, text: str) -> str:
        """
        【句子与段落直接翻译接口】
        直接返回流畅、准确的中文译文，不进行结构化入库。
        """
        sys_prompt = "你是专业英汉翻译专家。请直接将用户提供的英文句子或段落翻译为自然、准确、地道的中文，不要输出多余的解释。"
        return self._chat(sys_prompt, text, json_mode=False).strip()

    def query_correction(self, term: str, sentence: str = "") -> Dict[str, Any]:
        """
        【纠错模式接口】
        结合所选词所在句子，判断在此处使用是否合适（语义、搭配、语法形式、语体色彩），
        并且不论合适与否，均给出可在该句中替换使用的可替代词。不存入单词本。
        """
        sys_prompt = (
            "你是资深英语写作纠错与选词润色专家。用户会划选一个词或短语（并提供其所在的完整句子），"
            "请你判断所选词在此句子中使用是否合适（包括词义准确性、固定搭配、词性与语法屈折、语体正式度等），"
            "并且**不论在此使用合适与否，都必须给出 3~5 个适用于该句语境的可替代词或表达**。\n"
            "请严格输出 JSON 格式，不要输出任何额外解释：\n"
            "{\n"
            '  "is_appropriate": true 或 false,\n'
            '  "verdict": "简短结论（如：使用合适 / 搭配不当 / 词义偏差 / 语法形式有误 / 语体欠佳）",\n'
            '  "analysis": "详细说明该词在句中使用是否得当及具体原因；若不合适请说明问题所在与修改建议",\n'
            '  "alternatives": [\n'
            '    {"word": "可替代词或短语（保持适配原句的词形）", "meaning": "中文释义及替换后的语境说明或优势"}\n'
            "  ],\n"
            '  "sentence_translation": "所在句子的自然中文翻译（若未提供句子则填 \\"\\"）"\n'
            "}"
        )
        if sentence:
            user_prompt = f"所选词：{term}\n所在句子：{sentence}"
        else:
            user_prompt = f"所选词：{term}\n（未提取到外部句子，请判断该词/表达本身用法是否恰当，并给出同义可替代词）"

        raw = self._chat(sys_prompt, user_prompt, json_mode=True)
        parsed = _parse_json_object(raw)

        raw_alts = parsed.get("alternatives") or []
        norm_alts: List[Dict[str, str]] = []
        if isinstance(raw_alts, list):
            for item in raw_alts:
                if isinstance(item, dict):
                    w = str(
                        item.get("word")
                        or item.get("term")
                        or item.get("replacement")
                        or ""
                    ).strip()
                    m = str(
                        item.get("meaning")
                        or item.get("note")
                        or item.get("reason")
                        or ""
                    ).strip()
                    if w:
                        norm_alts.append({"word": w, "meaning": m})
                elif isinstance(item, str) and item.strip():
                    norm_alts.append({"word": item.strip(), "meaning": ""})

        is_app = bool(parsed.get("is_appropriate", True))
        verdict = str(parsed.get("verdict") or ("使用合适" if is_app else "建议替换"))
        return {
            "is_appropriate": is_app,
            "verdict": verdict,
            "analysis": str(parsed.get("analysis") or ""),
            "alternatives": norm_alts,
            "sentence_translation": str(parsed.get("sentence_translation") or ""),
        }


def process_correction(
    ctx: SelectionContext,
    cfg: Dict[str, Any],
) -> TranslationResult:
    """
    纠错模式编排器：
    自动结合所选词与所在句子，判断在此处使用是否合适，不论合适与否都给出可替代词。
    在该功能中绝不存单词到单词本 (stored=False)。
    """
    llm = LLMService(
        api_key=cfg.get("api_key", ""),
        base_url=cfg.get("base_url", ""),
        model=cfg.get("model", ""),
    )
    try:
        sentence = ctx.sentence or (
            ctx.selected_text if ctx.selection_type == "sentence" else ""
        )
        res = llm.query_correction(ctx.selected_text, sentence)
        return TranslationResult(
            mode="correction",
            term=ctx.selected_text,
            sentence=sentence,
            sentence_translation=res["sentence_translation"],
            is_appropriate=res["is_appropriate"],
            verdict=res["verdict"],
            correction_analysis=res["analysis"],
            alternatives=res["alternatives"],
            stored=False,
        )
    except Exception as exc:
        return TranslationResult(
            mode="error",
            term=ctx.selected_text,
            error_msg=str(exc),
            stored=False,
        )


def process_selection(
    ctx: SelectionContext,
    storage: VocabStorage,
    cfg: Dict[str, Any],
    mode: str = "translate",
) -> TranslationResult:
    """
    核心翻译与存储编排器：
    根据取词结果（单词 / 短语 / 句子）及本地数据库状态（新词 / 重复划词增量更新 / 软删除回归），
    自动调用对应 AI 接口并完成结构化存储。若 mode=='correction' 则执行纠错模式且不存单词本。
    """
    if mode == "correction":
        return process_correction(ctx, cfg)

    llm = LLMService(
        api_key=cfg.get("api_key", ""),
        base_url=cfg.get("base_url", ""),
        model=cfg.get("model", ""),
    )

    try:
        # 1. 句子或段落（>= 6 个词）：直接翻译，不储存
        if ctx.selection_type == "sentence":
            trans = llm.translate_text(ctx.selected_text)
            return TranslationResult(
                mode="sentence",
                term=ctx.selected_text,
                direct_translation=trans,
                stored=False,
            )

        # 2. 检查本地是否已存在该单词或短语（包含曾被软删除的记录）
        existing = storage.get_by_term(ctx.selected_text)
        if existing is not None:
            # 【重复划词 -> 增量更新模式】
            sent_trans = ""
            usage_note = ""

            if ctx.sentence:
                # 若这句话已经在历史记录里，直接复用；否则只发送句子调增量接口
                norm_s = re.sub(r"\s+", " ", ctx.sentence.strip())
                matched_hist = next(
                    (s for s in existing.get("sentences", []) if s["sentence"] == norm_s),
                    None,
                )
                if matched_hist and (matched_hist.get("usage_note") or matched_hist.get("translation")):
                    sent_trans = matched_hist.get("translation", "")
                    usage_note = matched_hist.get("usage_note", "")
                else:
                    inc = llm.query_incremental_sentence(existing["term"], ctx.sentence)
                    sent_trans = inc["sentence_translation"]
                    usage_note = inc["context_usage"]

            updated = storage.incremental_update(
                term=existing["term"],
                sentence=ctx.sentence,
                sentence_translation=sent_trans,
                usage_note=usage_note,
            )
            return TranslationResult(
                mode=updated["term_type"],
                term=updated["term"],
                phonetic=updated.get("phonetic", ""),
                meanings=updated.get("meanings", []),
                sentence=ctx.sentence,
                sentence_translation=sent_trans,
                context_usage=usage_note,
                sentence_over_limit=ctx.sentence_over_limit,
                sentence_word_count=ctx.sentence_word_count,
                sentence_limit=ctx.sentence_limit,
                is_incremental=True,
                just_resurrected=bool(updated.get("just_resurrected", False)),
                stored=True,
            )

        # 3. 首次划到的单词（1 个词）
        if ctx.selection_type == "word":
            res = llm.query_word(ctx.selected_text, ctx.sentence)
            saved = storage.save_new_vocab(
                term=ctx.selected_text,
                term_type="word",
                phonetic=res["phonetic"],
                meanings=res["meanings"],
                sentence=ctx.sentence,
                sentence_translation=res["sentence_translation"],
                usage_note=res["context_usage"],
            )
            return TranslationResult(
                mode="word",
                term=saved["term"],
                phonetic=saved.get("phonetic", ""),
                meanings=saved.get("meanings", []),
                sentence=ctx.sentence,
                sentence_translation=res["sentence_translation"],
                context_usage=res["context_usage"],
                sentence_over_limit=ctx.sentence_over_limit,
                sentence_word_count=ctx.sentence_word_count,
                sentence_limit=ctx.sentence_limit,
                is_incremental=False,
                stored=True,
            )

        # 4. 首次划到的多词片段（2 ~ 5 个词）：走短语接口，由 AI 判断是否构成短语
        res = llm.query_phrase(ctx.selected_text, ctx.sentence)
        if res["is_phrase"]:
            # 构成短语：返回音标、意思（不需要词性 pos），结构化存入数据库
            phrase_meanings = [{"pos": "", "meaning": res["meaning"]}]
            saved = storage.save_new_vocab(
                term=ctx.selected_text,
                term_type="phrase",
                phonetic=res["phonetic"],
                meanings=phrase_meanings,
                sentence=ctx.sentence,
                sentence_translation=res["sentence_translation"],
                usage_note=res["context_usage"],
            )
            return TranslationResult(
                mode="phrase",
                term=saved["term"],
                phonetic=saved.get("phonetic", ""),
                meanings=phrase_meanings,
                sentence=ctx.sentence,
                sentence_translation=res["sentence_translation"],
                context_usage=res["context_usage"],
                sentence_over_limit=ctx.sentence_over_limit,
                sentence_word_count=ctx.sentence_word_count,
                sentence_limit=ctx.sentence_limit,
                is_incremental=False,
                stored=True,
            )
        else:
            # AI 判定不构成固定短语：直接返回翻译，不进行结构化储存
            return TranslationResult(
                mode="phrase_non_fixed",
                term=ctx.selected_text,
                direct_translation=res["direct_translation"] or res["sentence_translation"],
                sentence=ctx.sentence,
                sentence_translation=res["sentence_translation"],
                sentence_over_limit=ctx.sentence_over_limit,
                sentence_word_count=ctx.sentence_word_count,
                sentence_limit=ctx.sentence_limit,
                stored=False,
            )

    except Exception as exc:
        return TranslationResult(
            mode="error",
            term=ctx.selected_text,
            error_msg=str(exc),
        )
