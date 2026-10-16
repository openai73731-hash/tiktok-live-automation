"""Readable, server-random account challenges; no UI or delivery authority."""
import secrets

PREFIX = "Hello everyone, have "
ADJECTIVES = tuple("calm kind nice good warm soft neat bright clear fair fresh light quiet clean safe glad merry sweet peaceful cheerful friendly lovely happy sunny gentle relaxed restful joyful pleasant hopeful cosy serene".split())
NOUNS = tuple("day morning evening weekend moment smile thought idea break walk hour minute night week season chat story song view visit trip memory surprise start ending meeting greeting party holiday pause dream outing".split())


def new_challenge_text():
    # Four distinct adjective/noun pairs provide >39 CSPRNG bits.
    # Exact SDK author, receipt, scope and <=60s
    # expiry remain mandatory; the phrase is never a reusable credential.
    adjectives, nouns, wishes = list(ADJECTIVES), list(NOUNS), []
    for _ in range(4):
        adjective, noun = secrets.choice(adjectives), secrets.choice(nouns)
        adjectives.remove(adjective)
        nouns.remove(noun)
        wishes.append(f"a {adjective} {noun}")
    return PREFIX + ", ".join(wishes[:3]) + " and " + wishes[3] + "."


def is_challenge_text(text):
    # Keep old technical challenges quarantined as well, including after expiry,
    # restart, or a failed observer. Neither namespace may enter AI/comment logs.
    return text.casefold().startswith(PREFIX.casefold()) or text.upper().startswith("MOB-")
