import re


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def normalize_email(value):
    email = str(value or "").strip().lower()
    return email if EMAIL_RE.match(email) else ""


def get_domain(email):
    email = normalize_email(email)
    return email.split("@", 1)[1] if "@" in email else ""
