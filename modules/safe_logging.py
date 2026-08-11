import logging
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path


EMAIL_RE = re.compile(r"\b([A-Z0-9._%+-])([A-Z0-9._%+-]*)(@[A-Z0-9.-]+\.[A-Z]{2,})\b", re.IGNORECASE)
SECRET_RE = re.compile(
    r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key|authorization|refresh[_-]?token|access[_-]?token)"
    r"\b\s*[:=]\s*['\"]?[^'\"\s,;]+"
)


def mask_email(value):
    value = str(value or "")
    match = EMAIL_RE.search(value)
    if not match:
        return value
    first, rest, domain = match.groups()
    masked_local = first + ("***" if rest else "")
    return value[:match.start()] + masked_local + domain + value[match.end():]


def redact_sensitive(value):
    text = str(value or "")
    text = SECRET_RE.sub(lambda m: f"{m.group(1)}=[REDACTED]", text)
    return EMAIL_RE.sub(lambda m: f"{m.group(1)}***{m.group(3)}", text)


def configure_file_logger(name, log_path, level=logging.INFO):
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    Path(log_path).parent.mkdir(parents=True, exist_ok=True)
    logger.setLevel(level)
    handler = RotatingFileHandler(log_path, maxBytes=2_000_000, backupCount=5, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    logger.propagate = False
    return logger
