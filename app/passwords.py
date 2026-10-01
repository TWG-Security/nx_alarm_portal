"""Password rules. Every place a password is set uses check() (ported from packages/core/src/password-policy.ts)."""

from dataclasses import dataclass

SYMBOLS = set("!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~ ")


@dataclass(frozen=True)
class Policy:
    min_length: int = 12
    upper: bool = True
    lower: bool = True
    number: bool = True
    symbol: bool = True


def policy() -> Policy:
    from app import platform_settings
    return getattr(platform_settings.current(), "password_policy", None) or Policy()


def hint(p: Policy | None = None) -> str:
    p = p or policy()
    parts = [k for k, on in (("an uppercase letter", p.upper), ("a lowercase letter", p.lower),
                             ("a number", p.number), ("a symbol", p.symbol)) if on]
    return f"At least {p.min_length} characters" + (f", with {', '.join(parts[:-1])}{' and ' if len(parts) > 1 else ''}{parts[-1]}" if parts else "") + "."


def check(password: str, p: Policy | None = None) -> str | None:
    """None if the password is acceptable, else a sentence saying what's missing."""
    p = p or policy()
    if len(password) > 200:
        return "Passwords can be at most 200 characters."
    missing = []
    if len(password) < p.min_length:
        missing.append(f"at least {p.min_length} characters (this has {len(password)})")
    if p.upper and not any(c.isupper() for c in password):
        missing.append("an uppercase letter")
    if p.lower and not any(c.islower() for c in password):
        missing.append("a lowercase letter")
    if p.number and not any(c.isdigit() for c in password):
        missing.append("a number")
    if p.symbol and not any(c in SYMBOLS or not c.isalnum() for c in password):
        missing.append("a symbol")
    if not missing:
        return None
    return "The password needs " + (", ".join(missing[:-1]) + " and " + missing[-1] if len(missing) > 1 else missing[0]) + "."


def expired(user) -> bool:
    """Older than the expiry rule (Platform page; 0 = never). No password yet = not expired."""
    from datetime import datetime, timedelta, timezone
    from app import platform_settings
    days = platform_settings.current().pw_expiry_days
    changed = user.password_changed_at
    if days <= 0 or changed is None or not user.password_hash:
        return False
    changed = changed if changed.tzinfo else changed.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - changed > timedelta(days=days)
