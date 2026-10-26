import hashlib
import secrets
from datetime import datetime, timezone


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def mask_phone(p: str) -> str:
    return "***" + p[-4:] if p else ""


def new_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def money(d) -> str:
    return f"${d:,.2f}"


def hash_token(t: str) -> str:
    return hashlib.sha256(t.encode()).hexdigest()


def short(text: str, n: int) -> str:
    """Trim to n characters at a word boundary."""
    text = " ".join(text.split())
    return text if len(text) <= n else text[:n].rsplit(" ", 1)[0].rstrip(",;-") or text[:n]


def english(name: str) -> str:
    """Bilingual flyer names read "FRENCH | ENGLISH" or "ENGLISH | FRENCH": keep the English side."""
    if "|" not in name:
        return name
    parts = [p.strip() for p in name.split("|") if p.strip()]
    return min(parts, key=_frenchness) if parts else name


FRENCH = {"de", "du", "des", "ou", "et", "au", "aux", "le", "la", "les", "avec", "sans", "beurre", "poulet", "pommes",
          "pates", "pâtes", "jus", "lait", "oeufs", "œufs", "fromage", "pain", "papier", "hygiénique", "dentifrice",
          "bananes", "oranges", "boeuf", "porc", "légumes", "fruits", "biologique", "alimentaires", "pilons", "cuisses"}


def _frenchness(text: str) -> int:
    t = text.lower()
    return sum(ch in "àâçéèêëîïôûùüÿœ" for ch in t) + 2 * sum(w.strip(",.") in FRENCH for w in t.split())
