from openai import OpenAI
import logging
import re

from config import DEFAULT_SYSTEM_PROMPT
from database.db_manager import get_llm_settings
from modules.network_proxy import apply_proxy_settings

try:
    from anthropic import Anthropic
except ImportError:  # pragma: no cover - optional until requirements are installed
    Anthropic = None


logger = logging.getLogger(__name__)


def _active_settings(purpose="cold"):
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


def _llm_complete(user_prompt, max_tokens=120, temperature=0.5, purpose="cold"):
    settings = _active_settings(purpose=purpose)
    if not settings:
        return ""

    provider = settings.get("provider")
    apply_proxy_settings()
    base_provider = provider[5:] if (provider or "").startswith("warm_") else provider
    model = settings.get("model")
    system_prompt = settings.get("system_prompt") or DEFAULT_SYSTEM_PROMPT

    if base_provider == "anthropic":
        if Anthropic is None:
            raise RuntimeError("The anthropic package is not installed.")
        kwargs = {"api_key": settings["api_key"]}
        if settings.get("base_url"):
            kwargs["base_url"] = settings["base_url"].rstrip("/")
        client = Anthropic(**kwargs)
        response = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )
        return _anthropic_text(response)

    client = OpenAI(api_key=settings["api_key"], base_url=settings.get("base_url") or None)
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        max_tokens=max_tokens,
        temperature=temperature,
    )
    return response.choices[0].message.content.strip()


def generate_icebreaker(company_info, position):
    """Generate a concise, human-sounding opening line for outbound email."""
    prompt = (
        "Write one customized, highly professional opening sentence for a B2B "
        "outbound email. Start directly with the observation. Do not include "
        "placeholders. Keep it specific, modest, low-pressure, and based only on "
        "the provided profile and position. Do not use hype, fake familiarity, "
        "unsupported claims, spam-filter language, or algorithm-evasion language.\n\n"
        f"Company profile: {company_info}\n"
        f"Recipient position: {position}"
    )
    try:
        result = _llm_complete(prompt, max_tokens=60, temperature=0.7)
        return result or "I noticed your team's focused work in the industry."
    except Exception:
        return "I stumbled upon your profile and was impressed by your team's trajectory."


def _protect_single_brace_variables(template):
    token_map = {}
    protected = template or ""

    def remember(value):
        token = f"__EPETREL_VAR_{len(token_map)}__"
        token_map[token] = value
        return token

    protected = re.sub(r"https?://[^\s<>'\"]+|www\.[^\s<>'\"]+", lambda match: remember(match.group(0)), protected)
    protected = re.sub(r"\{\{[^{}]+\}\}", lambda match: remember(match.group(0)), protected)
    protected = re.sub(r"\[[^\[\]\r\n]{1,100}\]", lambda match: remember(match.group(0)), protected)

    def replace_single_brace(match):
        inner = match.group(1).strip()
        if "|" in inner:
            return match.group(0)
        return remember("{" + inner + "}")

    protected = re.sub(r"\{([^{}]+)\}", replace_single_brace, protected)
    return protected, token_map


def _strip_response_wrappers(text):
    clean = (text or "").strip()
    clean = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", clean)
    clean = re.sub(r"\s*```$", "", clean).strip()
    clean = re.sub(
        r"^(?:email body|rewritten email|spintax|output|result|variant(?:s)?)\s*:\s*",
        "",
        clean,
        flags=re.IGNORECASE,
    ).strip()
    return clean


def _restore_tokens(text, token_map):
    restored = text or ""
    for token, original in token_map.items():
        restored = restored.replace(token, original)
    return restored.strip()


def _valid_spintax_format(text):
    stack = []
    for char in text or "":
        if char == "{":
            if stack:
                return False
            stack.append(char)
        elif char == "}":
            if not stack:
                return False
            stack.pop()
    if stack:
        return False

    for match in re.finditer(r"\{([^{}]*\|[^{}]*)\}", text or ""):
        options = [item.strip() for item in match.group(1).split("|")]
        if any(not item for item in options):
            return False
    return True


def _valid_template_format(text):
    protected, _ = _protect_single_brace_variables(text)
    return _valid_spintax_format(protected)


def _has_spintax(text):
    return any("|" in match.group(1) for match in re.finditer(r"\{([^{}]+)\}", text or ""))


def _tokens_are_outside_spintax(text, protected_tokens):
    for match in re.finditer(r"\{([^{}]*\|[^{}]*)\}", text or ""):
        if any(token in match.group(0) for token in protected_tokens):
            return False
    return True


def _token_counts_match(text, token_map):
    for token in token_map:
        if (text or "").count(token) != 1:
            return False
    return True


def _normalized_copy(text):
    return re.sub(r"\s+", " ", (text or "")).strip().lower()


def generate_copy_variants(user_template):
    """Generate deliverability-aware Spintax while preserving merge variables."""
    protected, token_map = _protect_single_brace_variables(user_template)
    protected_tokens = set(token_map)
    prompt = (
        "Rewrite the user's outbound email copy as natural, human-sounding Spintax variants and improve compliant inbox deliverability.\n"
        "Return only the complete rewritten body. It must be directly pasteable into the same template field.\n"
        "Use single-brace Spintax groups like {option A|option B|option C}; do not nest Spintax.\n"
        "Include at least 4 meaningful Spintax groups across greetings, transitions, value framing, and call-to-action wording.\n"
        "Preserve every protected token exactly as written, such as __EPETREL_VAR_0__.\n"
        "Protected tokens represent merge variables, bracket placeholders, double-brace placeholders, and URLs. Never rewrite, split, translate, remove, or duplicate them.\n"
        "Generate variants only in fixed copy outside protected tokens. Never place protected tokens inside Spintax.\n"
        "Correct: {Hi|Good day} __EPETREL_VAR_0__. Incorrect: {Hi __EPETREL_VAR_0__|Good day __EPETREL_VAR_0__}.\n"
        "Preserve line breaks, paragraph structure, and any HTML tags from the input.\n"
        "Keep the meaning and all factual claims intact. Do not add unsupported specifics.\n"
        "Improve legitimate Gmail deliverability by making the copy plain, specific, low-pressure, and easy to reply to.\n"
        "Make the voice feel like a transparent one-to-one note from a real sender, not a bulk campaign or AI-written promotion.\n"
        "Keep the body compact, usually 50-125 words including opt-out and signature if those are present.\n"
        "Use short paragraphs and one simple call to action, preferably a permission-based question.\n"
        "De-market the copy: remove or soften aggressive urgency, hard-sell phrasing, discount-first language, excessive punctuation, and emoji.\n"
        "Preserve the commercial intent, but phrase it in neutral, professional, conversational language. Do not disguise a marketing email as a transactional or critical notice.\n"
        "If footer or signature text is present, keep it as a normal professional signature. Do not obfuscate unsubscribe text or hide required compliance language.\n"
        "Avoid spammy wording, pressure, hype, urgency, deceptive framing, markdown, commentary, labels, or explanations.\n"
        "Avoid Re:/Fwd: style framing, fake familiarity, all-caps emphasis, excessive punctuation, emojis, clickbait, "
        "guarantees, discounts, 'free', 'act now', 'limited time', 'risk-free', '100%', 'click here', and similar terms.\n"
        "Avoid adding links, attachments, tracking language, hidden text, or formatting tricks. If the source has many links, "
        "prefer wording that asks whether the recipient would like the link in a reply.\n"
        "Keep any opt-out or not-relevant language calm and easy to understand.\n"
        "Do not mention spam filters, Gmail algorithms, bypassing detection, or evasion techniques in the email body.\n"
        "Create enough variation to reduce repeated copy while keeping every option grammatical, truthful, and customer-centered.\n\n"
        f"User copy:\n{protected}"
    )
    result = _llm_complete(prompt, max_tokens=900, temperature=0.65)
    cleaned = _strip_response_wrappers(result)
    if not cleaned:
        logger.warning("copy variant generation rejected: empty LLM response")
        return ""
    if not _token_counts_match(cleaned, token_map):
        logger.warning(
            "copy variant generation rejected: protected token count mismatch tokens=%s response=%r",
            sorted(token_map),
            cleaned[:500],
        )
        return ""
    if not _tokens_are_outside_spintax(cleaned, protected_tokens):
        logger.warning("copy variant generation rejected: protected token placed inside Spintax")
        return ""
    if not _valid_spintax_format(cleaned) or not _has_spintax(cleaned):
        logger.warning("copy variant generation rejected: invalid/missing protected Spintax response=%r", cleaned[:500])
        return ""
    restored = _restore_tokens(cleaned, token_map)
    if not _valid_template_format(restored):
        logger.warning("copy variant generation rejected: invalid restored template response=%r", restored[:500])
        return ""
    if _normalized_copy(restored) == _normalized_copy(user_template):
        logger.warning("copy variant generation rejected: output same as input")
        return ""
    return restored


def analyze_sentiment(email_content):
    """Classify a sales reply into a small set of intent tags."""
    prompt = (
        "Analyze the sales lead's email reply. Return strictly one of these tags: "
        "[Interested], [Refused], [Follow Up Later].\n\n"
        f"Email reply:\n{email_content}"
    )
    try:
        result = _llm_complete(prompt, max_tokens=10, temperature=0)
        return result if result in ["[Interested]", "[Refused]", "[Follow Up Later]"] else "Pending"
    except Exception:
        return "Pending"
