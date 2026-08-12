import re

from openai import OpenAI

from config import DEFAULT_SYSTEM_PROMPT
from database.db_manager import get_llm_settings
from modules.network_proxy import apply_proxy_settings

try:
    from anthropic import Anthropic
except ImportError:  # pragma: no cover - optional provider dependency
    Anthropic = None


def _active_settings(purpose="warm"):
    settings = get_llm_settings("warm_openai") if purpose == "warm" else get_llm_settings(purpose=purpose)
    if not settings or not settings.get("api_key"):
        return None
    settings["system_prompt"] = settings.get("system_prompt") or DEFAULT_SYSTEM_PROMPT
    return settings


def _anthropic_text(response):
    parts = []
    for block in getattr(response, "content", []) or []:
        text = getattr(block, "text", None)
        if text:
            parts.append(text)
    return "\n".join(parts).strip()


def _llm_complete(user_prompt, max_tokens=120, temperature=0.5, purpose="warm"):
    settings = _active_settings(purpose=purpose)
    if not settings:
        return ""

    provider = settings.get("provider")
    base_provider = provider[5:] if (provider or "").startswith("warm_") else provider
    apply_proxy_settings()

    if base_provider == "anthropic":
        if Anthropic is None:
            raise RuntimeError("The anthropic package is not installed.")
        kwargs = {"api_key": settings["api_key"]}
        if settings.get("base_url"):
            kwargs["base_url"] = settings["base_url"].rstrip("/")
        client = Anthropic(**kwargs)
        response = client.messages.create(
            model=settings.get("model"),
            max_tokens=max_tokens,
            temperature=temperature,
            system=settings.get("system_prompt") or DEFAULT_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
        )
        return _anthropic_text(response)

    client = OpenAI(api_key=settings["api_key"], base_url=settings.get("base_url") or None)
    response = client.chat.completions.create(
        model=settings.get("model"),
        messages=[
            {"role": "system", "content": settings.get("system_prompt") or DEFAULT_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        max_tokens=max_tokens,
        temperature=temperature,
    )
    return response.choices[0].message.content.strip()


def _strip_response_wrappers(text):
    clean = (text or "").strip()
    clean = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", clean)
    clean = re.sub(r"\s*```$", "", clean).strip()
    clean = re.sub(
        r"^(?:message|body|output|result)\s*:\s*",
        "",
        clean,
        flags=re.IGNORECASE,
    ).strip()
    return clean
