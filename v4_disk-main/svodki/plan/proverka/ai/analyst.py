"""LangChain + DeepSeek: structured AI report from digest."""
from __future__ import annotations

import json
import os
from typing import Any, Literal

from pydantic import BaseModel, Field

from proverka.ai.domain_rules import llm_prompt_context


class AiRelatedTask(BaseModel):
    task_id: int | None = None
    machine_id: int | None = None
    gosnumber: str = ""
    task_url: str = ""
    area_ha: float | None = None


class AiFinding(BaseModel):
    severity: Literal["critical", "warning", "info"]
    category: str
    title: str
    summary: str
    related_tasks: list[AiRelatedTask] = Field(default_factory=list)
    suggested_action: str = ""


class AiProverkaReport(BaseModel):
    report_date: str
    violation_count: int
    executive_summary: str
    findings: list[AiFinding] = Field(default_factory=list)


def ai_enabled() -> bool:
    return os.environ.get("PROVERKA_AI_ENABLED", "1").strip().lower() not in (
        "0", "false", "no", "off",
    )


def _api_key() -> str:
    try:
        from app.adapters.outbound.proverka.ai_settings import load_ai_runtime_settings

        key = load_ai_runtime_settings().get("api_key") or ""
        if key:
            return key.strip()
    except Exception:
        pass
    return (
        os.environ.get("DEEPSEEK_API_KEY")
        or os.environ.get("OPENAI_API_KEY")
        or ""
    ).strip()


def _task_to_related(t: dict[str, Any]) -> AiRelatedTask:
    return AiRelatedTask(
        task_id=t.get("task_id"),
        machine_id=t.get("machine_id"),
        gosnumber=str(t.get("gosnumber") or ""),
        task_url=str(t.get("task_url") or ""),
        area_ha=t.get("svodka_ha") if t.get("svodka_ha") is not None else t.get("area_ha"),
    )


def _report_id_from_title(title: str) -> str | None:
    tl = title.lower()
    if "хк крупно" in tl:
        return "1"
    if tl.startswith("хк ·") or tl.startswith("хк ·"):
        return "2"
    return None


def _tasks_for_cropwise_issue(
    issue: dict[str, Any],
    task_errors: list[dict[str, Any]],
) -> list[AiRelatedTask]:
    rid = str(issue.get("report_id") or "")
    operation = str(issue.get("operation") or "")
    title = str(issue.get("title") or "")
    title_rid = _report_id_from_title(title)
    keywords: list[str] = []
    if operation:
        keywords.append(operation)
    if "5.4" in operation or "5.4" in title:
        keywords.extend(["5.4", "овощ", "картофел"])

    matched: list[AiRelatedTask] = []
    seen: set[int] = set()
    for t in task_errors:
        tid = t.get("task_id")
        if tid is None:
            continue
        tid_i = int(tid)
        if tid_i in seen:
            continue
        t_rid = str(t.get("report_id") or "")
        work = str(t.get("work") or "")
        if rid and t_rid and t_rid != rid:
            continue
        if title_rid and t_rid and t_rid != title_rid:
            continue
        if keywords and not any(k.lower() in work.lower() for k in keywords if k):
            if operation and operation not in work:
                continue
        matched.append(_task_to_related(t))
        seen.add(tid_i)
    return matched


def _attach_tasks_to_findings(
    findings: list[AiFinding],
    digest: dict[str, Any],
) -> list[AiFinding]:
    task_errors = digest.get("task_errors_top") or []
    issues = {str(i.get("title") or ""): i for i in (digest.get("cropwise_issues") or [])}
    out: list[AiFinding] = []
    for f in findings:
        if f.related_tasks:
            out.append(f)
            continue
        issue = issues.get(f.title)
        if issue:
            tasks = _tasks_for_cropwise_issue(issue, task_errors)
        else:
            pseudo = {"report_id": _report_id_from_title(f.title), "title": f.title, "operation": ""}
            if "5.4" in f.title:
                pseudo["operation"] = "5.4 Уборка картофеля, лука"
            tasks = _tasks_for_cropwise_issue(pseudo, task_errors)
        if tasks:
            f = f.model_copy(update={"related_tasks": tasks})
        out.append(f)
    return out


def _finalize_report(report: AiProverkaReport, digest: dict[str, Any]) -> AiProverkaReport:
    report.findings = _attach_tasks_to_findings(report.findings, digest)
    report.violation_count = len(report.findings)
    return report


def _fallback_report(digest: dict[str, Any]) -> AiProverkaReport:
    """Отчёт только из rule_findings и digest без LLM."""
    findings: list[AiFinding] = []
    for rf in digest.get("rule_findings") or []:
        tasks = [
            AiRelatedTask(
                task_id=t.get("task_id"),
                machine_id=t.get("machine_id"),
                gosnumber=str(t.get("gosnumber") or ""),
                task_url=str(t.get("task_url") or ""),
                area_ha=t.get("area_ha"),
            )
            for t in (rf.get("related_tasks") or [])
        ]
        findings.append(AiFinding(
            severity=rf.get("severity") or "warning",
            category="domain_rule",
            title=str(rf.get("title") or rf.get("rule_id") or "Правило"),
            summary=str(rf.get("summary") or ""),
            related_tasks=tasks,
            suggested_action=str(rf.get("suggested_action") or ""),
        ))

    for cc in digest.get("cross_check_diffs") or []:
        findings.append(AiFinding(
            severity="warning",
            category="cross_check",
            title=f"Сводки расходятся: {cc.get('operation')}",
            summary=f"totals={cc.get('totals')}, max Δ={cc.get('max_diff_ha')} га",
            suggested_action="Сверьте season-фильтры и эталонную тематическую сводку.",
        ))

    task_errors = digest.get("task_errors_top") or []
    for ci in digest.get("cropwise_issues") or []:
        related = _tasks_for_cropwise_issue(ci, task_errors)
        action = "Проверьте work.json и seasonValue в Cropwise UI."
        if related:
            action += f" Заданий с расхождением: {len(related)}."
        findings.append(AiFinding(
            severity="warning" if ci.get("status") != "UI degraded" else "critical",
            category="cropwise",
            title=f"{ci.get('title')} ({ci.get('status')})",
            summary=(
                f"svodka={ci.get('svodka_ha')} га, cropwise={ci.get('cropwise_ha')} га, "
                f"Δ={ci.get('diff_ha')} га"
            ),
            related_tasks=related,
            suggested_action=action,
        ))

    vc = len(findings)
    if vc:
        summary = f"Обнаружено {vc} проблем(ы) (rule-based fallback, LLM не использовался)."
    else:
        summary = "Критичных расхождений в digest не найдено."
    return AiProverkaReport(
        report_date=str(digest.get("report_date") or ""),
        violation_count=vc,
        executive_summary=summary,
        findings=findings[: int(os.environ.get("PROVERKA_AI_MAX_FINDINGS", "45"))],
    )


def _json_output_hint() -> str:
    """DeepSeek json_object mode: в промпте обязательно слово json + пример."""
    return (
        "Ответь одним JSON-объектом (без markdown), формат:\n"
        '{"report_date":"YYYY-MM-DD","violation_count":N,'
        '"executive_summary":"...",'
        '"findings":[{"severity":"critical|warning|info","category":"...",'
        '"title":"...","summary":"...","suggested_action":"...",'
        '"related_tasks":[{"task_id":123,"gosnumber":"...","task_url":"...","area_ha":1.2}]}]}'
    )


def _parse_llm_json(raw: str) -> AiProverkaReport:
    text = (raw or "").strip()
    if not text:
        raise ValueError("LLM вернул пустой ответ")
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    data = json.loads(text)
    return AiProverkaReport.model_validate(data)


def _invoke_deepseek_json(
    digest: dict[str, Any],
    *,
    system: str,
    model: str,
    base_url: str,
    extra_body: dict[str, Any] | None,
) -> AiProverkaReport:
    from langchain_core.messages import HumanMessage, SystemMessage
    from langchain_openai import ChatOpenAI

    llm = ChatOpenAI(
        model=model,
        api_key=_api_key(),
        base_url=base_url,
        temperature=0.1,
        timeout=int(os.environ.get("PROVERKA_AI_TIMEOUT_SEC", "120")),
        max_tokens=int(os.environ.get("PROVERKA_AI_MAX_OUTPUT_TOKENS", "6144")),
        model_kwargs={"response_format": {"type": "json_object"}},
        **({"extra_body": extra_body} if extra_body else {}),
    )

    from proverka.ai.digest import digest_char_limit

    user_content = json.dumps(digest, ensure_ascii=False, indent=2)
    char_limit = digest_char_limit()
    if len(user_content) > char_limit:
        user_content = user_content[:char_limit] + "\n…(truncated)"

    response = llm.invoke([
        SystemMessage(content=system + "\n\n" + _json_output_hint()),
        HumanMessage(content=f"Digest JSON:\n{user_content}"),
    ])
    content = response.content
    if isinstance(content, list):
        content = "".join(
            block.get("text", "") if isinstance(block, dict) else str(block)
            for block in content
        )
    return _parse_llm_json(str(content))


def _report_to_markdown(
    report: AiProverkaReport,
    *,
    digest: dict[str, Any] | None = None,
) -> str:
    lines = [
        f"# AI-отчёт proverka · {report.report_date}",
        "",
        report.executive_summary,
        "",
    ]
    hint = (digest or {}).get("per_report_markdown_hint") or ""
    if hint and hint not in (report.executive_summary or ""):
        lines.append("## По сводкам")
        lines.append("")
        lines.append(hint)
        lines.append("")
    if not report.findings:
        lines.append("_Нарушений не выявлено._")
        return "\n".join(lines)
    for i, f in enumerate(report.findings, 1):
        lines.append(f"## {i}. [{f.severity.upper()}] {f.title}")
        lines.append("")
        lines.append(f.summary)
        if f.suggested_action:
            lines.append("")
            lines.append(f"**Действие:** {f.suggested_action}")
        if f.related_tasks:
            lines.append("")
            lines.append("**Задания:**")
            for t in f.related_tasks[:10]:
                url = t.task_url or ""
                gos = t.gosnumber or "?"
                tid = t.task_id or "?"
                area = f"{t.area_ha:.2f}" if t.area_ha is not None else "?"
                if url:
                    lines.append(f"- #{tid} {gos} · {area} га · {url}")
                else:
                    lines.append(f"- #{tid} {gos} · {area} га")
        lines.append("")
    return "\n".join(lines)


def analyze_digest(digest: dict[str, Any]) -> dict[str, Any]:
    """
    Вызывает DeepSeek или fallback. Возвращает dict для ai_report.json / status.json.
    """
    prompt_context = llm_prompt_context()

    if not ai_enabled() or not _api_key():
        report = _finalize_report(_fallback_report(digest), digest)
        return {
            "ok": True,
            "llm_used": False,
            "report": report.model_dump(),
            "markdown": _report_to_markdown(report, digest=digest),
        }

    try:
        import langchain_openai  # noqa: F401
    except ImportError:
        report = _finalize_report(_fallback_report(digest), digest)
        return {
            "ok": True,
            "llm_used": False,
            "error": "langchain_openai not installed",
            "report": report.model_dump(),
            "markdown": _report_to_markdown(report, digest=digest),
        }

    # deepseek-flash — json_object (не json_schema: DeepSeek его не поддерживает)
    try:
        from app.adapters.outbound.proverka.ai_settings import load_ai_runtime_settings

        runtime = load_ai_runtime_settings()
        model = runtime.get("model") or "deepseek-flash"
        base_url = runtime.get("base_url") or "https://api.deepseek.com"
    except Exception:
        model = os.environ.get("PROVERKA_AI_MODEL", "deepseek-flash")
        base_url = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    thinking = os.environ.get("PROVERKA_AI_THINKING", "disabled").strip().lower()
    extra_body: dict[str, Any] | None = None
    if thinking in ("0", "false", "no", "off", "disabled"):
        extra_body = {"thinking": {"type": "disabled"}}

    system = (
        "Ты ассистент агронома. Анализируешь digest сверки сводок с Cropwise и между сводками. "
        "Используй ТОЛЬКО факты из JSON (cross_check_*, sheet_check_diffs, report_view_by_report, "
        "violations, cropwise_issues, rule_findings). Не выдумывай числа. "
        "violation_count = len(findings). "
        "executive_summary — 2–4 предложения на русском, затем блоки по каждой затронутой сводке: "
        "«### N сводка.» и нумерованный список проблем (как в per_report_markdown_hint). "
        "Для расхождений по культурам между peer-сводками формулируй: "
        "«Расхождение в культуре «…» с 1 сводкой на X га и на Y га со 2 сводкой». "
        "Применяй промпты про сроки посева и уборки культур из system: если культура посеяна "
        "или убрана вне допустимого окна — finding severity=critical с явной датой отчёта. "
        "Не включай findings для blank/UI error при svodka=0 и cropwise=0 (нет работ за смену). "
        "Для cropwise-нарушений с diff>0 указывай related_tasks из task_errors_top (task_id, url)."
    )
    if prompt_context:
        system += "\n\n" + prompt_context

    try:
        report = _invoke_deepseek_json(
            digest,
            system=system,
            model=model,
            base_url=base_url,
            extra_body=extra_body,
        )
        if not report.report_date:
            report.report_date = str(digest.get("report_date") or "")
        report = _finalize_report(report, digest)
        return {
            "ok": True,
            "llm_used": True,
            "model": model,
            "report": report.model_dump(),
            "markdown": _report_to_markdown(report, digest=digest),
        }
    except Exception as exc:
        err = str(exc)
        if "401" in err or "Unauthorized" in err:
            err = (
                "DeepSeek API: 401 Unauthorized — неверный DEEPSEEK_API_KEY "
                f"(проверьте .env, без кавычек и без двойного sk-). Детали: {exc}"
            )
        elif "response_format" in err or "json_schema" in err:
            err = (
                "DeepSeek API: unsupported response_format (нужен json_object). "
                f"Детали: {exc}"
            )
        report = _finalize_report(_fallback_report(digest), digest)
        if err:
            report.executive_summary = f"{report.executive_summary} Ошибка LLM: {err}"
        return {
            "ok": False,
            "llm_used": False,
            "error": err,
            "report": report.model_dump(),
            "markdown": _report_to_markdown(report, digest=digest),
        }


def run_ai_analysis(batch: dict[str, Any]) -> dict[str, Any]:
    """Полный pipeline: digest + rules + LLM."""
    from proverka.ai.digest import build_digest
    from proverka.ai.domain_rules import run_domain_rules

    digest = build_digest(batch)
    digest["rule_findings"] = run_domain_rules(batch)
    result = analyze_digest(digest)
    result["digest"] = digest
    report = result.get("report") or {}
    result["violation_count"] = int(report.get("violation_count") or len(report.get("findings") or []))
    return result
