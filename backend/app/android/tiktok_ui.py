"""Conservative hierarchy inspection. Never infer account/LIVE from generic text."""
from dataclasses import dataclass
import re
import xml.etree.ElementTree as ET
from app.android.models import ScreenState


@dataclass(frozen=True)
class ScreenProfile:
    # Backend-only, device/version specific selectors reviewed on a real device.
    package: str
    version: str
    input_id: str
    send_id: str
    account_id: str
    live_id: str
    verified: bool = False


def foreground(output):
    """Read only resumed records, preferring the current top marker over history."""
    records = {}
    for line in output.splitlines():
        marker = re.fullmatch(r"[ \t]*(topResumedActivity|mResumedActivity|ResumedActivity)(?=[ \t=:]|$)(.*)", line)
        if marker is None: continue
        record = re.fullmatch(r"[ \t]*[=:][ \t]*ActivityRecord\{([^{}\r\n]*)\}[ \t]*", marker[2])
        components = re.findall(r"(?:^|[ \t])([A-Za-z0-9._]+)/([A-Za-z0-9._$]+)(?=[ \t]|$)", record[1]) if record else []
        component = components[0] if len(components) == 1 and len(components[0][1]) <= 200 else None
        records.setdefault(marker[1], set()).add(component)
    for name in ("topResumedActivity", "mResumedActivity", "ResumedActivity"):
        if name in records:
            candidates = records[name]
            # Conflicting/malformed authoritative records cannot fall back to history.
            if None in candidates or len(candidates) != 1: return None, None
            # Preserve Android relative activity spelling, including the leading dot.
            return next(iter(candidates))
    return None, None


def inspect_hierarchy(xml, package, target, profile=None):
    result = dict(screen=ScreenState.UNKNOWN_SCREEN, reason="unknown_screen", comment_input_found=False,
        send_button_found=False, security_state=False, account_verified=False, live_verified=False, profile_verified=False)
    if len(xml.encode("utf-8")) > 524288 or re.search(r"<!\s*(?:DOCTYPE|ENTITY)", xml, re.I): return result
    start = xml.find("<?xml")
    if start >= 0: xml = xml[start:]
    try: root = ET.fromstring(xml)
    except (ET.ParseError, ValueError, RecursionError): return result
    if root.tag != "hierarchy": return result
    stack, nodes = [(root, 0)], []
    while stack:
        node, depth = stack.pop()
        if depth > 32 or len(nodes) >= 2000: return result
        nodes.append(node); stack.extend((child, depth + 1) for child in node)
    security = {
        ScreenState.CAPTCHA: ("captcha", "drag the puzzle", "complete the puzzle"),
        ScreenState.VERIFY: ("verify your identity", "verification required", "подтвердите личность"),
        ScreenState.LOGIN_REQUIRED: ("log in", "sign in", "войти в аккаунт"),
        ScreenState.SECURITY_CHALLENGE: ("security check", "unusual activity", "проверка безопасности"),
    }
    for node in nodes:
        values = " ".join(node.get(key, "") for key in ("text", "content-desc", "resource-id")).casefold()
        for state, markers in security.items():
            if any(marker in values for marker in markers):
                return result | dict(screen=state, reason="security_ui", security_state=True)
    app_nodes = [n for n in nodes if n.get("package") == package]
    if profile is None or not profile.verified or profile.package != package:
        return result | dict(reason="profile_unverified")
    by_id = {}
    for node in app_nodes:
        identity = node.get("resource-id")
        if identity:
            if identity in by_id: return result
            by_id[identity] = node
    if any(n.get("package") not in {None, "", package} for n in nodes): return result
    if any(n.get("class") == "android.app.Dialog" for n in nodes): return result
    result["profile_verified"] = True
    account = by_id.get(profile.account_id)
    result["account_verified"] = account is not None and account.get("text", "").removeprefix("@") == target
    if not result["account_verified"]: return result | dict(reason="account_unverified")
    live = by_id.get(profile.live_id)
    result["live_verified"] = live is not None and live.get("text") == "LIVE" and live.get("enabled") == "true"
    if not result["live_verified"]: return result | dict(reason="live_unverified")
    entry, send = by_id.get(profile.input_id), by_id.get(profile.send_id)
    result["comment_input_found"] = entry is not None and entry.get("class") == "android.widget.EditText" and entry.get("enabled") == "true"
    result["send_button_found"] = send is not None and send.get("enabled") == "true" and send.get("clickable") == "true"
    ready = result["comment_input_found"] and result["send_button_found"]
    return result | dict(screen=ScreenState.COMMENT_INPUT_READY if ready else ScreenState.LIVE_READY,
                         reason="ready" if ready else "input_unavailable")


def exact_input(xml, profile, text):
    """Read back the unique reviewed input; never expose hierarchy text in a DTO."""
    try:
        root = ET.fromstring(xml)
        nodes = [n for n in root.iter() if n.get("resource-id") == profile.input_id
                 and n.get("package") == profile.package]
        return len(nodes) == 1 and nodes[0].get("text") == text
    except (ET.ParseError, ValueError, RecursionError):
        return False
