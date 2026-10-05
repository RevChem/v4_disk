"""Вопрос по фактам заданий: фильтр применён снаружи, модели уходит только срез.

Экономия токенов
----------------
* пустой срез или пустой вопрос — модель не вызывается вовсе;
* вместо JSON — TSV с вынесенными константными колонками (см. task_facts.format_for_llm);
* итоги по ВСЕМ строкам даются отдельными строками, поэтому усечение таблицы не портит суммы;
* данные идут в начале сообщения, вопрос — в конце: системный промпт + данные образуют
  одинаковый префикс для разных вопросов по одному срезу (кэш префикса у DeepSeek
  тарифицируется дешевле — usage.cache_hit_tokens показывает, сработал ли он);
* thinking по умолчанию выключен (как в analyst), max_tokens ответа ограничен
  (PROVERKA_FACTS_MAX_OUTPUT_TOKENS, по умолчанию 1800).
"""
from __future__ import annotations

import os
from typing import Any, Sequence

from proverka import task_facts as tf

SYSTEM_PROMPT = (
    "Ты помощник агронома. Отвечай ТОЛЬКО по таблице фактов (строка = задание × поле за смену, "
    "ha — площадь за смену, га). Не выдумывай числа и задания. Если таблица усечена, суммы бери из "
    "строк «Итого» и «По …» — они посчитаны по всем строкам. Если данных недостаточно — так и скажи. "
    "Отвечай кратко, по-русски; task_id называй, когда спрашивают про конкретные задания."
)

_MAX_QUESTION_CHARS = int(os.environ.get("PROVERKA_FACTS_MAX_QUESTION_CHARS", "3000"))


def _runtime() -> dict[str, str]:
    """model / base_url / api_key: настройки из UI поверх .env — как в ai/analyst.py."""
    from proverka.ai.analyst import _api_key

    try:
        from app.adapters.outbound.proverka.ai_settings import load_ai_runtime_settings

        rt = load_ai_runtime_settings()
        model = rt.get("model") or "deepseek-flash"
        base_url = rt.get("base_url") or "https://api.deepseek.com"
    except Exception:  # noqa: BLE001
        model = os.environ.get("PROVERKA_AI_MODEL", "deepseek-flash")
        base_url = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    return {"model": model, "base_url": base_url, "api_key": _api_key()}


def _llm_available() -> tuple[bool, str]:
    from proverka.ai.analyst import ai_enabled

    if not ai_enabled():
        return False, "AI отключён (PROVERKA_AI_ENABLED=0)"
    if not _runtime()["api_key"]:
        return False, "DEEPSEEK_API_KEY не задан"
    try:
        import langchain_openai  # noqa: F401
    except ImportError:
        return False, "langchain_openai не установлен"
    return True, ""


def _extra_body() -> dict[str, Any] | None:
    thinking = os.environ.get("PROVERKA_AI_THINKING", "disabled").strip().lower()
    if thinking in ("0", "false", "no", "off", "disabled"):
        return {"thinking": {"type": "disabled"}}
    return None


def _usage(msg: Any) -> dict[str, Any]:
    um = getattr(msg, "usage_metadata", None) or {}
    tu = (getattr(msg, "response_metadata", None) or {}).get("token_usage") or {}
    return {
        "input_tokens": um.get("input_tokens", tu.get("prompt_tokens")),
        "output_tokens": um.get("output_tokens", tu.get("completion_tokens")),
        "cache_hit_tokens": tu.get("prompt_cache_hit_tokens"),
    }


def _call_llm(system: str, user: str, **_kw: Any) -> tuple[str, dict[str, Any]]:
    from langchain_core.messages import HumanMessage, SystemMessage
    from langchain_openai import ChatOpenAI

    rt = _runtime()
    extra = _extra_body()
    llm = ChatOpenAI(
        model=rt["model"],
        api_key=rt["api_key"],
        base_url=rt["base_url"],
        temperature=0,
        timeout=int(os.environ.get("PROVERKA_AI_TIMEOUT_SEC", "120")),
        max_tokens=int(os.environ.get("PROVERKA_FACTS_MAX_OUTPUT_TOKENS", "1800")),
        **({"extra_body": extra} if extra else {}),
    )
    msg = llm.invoke([SystemMessage(content=system), HumanMessage(content=user)])
    content = msg.content
    if isinstance(content, list):
        content = "".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in content)
    return str(content).strip(), _usage(msg)


def ask_facts(
    question: str,
    rows: Sequence[dict[str, Any]],
    *,
    columns: Sequence[str] | None = None,
    max_rows: int | None = None,
    use_llm: bool = True,
) -> dict[str, Any]:
    """Ответ по срезу фактов. rows уже отфильтрованы (tf.filter_facts)."""
    question = (question or "").strip()[:_MAX_QUESTION_CHARS]
    table, meta = tf.format_for_llm(rows, columns=columns, max_rows=max_rows)
    out: dict[str, Any] = {
        "ok": True,
        "llm_used": False,
        "question": question,
        "answer": "",
        "usage": None,
        "model": None,
        "error": None,
        **meta,
    }
    if not meta["rows_total"]:
        out["answer"] = "По заданному фильтру данных нет."
        return out
    if not question or not use_llm:
        out["answer"] = table
        return out

    ok, why = _llm_available()
    if not ok:
        out["answer"] = table
        out["error"] = why
        return out

    user = f"Данные (TSV):\n{table}\n\nВопрос: {question}"
    try:
        text, usage = _call_llm(SYSTEM_PROMPT, user)
    except Exception as exc:  # noqa: BLE001
        out["answer"] = table
        out["error"] = f"{type(exc).__name__}: {exc}"[:300]
        return out
    out.update(
        llm_used=True,
        answer=text,
        usage=usage,
        model=_runtime()["model"],
        est_input_tokens=tf.estimate_tokens(SYSTEM_PROMPT) + tf.estimate_tokens(user),
    )
    return out


def answer_tsv(question: str, table: str, *, system: str) -> dict[str, Any]:
    """Ответ по уже собранной TSV-таблице. Пустая таблица и пустой вопрос модель не вызывают."""
    question = (question or "").strip()[:_MAX_QUESTION_CHARS]
    table = table or ""
    out: dict[str, Any] = {
        "ok": True,
        "llm_used": False,
        "question": question,
        "answer": table,
        "usage": None,
        "model": None,
        "error": None,
    }
    if not table.strip() or not question:
        if not table.strip():
            out["answer"] = "По заданному фильтру данных нет."
        return out
    ok, why = _llm_available()
    if not ok:
        out["error"] = why
        return out
    user = f"Данные (TSV):\n{table}\n\nВопрос: {question}"
    try:
        text, usage = _call_llm(system, user)
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"{type(exc).__name__}: {exc}"[:300]
        return out
    out.update(llm_used=True, answer=text, usage=usage, model=_runtime()["model"])
    return out
