from __future__ import annotations

import json
import logging
import time
from typing import Any, Optional

from bs4 import BeautifulSoup
from openai import OpenAI

from .config import Config
from .models import Event, GmailMessage, YogaDecision

logger = logging.getLogger(__name__)


_JSON_SCHEMA: dict[str, Any] = {
    "name": "yoga_decision",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "is_yoga": {"type": "boolean"},
            "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
            "reason": {"type": "string"},
            "matched_signals": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["is_yoga", "confidence", "reason", "matched_signals"],
    },
}
_TOOL_SCHEMA: dict[str, Any] = _JSON_SCHEMA["schema"]


def _html_to_text(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    return soup.get_text("\n")


def _truncate(text: str, max_chars: int) -> str:
    if max_chars <= 0:
        return ""
    return text[:max_chars]


def _extract_body_excerpt(msg: GmailMessage, max_chars: int) -> str:
    if msg.text_plain:
        return _truncate(msg.text_plain, max_chars)
    if msg.text_html:
        return _truncate(_html_to_text(msg.text_html), max_chars)
    return ""


def _build_input_text(config: Config, msg: GmailMessage, event: Event) -> str:
    excerpt = _extract_body_excerpt(msg, config.yoga_classifier_max_chars)
    lines = [
        f"provider: {event.provider}",
        f"msg.subject: {msg.subject or ''}",
        f"msg.snippet: {msg.snippet or ''}",
        f"event.title: {event.title or ''}",
        f"event.location_name: {event.location_name or ''}",
        f"event.source_url: {event.source_url or ''}",
        "msg.body_excerpt:",
        excerpt,
    ]
    return "\n".join(lines).strip()


def _build_keyword_text(config: Config, msg: GmailMessage, event: Event) -> str:
    excerpt = _extract_body_excerpt(msg, config.yoga_classifier_max_chars)
    parts = [
        event.provider,
        msg.subject or "",
        msg.snippet or "",
        event.title or "",
        event.location_name or "",
        event.source_url or "",
        excerpt,
    ]
    return "\n".join(parts)


def _get_response_text(response: Any) -> Optional[str]:
    text = getattr(response, "output_text", None)
    if text:
        return text
    output = getattr(response, "output", None) or []
    for item in output:
        content = getattr(item, "content", None) or []
        for part in content:
            part_text = getattr(part, "text", None)
            if part_text:
                return part_text
    return None


def _call_responses_json_schema(
    client: OpenAI, model: str, system_text: str, user_text: str
) -> YogaDecision:
    response = client.responses.create(
        model=model,
        input=[
            {"role": "system", "content": [{"type": "text", "text": system_text}]},
            {"role": "user", "content": [{"type": "text", "text": user_text}]},
        ],
        response_format={"type": "json_schema", "json_schema": _JSON_SCHEMA},
    )
    text = _get_response_text(response)
    if not text:
        raise ValueError("empty response text")
    data = json.loads(text)
    return YogaDecision.model_validate(data)


def _call_chat_completions_tools(
    client: OpenAI, model: str, system_text: str, user_text: str
) -> YogaDecision:
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_text},
            {"role": "user", "content": user_text},
        ],
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "yoga_decision",
                    "description": "Classify if an event is yoga-related and return a strict decision object.",
                    "parameters": _TOOL_SCHEMA,
                },
            }
        ],
        tool_choice={"type": "function", "function": {"name": "yoga_decision"}},
    )
    message = response.choices[0].message
    tool_calls = getattr(message, "tool_calls", None) or []
    if not tool_calls:
        raise ValueError("missing tool_calls in chat completion response")
    args = tool_calls[0].function.arguments
    data = json.loads(args)
    return YogaDecision.model_validate(data)


def _call_chat_completions_json_object(
    client: OpenAI, model: str, system_text: str, user_text: str
) -> YogaDecision:
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_text},
            {"role": "user", "content": user_text},
        ],
        response_format={"type": "json_object"},
    )
    content = response.choices[0].message.content
    if not content:
        raise ValueError("empty chat completion content")
    data = json.loads(content)
    return YogaDecision.model_validate(data)


def _decision_from_error(config: Config, reason: str) -> YogaDecision:
    if config.yoga_classifier_fail_open:
        return YogaDecision(
            is_yoga=True,
            confidence=0.0,
            reason=reason,
            matched_signals=["classifier_error", "fail_open"],
        )
    return YogaDecision(
        is_yoga=False,
        confidence=0.0,
        reason=reason,
        matched_signals=["classifier_error"],
    )


def _parse_terms(value: str) -> list[str]:
    return [t.strip().lower() for t in value.split(",") if t.strip()]


def _parse_error_keywords(value: str) -> list[str]:
    return [t.strip().lower() for t in value.split(",") if t.strip()]


def _parse_provider_list(value: str) -> set[str]:
    return {p.strip().lower() for p in (value or "").split(",") if p.strip()}


def _is_target_provider(config: Config, provider: str) -> bool:
    """
    envで判定対象providerを絞る。
    例:
      YOGA_CLASSIFIER_TARGET_PROVIDERS=peatix,mosh

    未設定/空の場合は「全provider対象」（後方互換）。
    """
    raw = getattr(config, "yoga_classifier_target_providers", "") or ""
    targets = _parse_provider_list(raw)
    if not targets:
        return True
    return (provider or "").strip().lower() in targets


def _is_api_unavailable_error(config: Config, exc: Exception) -> bool:
    msg = str(exc).lower()
    for kw in _parse_error_keywords(config.yoga_classifier_keyword_fallback_on_errors):
        if kw in msg:
            return True
    status = getattr(exc, "status_code", None)
    if status == 429:
        return True
    return False


def _keyword_fallback_decision(
    config: Config, msg: GmailMessage, event: Event, reason: str
) -> YogaDecision:
    terms = _parse_terms(config.yoga_classifier_keyword_fallback_terms)
    text = _build_keyword_text(config, msg, event).lower()
    matched = [t for t in terms if t and t in text]
    if matched:
        return YogaDecision(
            is_yoga=True,
            confidence=0.65,
            reason=reason,
            matched_signals=[f"keyword:{m}" for m in matched],
        )
    return YogaDecision(
        is_yoga=False,
        confidence=0.55,
        reason=reason,
        matched_signals=["keyword:no_match"],
    )


def classify_yoga_event(config: Config, msg: GmailMessage, event: Event) -> YogaDecision:
    # --- DEBUG LOG: target provider 判定の見える化 ---
    raw_targets = getattr(config, "yoga_classifier_target_providers", "") or ""
    is_target = _is_target_provider(config, event.provider)
    logger.info(
        "yoga_classifier: target_providers_raw=%r provider=%s is_target=%s",
        raw_targets,
        event.provider,
        is_target,
    )

    # --- NEW: provider単位で判定対象を制御 ---
    if not is_target:
        decision = YogaDecision(
            is_yoga=True,
            confidence=1.0,
            reason=f"provider '{event.provider}' not targeted; treated as yoga",
            matched_signals=["provider_not_targeted"],
        )
        logger.info("yoga_classifier: route=provider_bypass provider=%s", event.provider)
        return decision
    # --- NEW END ---

    # AI無効時は「全部ヨガ扱い」ではなくキーワード判定へ
    if not config.yoga_classifier_enabled:
        if config.yoga_classifier_keyword_fallback_enabled:
            decision = _keyword_fallback_decision(
                config,
                msg,
                event,
                "classifier disabled; keyword fallback",
            )
            logger.info("yoga_classifier: route=keyword_fallback (disabled)")
            return decision

        decision = _decision_from_error(config, "classifier disabled (keyword fallback disabled)")
        logger.info(
            "yoga_classifier: route=fail_open" if decision.is_yoga else "yoga_classifier: route=fail_closed"
        )
        return decision

    if not config.openai_api_key:
        if config.yoga_classifier_keyword_fallback_enabled:
            decision = _keyword_fallback_decision(
                config,
                msg,
                event,
                "missing OPENAI_API_KEY; keyword fallback",
            )
            logger.info("yoga_classifier: route=keyword_fallback (missing_key)")
            return decision
        return _decision_from_error(config, "missing OPENAI_API_KEY")

    client = OpenAI(api_key=config.openai_api_key)
    input_text = _build_input_text(config, msg, event)

    system_text = (
        "You are a strict classifier that decides whether an event is yoga-related. "
        "Use only the provided fields. Respond with JSON only, matching the schema."
    )

    user_text = (
        "Classify if this is a yoga-related event. "
        "Return is_yoga=true only when clearly related to yoga practice, yoga classes, "
        "asana, meditation within yoga context, or yoga studios/events. "
        "If it's unrelated (design school, general talks, business, etc.), return false.\n\n"
        f"{input_text}"
    )

    last_err: Optional[Exception] = None
    for attempt in range(2):
        try:
            try:
                decision = _call_responses_json_schema(
                    client, config.openai_model_yoga_classifier, system_text, user_text
                )
                logger.info("yoga_classifier: route=responses")
                return decision
            except TypeError as exc:
                logger.info("yoga_classifier: responses fallback: %s", exc)
                try:
                    decision = _call_chat_completions_tools(
                        client, config.openai_model_yoga_classifier, system_text, user_text
                    )
                    logger.info("yoga_classifier: route=tools")
                    return decision
                except TypeError as exc2:
                    logger.info("yoga_classifier: chat.tools fallback: %s", exc2)
                    decision = _call_chat_completions_json_object(
                        client, config.openai_model_yoga_classifier, system_text, user_text
                    )
                    logger.info("yoga_classifier: route=json_object")
                    return decision
        except Exception as exc:
            last_err = exc
            if _is_api_unavailable_error(config, exc):
                if config.yoga_classifier_keyword_fallback_enabled:
                    decision = _keyword_fallback_decision(
                        config,
                        msg,
                        event,
                        "keyword fallback due to API unavailable",
                    )
                    logger.info("yoga_classifier: route=keyword_fallback")
                    return decision
                decision = _decision_from_error(config, "api unavailable (keyword fallback disabled)")
                logger.info(
                    "yoga_classifier: route=fail_open" if decision.is_yoga else "yoga_classifier: route=fail_closed"
                )
                return decision
            if attempt == 0:
                time.sleep(0.5)
                continue
            logger.warning("yoga_classifier: failed after retry: %s", exc)

    reason = f"classifier error: {last_err}" if last_err else "classifier error"
    decision = _decision_from_error(config, reason)
    logger.info("yoga_classifier: route=fail_open" if decision.is_yoga else "yoga_classifier: route=fail_closed")
    return decision
