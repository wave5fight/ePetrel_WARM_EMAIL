import re
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse

from modules.email_engine import html_to_plain_text


RISKY_TERMS = (
    "free!!!",
    "guaranteed",
    "risk-free",
    "act now",
    "limited time",
    "100%",
    "urgent",
    "winner",
)

EXTRA_RISKY_TERMS = (
    "act fast",
    "limited spots",
    "one time offer",
    "no obligation",
    "risk free",
    "guaranteed roi",
    "double your revenue",
    "verify your account",
    "wire transfer",
    "crypto investment",
)

AI_SLOP_PHRASES = (
    "game-changer",
    "revolutionize",
    "unlock your potential",
    "take your business to the next level",
    "cutting-edge",
    "transform your workflow",
    "supercharge",
    "seamless experience",
)

SHORT_LINK_DOMAINS = {
    "bit.ly",
    "cutt.ly",
    "goo.gl",
    "is.gd",
    "lnkd.in",
    "ow.ly",
    "rebrand.ly",
    "t.co",
    "tinyurl.com",
}

COLD_EMAIL_WORD_MIN = 50
COLD_EMAIL_WORD_MAX = 125
RAW_URL_RE = re.compile(r"\b(?:https?://[^\s<>'\"]+|www\.[^\s<>'\"]+)", re.IGNORECASE)


class _HtmlSignalParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []
        self.images = 0
        self.hidden_fragments = 0

    def handle_starttag(self, tag, attrs):
        attrs_dict = {str(key).lower(): str(value or "") for key, value in attrs}
        if tag.lower() == "a":
            href = attrs_dict.get("href", "").strip()
            if href:
                self.links.append(href)
        if tag.lower() == "img":
            self.images += 1
        style = attrs_dict.get("style", "").lower()
        if "display:none" in style.replace(" ", "") or "visibility:hidden" in style.replace(" ", ""):
            self.hidden_fragments += 1


def _dangerous_words_path():
    return Path(__file__).resolve().parents[1] / "Doc" / "dangerousWords.txt"


def load_dangerous_words():
    words = []
    path = _dangerous_words_path()
    if path.exists():
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            word = line.strip()
            if word and not word.startswith("#"):
                words.append(word)
    words.extend(RISKY_TERMS)
    words.extend(EXTRA_RISKY_TERMS)
    unique = {}
    for word in words:
        cleaned = word.strip()
        if cleaned:
            unique.setdefault(cleaned.lower(), cleaned)
    return sorted(unique.values(), key=lambda item: item.lower())


def _find_terms(text, terms, limit=40):
    lowered = (text or "").lower()
    found = []
    for term in terms:
        normalized = term.lower()
        if not normalized:
            continue
        if re.search(r"(?<![a-z0-9])" + re.escape(normalized) + r"(?![a-z0-9])", lowered):
            found.append(term)
        if len(found) >= limit:
            break
    return found


def _html_signals(body_html):
    parser = _HtmlSignalParser()
    try:
        parser.feed(body_html or "")
    except Exception:
        pass
    return parser


def _score_from_findings(findings, base=100):
    score = base
    for item in findings:
        severity = item.get("severity")
        if severity == "error":
            score -= 18
        elif severity == "warning":
            score -= 8
        else:
            score -= 3
    return max(0, min(100, score))


def _level_from_score(score):
    if score >= 85:
        return "success"
    if score >= 65:
        return "warning"
    return "error"


def _make_finding(code, title, detail, severity="warning"):
    return {
        "code": code,
        "title": title,
        "detail": detail,
        "severity": severity,
    }


def count_words(text):
    plain = html_to_plain_text(text or "")
    latin_words = re.findall(r"[A-Za-z0-9]+(?:[’'-][A-Za-z0-9]+)*", plain)
    cjk_chars = re.findall(r"[\u4e00-\u9fff]", plain)
    return len(latin_words) + len(cjk_chars)


def extract_links(body_html, plain_text=""):
    parser = _html_signals(body_html)
    links = list(parser.links)
    seen = set(links)
    for match in RAW_URL_RE.finditer(plain_text or html_to_plain_text(body_html or "")):
        href = match.group(0).rstrip(".,);]")
        if href not in seen:
            seen.add(href)
            links.append(href)
    return parser, links


LINT_MESSAGES = {
    "zh": {
        "subject_long": "主题过长，建议控制在 80 个字符以内。",
        "missing_plain": "缺少有效纯文本版本。",
        "body_short": "当前冷邮件约 {count} 词，偏短；建议正文、退订和签名合计控制在 {min}-{max} 词。",
        "body_long": "当前冷邮件约 {count} 词，偏长；多数冷邮件建议控制在 {min}-{max} 词，便于快速阅读和回复。",
        "missing_opt_out": "正文未出现退订或拒绝说明；系统不会自动写入正文，如需显示请手动填写。",
        "many_links": "链接数量偏多，冷启动阶段建议每封 0-1 个链接，最多不超过 2 个。",
        "image_heavy": "图片占比可能偏高，建议主要用文字表达。",
        "risky_terms": "检测到高风险营销词：{terms}。",
        "raw_urls": "正文出现裸 URL；建议改成自然锚文本，非必要链接可改为“回复后发送”。",
        "generic_greeting": "问候语偏泛化，建议使用真实姓名、公司或具体上下文。",
    },
    "en": {
        "subject_long": "The subject is long; keep it under 80 characters when possible.",
        "missing_plain": "A valid plain-text version is missing.",
        "body_short": "Current cold email is about {count} words, which is short. Aim for {min}-{max} words across body, unsubscribe line, and signature.",
        "body_long": "Current cold email is about {count} words, which is long. Most cold emails work better around {min}-{max} words for quick scanning and replies.",
        "missing_opt_out": "No opt-out or refusal language is visible. The system will not add body copy automatically; add it manually if you want it shown.",
        "many_links": "Too many links detected. During cold start, keep each email to 0-1 links, with 2 as the upper bound.",
        "image_heavy": "The email may be image-heavy. Prefer text-first copy when the body is short.",
        "risky_terms": "Risky marketing terms detected: {terms}.",
        "raw_urls": "Raw URLs are visible in the body. Use natural anchor text, or ask permission to send non-essential links in a reply.",
        "generic_greeting": "The greeting is generic. Use a real name, company, or specific recipient context when possible.",
    },
}


def _lint_text(lang, key, **kwargs):
    messages = LINT_MESSAGES.get(lang) or LINT_MESSAGES["zh"]
    return messages[key].format(**kwargs)


def lint_email(subject, body_html, plain_text=None, lang="zh"):
    warnings = []
    subject = subject or ""
    body_html = body_html or ""
    plain_text = plain_text or html_to_plain_text(body_html)
    combined = f"{subject}\n{plain_text}".lower()

    if len(subject) > 80:
        warnings.append(_lint_text(lang, "subject_long"))
    if not plain_text:
        warnings.append(_lint_text(lang, "missing_plain"))
    word_count = count_words(plain_text)
    if word_count < COLD_EMAIL_WORD_MIN:
        warnings.append(_lint_text(lang, "body_short", count=word_count, min=COLD_EMAIL_WORD_MIN, max=COLD_EMAIL_WORD_MAX))
    elif word_count > COLD_EMAIL_WORD_MAX:
        warnings.append(_lint_text(lang, "body_long", count=word_count, min=COLD_EMAIL_WORD_MIN, max=COLD_EMAIL_WORD_MAX))
    if not re.search(r"\b(unsubscribe|opt out|reply\s+with\s+['\"]?no|reply\s+['\"]?no|remove me|not interested|not relevant|not the right person)\b", combined, re.IGNORECASE):
        warnings.append(_lint_text(lang, "missing_opt_out"))
    _, links = extract_links(body_html, plain_text)
    if len(links) > 2:
        warnings.append(_lint_text(lang, "many_links"))
    if re.search(RAW_URL_RE, plain_text or ""):
        warnings.append(_lint_text(lang, "raw_urls"))
    if re.search(r"^\s*(hi|hello|dear)\s+(there|friend|team)\b", plain_text, re.IGNORECASE):
        warnings.append(_lint_text(lang, "generic_greeting"))
    if "<img" in body_html.lower() and len(plain_text) < 200:
        warnings.append(_lint_text(lang, "image_heavy"))

    found_terms = [term for term in tuple(RISKY_TERMS) + AI_SLOP_PHRASES if term in combined]
    if found_terms:
        warnings.append(_lint_text(lang, "risky_terms", terms=", ".join(found_terms)))

    return warnings


def analyze_email_locally(subject, body_html, plain_text=None, sender_email="", ps_auto_added=True):
    subject = subject or ""
    body_html = body_html or ""
    plain_text = plain_text or html_to_plain_text(body_html)
    combined = f"{subject}\n{plain_text}"
    parser, links = extract_links(body_html, plain_text)
    link_domains = []
    for href in links:
        parsed = urlparse(href if re.match(r"^[a-z][a-z0-9+.-]*://", href, re.IGNORECASE) else f"https://{href}")
        host = (parsed.netloc or "").lower()
        if host:
            link_domains.append(host[4:] if host.startswith("www.") else host)

    dangerous_words = _find_terms(combined, load_dangerous_words())
    content_findings = []
    format_findings = []
    compliance_findings = []

    if len(subject) > 80:
        content_findings.append(_make_finding("subject_too_long", "Subject is long", "Keep the subject under 80 characters when possible."))
    if re.search(r"\b(RE|FWD?)\s*:", subject, re.IGNORECASE):
        content_findings.append(_make_finding("reply_prefix", "Misleading reply prefix", "Avoid Re:/Fwd: unless the message is a real reply or forward.", "error"))
    if subject and subject.upper() == subject and re.search(r"[A-Z]", subject):
        content_findings.append(_make_finding("subject_all_caps", "Subject uses all caps", "All-caps subject lines often look promotional or urgent."))
    if re.search(r"(!{2,}|\?{2,}|\${2,})", subject):
        content_findings.append(_make_finding("subject_punctuation", "Subject has heavy punctuation", "Reduce repeated punctuation and currency symbols."))
    word_count = count_words(plain_text)
    if word_count < COLD_EMAIL_WORD_MIN:
        content_findings.append(_make_finding("body_too_short", "Body is very short", f"Current copy is about {word_count} words. Aim for {COLD_EMAIL_WORD_MIN}-{COLD_EMAIL_WORD_MAX} words for cold outreach."))
    elif word_count > COLD_EMAIL_WORD_MAX:
        content_findings.append(_make_finding("body_too_long", "Body is long", f"Current copy is about {word_count} words. Aim for {COLD_EMAIL_WORD_MIN}-{COLD_EMAIL_WORD_MAX} words so the email is easy to scan."))
    if dangerous_words:
        content_findings.append(
            _make_finding(
                "dangerous_words",
                "Risk words detected",
                ", ".join(dangerous_words[:12]),
                "warning" if len(dangerous_words) < 6 else "error",
            )
        )
    ai_phrases = _find_terms(combined, AI_SLOP_PHRASES)
    if ai_phrases:
        content_findings.append(
            _make_finding(
                "generic_marketing_language",
                "Generic marketing language detected",
                ", ".join(ai_phrases[:8]),
            )
        )
    if re.search(r"^\s*(hi|hello|dear)\s+(there|friend|team)\b", plain_text, re.IGNORECASE):
        content_findings.append(
            _make_finding(
                "generic_greeting",
                "Greeting is generic",
                "Use the recipient's name, company, or a concrete context signal when available.",
            )
        )
    if re.search(r"\b(save|increase|grow|boost|improve)\s+\d+%|\b\d+x\s+(more|faster|growth|revenue)\b", combined, re.IGNORECASE):
        content_findings.append(
            _make_finding(
                "unsupported_metric_claim",
                "Strong metric claim detected",
                "Only use quantified performance claims when the source template provides evidence.",
                "warning",
            )
        )

    html_length = max(1, len(re.sub(r"\s+", "", body_html)))
    text_length = len(re.sub(r"\s+", "", plain_text))
    if body_html and text_length / html_length < 0.25:
        format_findings.append(_make_finding("low_text_ratio", "HTML-to-text ratio is low", "Use more real text and less layout markup."))
    if parser.images and text_length < 200:
        format_findings.append(_make_finding("image_heavy", "Image-heavy message", "Avoid relying on images when text content is short."))
    if parser.hidden_fragments:
        format_findings.append(_make_finding("hidden_content", "Hidden HTML content found", "Hidden content can trigger spam filtering.", "error"))
    if len(links) > 2:
        format_findings.append(_make_finding("many_links", "Many links detected", "Cold outreach is safer with 0-1 links, with 2 as the upper bound."))
    if re.search(RAW_URL_RE, plain_text or ""):
        format_findings.append(_make_finding("raw_urls", "Raw URLs visible", "Use natural anchor text, or ask permission to send non-essential links in a reply."))
    short_links = sorted({domain for domain in link_domains if domain in SHORT_LINK_DOMAINS})
    if short_links:
        format_findings.append(_make_finding("short_links", "Short links detected", ", ".join(short_links), "error"))
    tracking_links = [href for href in links if re.search(r"[?&](utm_|fbclid|gclid|mc_cid|mc_eid)", href, re.IGNORECASE)]
    if tracking_links:
        format_findings.append(_make_finding("tracking_params", "Tracking parameters detected", "UTM and click identifiers can add risk in cold outreach."))

    has_opt_out = bool(re.search(r"\b(unsubscribe|opt out|reply\s+with\s+['\"]?no|reply\s+['\"]?no|remove me|not interested|not relevant|not the right person)\b", combined, re.IGNORECASE))
    if not has_opt_out:
        detail = "The sending engine will not add body opt-out copy automatically. Add it manually only if you want it shown."
        compliance_findings.append(_make_finding("missing_opt_out", "Opt-out language not visible", detail, "warning"))
    elif ps_auto_added:
        compliance_findings.append(_make_finding("auto_ps_present", "Auto refusal P.S. included", "The template analysis includes the automatically appended refusal/opt-out line.", "info"))

    categories = [
        {
            "key": "content",
            "title": "Content",
            "score": _score_from_findings(content_findings),
            "findings": content_findings,
        },
        {
            "key": "format",
            "title": "Format",
            "score": _score_from_findings(format_findings),
            "findings": format_findings,
        },
        {
            "key": "compliance",
            "title": "Compliance",
            "score": _score_from_findings(compliance_findings, base=95),
            "findings": compliance_findings,
        },
    ]
    for category in categories:
        category["level"] = _level_from_score(category["score"])

    overall = round(sum(category["score"] for category in categories) / max(1, len(categories)))
    return {
        "source": "local",
        "score": overall,
        "level": _level_from_score(overall),
        "dangerous_words": dangerous_words,
        "link_domains": sorted(set(link_domains)),
        "sender_email": sender_email,
        "categories": categories,
        "findings": [finding for category in categories for finding in category["findings"]],
    }
