"""Structural lookup-response signals. No payloads leave this classifier."""
from html.parser import HTMLParser
import json
import re
from urllib.parse import urlsplit


HARD_REASON_CODES = frozenset({
    "http_401", "http_403", "http_429", "captcha_marker", "verification_marker",
    "security_challenge_marker", "login_interstitial_marker",
    "challenge_interstitial_marker", "access_restriction_marker", "rate_limit_marker",
})
SOFT_REASON_PATTERNS = {
    "generic_login_text": r"log[\s_-]?in",
    "generic_captcha_text": r"captcha",
    "generic_verification_text": r"verif(?:y|ication)",
    "generic_security_text": r"security|challenge|access[\s_-]?denied|unauthorized|forbidden",
    "generic_rate_limit_text": r"rate[\s_-]?limit",
}
CHALLENGE_REASON_CODES = HARD_REASON_CODES | SOFT_REASON_PATTERNS.keys()

# Only active state values count, not feature names, URLs, translations or code
# mentioning these words. LIVE data never overrides these explicit states.
ACTIVE_STATES = {
    "captcha": "captcha_marker", "captcha_required": "captcha_marker",
    "verification_required": "verification_marker", "verify_to_continue": "verification_marker",
    "security_challenge": "security_challenge_marker", "challenge_required": "security_challenge_marker",
    "login_required": "login_interstitial_marker",
    "access_denied": "access_restriction_marker", "forbidden": "access_restriction_marker",
    "unauthorized": "access_restriction_marker", "rate_limit": "rate_limit_marker",
}
ACTIVE_FLAGS = {
    "captcha_required": "captcha_marker", "captchaRequired": "captcha_marker",
    "verification_required": "verification_marker", "verificationRequired": "verification_marker",
    "challenge_required": "security_challenge_marker", "challengeRequired": "security_challenge_marker",
    "security_challenge": "security_challenge_marker",
}


def state_reasons(payload) -> set[str]:
    reasons: set[str] = set()
    pending = [payload]
    while pending:
        state = pending.pop()
        if not isinstance(state, dict):
            continue
        for key, reason in ACTIVE_FLAGS.items():
            value = state.get(key)
            if value is True or (type(value) is int and value == 1):
                reasons.add(reason)
        for key in ("message", "error", "status", "state"):
            value = state.get(key)
            if isinstance(value, str):
                reason = ACTIVE_STATES.get(re.sub(r"[\s-]+", "_", value.strip().lower()))
                if reason:
                    reasons.add(reason)
        # Do not reinterpret viewer text or the LIVE user's profile as auth state.
        pending.extend(state.get(key) for key in (
            "data", "security", "challenge", "verification", "captcha", "auth",
        ))
    return reasons


class SecurityHTMLParser(HTMLParser):
    """Recognize page structure, never execute scripts or follow URLs."""

    def __init__(self, *, has_live_state: bool):
        super().__init__(convert_charrefs=True)
        self.has_live_state = has_live_state
        self.reasons: set[str] = set()
        self.capture: str | None = None
        self.parts: list[str] = []
        self.script_type = ""

    def handle_starttag(self, tag, attrs):
        if self.capture == "script":
            return
        values = dict(attrs)
        tokens = set((values.get("id") or "").lower().split())
        tokens.update((values.get("class") or "").lower().split())
        if tokens & {"captcha", "captcha-container", "captcha_container",
                     "captcha-verify-container", "secsdk-captcha-drag-wrapper"}:
            self.reasons.add("captcha_marker")
        if tokens & {"verify-page", "verification-page", "verification-challenge"}:
            self.reasons.add("verification_marker")
        if tokens & {"challenge-form", "security-challenge"}:
            self.reasons.add("security_challenge_marker")
        if not self.has_live_state:
            if tokens & {"challenge-interstitial", "security-interstitial"}:
                self.reasons.add("challenge_interstitial_marker")
            if tokens & {"login-wall", "login-interstitial"}:
                self.reasons.add("login_interstitial_marker")
            if tag == "form":
                # Ordinary links and modal labels are not a login wall.
                path = urlsplit(values.get("action") or "").path.lower().rstrip("/")
                if path.endswith(("/login", "/log-in", "/signin")):
                    self.reasons.add("login_interstitial_marker")
        if tag in {"title", "h1", "script"} and self.capture is None:
            self.capture = tag
            self.parts = []
            self.script_type = (values.get("type") or "").lower()

    def handle_data(self, data):
        if self.capture:
            self.parts.append(data)

    def handle_endtag(self, tag):
        if tag != self.capture:
            return
        text = "".join(self.parts)
        if tag == "script":
            if self.script_type in {"application/json", "application/ld+json"}:
                try:
                    self.reasons.update(state_reasons(json.loads(text)))
                except ValueError:
                    pass  # The SDK retains responsibility for parsing LIVE state.
            else:
                # An explicit active flag assignment, not a bundle/string reference.
                match = re.fullmatch(r"\s*(?:window\.)?(\w+)\s*=\s*true\s*;?\s*", text)
                if match and match[1] in ACTIVE_FLAGS:
                    self.reasons.add(ACTIVE_FLAGS[match[1]])
        else:
            heading = " ".join(text.lower().split())
            if re.search(r"^(?:captcha|captcha required)$|(?:complete|solve) (?:the )?captcha", heading):
                self.reasons.add("captcha_marker")
            if re.search(r"verify (?:to continue|you are human|your identity)|verification required", heading):
                self.reasons.add("verification_marker")
            if re.search(r"security (?:check|challenge)|^challenge required$", heading):
                self.reasons.add("security_challenge_marker")
            if heading in {"access denied", "forbidden", "unauthorized"}:
                self.reasons.add("access_restriction_marker")
            if not self.has_live_state and heading in {"login", "log in", "sign in", "log in to continue"}:
                self.reasons.add("login_interstitial_marker")
        self.capture = None
        self.parts = []


def classify_security(*, status: int, body: str, payload, is_html: bool,
                      has_live_state: bool) -> tuple[bool, list[str]]:
    reasons = {code for code, pattern in SOFT_REASON_PATTERNS.items()
               if re.search(pattern, body, re.I)}
    if status in {401, 403, 429}:
        reasons.add({401: "http_401", 403: "http_403", 429: "http_429"}[status])
    reasons.update(state_reasons(payload))
    if is_html:
        parser = SecurityHTMLParser(has_live_state=has_live_state)
        parser.feed(body)
        parser.close()
        reasons.update(parser.reasons)
    return bool(reasons & HARD_REASON_CODES), sorted(reasons)
