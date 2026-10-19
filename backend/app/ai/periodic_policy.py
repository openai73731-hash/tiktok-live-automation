"""Conservative output rules for periodic host statements, never viewer replies."""
import re

# Exact neutral templates approved by the operator for this release. They have
# no punctuation for IME compatibility; this does not admit arbitrary first-
# person claims, questions or testimonials from generated drafts.
RELEASE_TEMPLATES = ("Welcome to the live stream", "We are sharing useful ideas today",
    "Thanks for spending time with us")

def periodic_statement(text):
    if text in RELEASE_TEMPLATES: return True
    if not isinstance(text, str) or not re.fullmatch(r"[A-Za-z0-9 .,!?':;()\-]{1,240}", text) or text != text.strip(): return False
    if len(text) > 240 or not text.endswith('.') or '?' in text or '!' in text: return False
    # Require one simple declarative sentence; do not use sentence normalization
    # or model assurances to turn a question/call-to-action into approved content.
    if re.search(r'[.;]', text[:-1]): return False
    if re.match(r'(?i)^(?:who|what|when|where|why|how|is|are|am|was|were|do|does|did|can|could|would|will|should|have|has|had|may|might)\b', text): return False
    if re.search(r"(?i)\b(?:feel free|let us know|tell us|ask (?:us|me)|join (?:us|the conversation)|share your|what do you|would you|do you)\b", text): return False
    if re.search(r"(?i)\b(?:I|I've|I'm|we|we've|our viewers|viewers say|everyone here|the host|right now|just happened|today's live)\b", text): return False
    return bool(re.search('[A-Za-z]', text))
