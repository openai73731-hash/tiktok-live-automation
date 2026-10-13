"""Conservative hierarchy inspection. Never infer account/LIVE from generic text."""
from dataclasses import dataclass
import re
import xml.etree.ElementTree as ET
from app.android.models import ClientVariant, ScreenState


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
    client_variant: ClientVariant = ClientVariant.UNKNOWN
    activities: tuple[str, ...] = ()
    version_code: str | None = None
    serial: str | None = None
    live_markers: tuple[tuple[str, str], ...] = ()


PACKAGE = "com.zhiliaoapp.musically"
def rid(name): return PACKAGE + ":id/" + name

TIKTOK_YOU_4683 = ScreenProfile(PACKAGE, "46.8.3", rid("h4k"), rid("vp8"), rid("sxp"), "",
    True, ClientVariant.TIKTOK_YOU, ("com.windukk.mod.icon.default",), "2024608030", "903ed867",
    ((rid("tv_online_audience_num"), "android.widget.TextView"),
     (rid("oyh"), "androidx.recyclerview.widget.RecyclerView"),
     (rid("user_info_container"), "android.view.ViewGroup"), (rid("ttlive_toolbar_more"), "android.widget.FrameLayout")))


def parse_hierarchy(xml):
    if len(xml.encode("utf-8")) > 524288 or re.search(r"<!\s*(?:DOCTYPE|ENTITY)", xml, re.I): return None
    try: root = ET.fromstring(xml)
    except (ET.ParseError, ValueError, RecursionError): return None
    if root.tag != "hierarchy": return None
    stack, count = [(root, 0)], 0
    while stack:
        n, depth = stack.pop(); count += 1
        if count > 2000 or depth > 32: return None
        stack.extend((c, depth+1) for c in n)
    return root


def unique_node(xml, identity, package=PACKAGE):
    root = parse_hierarchy(xml)
    nodes = [n for n in root.iter() if n.get("resource-id") == identity and n.get("package") == package] if root is not None else []
    return nodes[0] if len(nodes) == 1 else None


def profile_account(xml, expected):
    root = parse_hierarchy(xml)
    if root is None or not expected: return False
    if any(n.get("package") not in {None, "", PACKAGE} or n.get("class") == "android.app.Dialog" for n in root.iter()): return False
    # sxp is the exact handle button observed on Profile; LIVE user_name is never used.
    nodes = [n for n in root.iter() if n.get("resource-id") == rid("sxp") and n.get("text") == "@" + expected
        and n.get("class") == "android.widget.Button" and n.get("enabled") == "true" and n.get("package") == PACKAGE]
    ids = {n.get("resource-id") for n in root.iter()}
    owner = unique_node(xml, rid("sxq"))
    owner_control = owner is not None and owner.get("class") == "android.widget.Button" and owner.get("enabled") == "true" and owner.get("text") == "Добавить описание"
    return len(nodes) == 1 and owner_control and {rid("syx"), rid("sxx"), rid("viewpager")} <= ids


def composer_shape(root):
    ids = {n.get("resource-id") for n in root.iter() if n.get("resource-id")}
    allowed = {"android:id/content", "android:id/navigationBarBackground", *(rid(n) for n in
        ("f0t", "l_8", "view_shadow", "l_6", "lbz", "yo8", "lcm", "h4k", "h4t", "qde", "qcv", "vp8", "vpl", "l_7"))}
    required = {rid(n) for n in ("l_8", "view_shadow", "l_6", "lbz", "yo8", "lcm", "h4k", "vp8", "vpl")}
    if not required <= ids or not ids <= allowed: return False
    # The reviewed composer is a separate window, not the host hierarchy behind it.
    # Accept exactly its two inert anonymous wrappers; unknown overlays fail closed.
    anonymous = [n for n in root.iter("node") if not n.get("resource-id")]
    if len(anonymous) != 2 or any(n.get("class") != "android.widget.FrameLayout" or
            n.get("clickable") != "false" or n.get("text") or n.get("content-desc") for n in anonymous): return False
    if any(sum(n.get("resource-id") == identity for n in root.iter()) != 1 for identity in ids): return False
    if rid("h4t") in ids:
        entry = next(n for n in root.iter() if n.get("resource-id") == rid("h4k"))
        placeholder = next(n for n in root.iter() if n.get("resource-id") == rid("h4t"))
        container = next(n for n in root.iter() if n.get("resource-id") == rid("lcm"))
        # Observed empty-composer variant: a noninteractive sibling of the exact
        # focused input. Localized placeholder wording grants no authority.
        if (entry not in list(container) or placeholder not in list(container) or
                entry.get("package") != PACKAGE or entry.get("class") != "android.widget.EditText" or
                any(entry.get(k) != "true" for k in ("enabled", "clickable", "focusable", "focused")) or
                entry.get("password") == "true" or placeholder.get("package") != PACKAGE or
                placeholder.get("class") != "android.widget.TextView" or placeholder.get("enabled") != "true" or
                any(placeholder.get(k) != "false" for k in ("clickable", "focusable", "focused"))): return False
    def chain(names):
        nodes = [root]
        for name in names:
            nodes = [c for n in nodes for c in (n.iter() if n is root else n) if c.get("resource-id") == rid(name)]
            if len(nodes) != 1: return False
        return True
    return chain(("l_8", "l_6", "lbz", "yo8", "lcm", "h4k")) and chain(("l_8", "l_6", "lbz", "vp8", "vpl"))


def foreground(output):
    """Resolve current app evidence; an inert AOD window grants no authority."""
    records = {}
    lines = output.splitlines()
    for index, line in enumerate(lines):
        marker = re.fullmatch(r"[ \t]*(topResumedActivity|mResumedActivity|ResumedActivity|mFocusedApp|mCurrentFocus)(?=[ \t=:]|$)(.*)", line)
        if marker is None: continue
        value = marker[2]
        if re.fullmatch(r"[ \t]*[=:][ \t]*", value) and index + 1 < len(lines):
            value += lines[index + 1].strip()
        # MIUI can put the numeric task outside the component's closing brace.
        # Accept only that observed suffix, never arbitrary trailing history/text.
        kind = "Window" if marker[1] == "mCurrentFocus" else "ActivityRecord"
        record = re.fullmatch(r"[ \t]*[=:][ \t]*" + kind + r"\{([^{}\r\n]*)\}(?:[ \t]+t[0-9]{1,10}\})?[ \t]*", value)
        components = re.findall(r"(?:^|[ \t])([A-Za-z0-9._]+)/([A-Za-z0-9._$]+)(?=[ \t]|$)", record[1]) if record else []
        bare = re.fullmatch(r"[ \t]*[=:][ \t]*([A-Za-z0-9._]+)/([A-Za-z0-9._$]+)[ \t]*", value)
        if bare: components = [(bare[1], bare[2])]
        component = components[0] if len(components) == 1 and len(components[0][1]) <= 200 else None
        records.setdefault(marker[1], []).append(component)
    selected = None
    for name in ("topResumedActivity", "mResumedActivity", "ResumedActivity", "mFocusedApp"):
        if name in records:
            candidates = records[name]
            # Conflicting/malformed authoritative records cannot fall back to history.
            if None in candidates or len({c[0] for c in candidates}) != 1: return None, None
            selected = candidates[0]
            break
    if selected is None:
        candidates = records.get("mCurrentFocus", [])
        return candidates[0] if candidates and None not in candidates and len({c[0] for c in candidates}) == 1 else (None, None)
    focused = records.get("mFocusedApp", set())
    # Activity/alias namespaces need not match their application package. Two
    # activities of the same package are not conflicting foreground apps.
    if None in focused or any(c[0] != selected[0] for c in focused): return None, None
    # Componentless AOD/missing window focus must not override reliable app
    # evidence. A concrete conflicting window component still blocks readiness.
    if any(c is not None and c[0] != selected[0] for c in records.get("mCurrentFocus", ())):
        return None, None
    # Preserve Android relative activity spelling, including the leading dot.
    return selected


def observed_variant(package, activity):
    # A display hint, never proof of authenticity, account, LIVE or delivery safety.
    if package == "com.zhiliaoapp.musically" and activity == "com.windukk.mod.icon.default":
        return ClientVariant.TIKTOK_YOU
    return ClientVariant.UNKNOWN


def package_version(output):
    versions = set(re.findall(r"\bversionName=([^\s]+)", output))
    codes = set(re.findall(r"\bversionCode=([^\s]+)", output))
    version = next(iter(versions)) if len(versions) == 1 else None
    code = next(iter(codes)) if len(codes) == 1 else None
    if version is None or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,63}", version): version = None
    if code is None or not re.fullmatch(r"[0-9]{1,12}", code): code = None
    return version, code


def inspect_hierarchy(xml, package, target, profile=None, *, account_bound=False, live_bound=False, send_confirmed=False):
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
    if profile.live_markers:
        return reviewed_hierarchy(root, nodes, profile, result, account_bound, live_bound, send_confirmed)
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


def reviewed_hierarchy(root, nodes, profile, result, account_bound, live_bound, send_confirmed):
    if any(n.get("package") not in {None, "", profile.package} for n in nodes): return result
    if any(n.get("class") == "android.app.Dialog" for n in nodes): return result
    groups = {}
    for n in nodes: groups.setdefault(n.get("resource-id"), []).append(n)
    def one(identity):
        matches = groups.get(identity, []); return matches[0] if len(matches) == 1 else None
    overlay = (("n5z", "android.view.ViewGroup"), ("n5m", "android.widget.LinearLayout"),
               ("n5w", "android.widget.LinearLayout"), ("n60", "android.widget.TextView"))
    overlay_nodes = [one(rid(identity)) for identity, _ in overlay]
    if all(n is not None and n.get("class") == klass and n.get("enabled") == "true"
           for n, (_, klass) in zip(overlay_nodes, overlay)):
        if all(child in list(parent) for parent, child in zip(overlay_nodes, overlay_nodes[1:])):
            return result | dict(screen=ScreenState.TRANSIENT_OVERLAY, reason="transient_overlay",
                profile_verified=True, account_verified=account_bound)
    live = all((n := one(identity)) is not None and n.get("class") == klass and n.get("enabled") == "true"
        for identity, klass in profile.live_markers)
    composer = composer_shape(root)
    result.update(profile_verified=True, account_verified=account_bound, live_verified=live or composer and live_bound)
    entry, send, child = one(profile.input_id), one(profile.send_id), one(rid("vpl"))
    if (composer and entry is not None and entry.get("class") == "android.widget.EditText" and
            all(entry.get(k) == "true" for k in ("enabled", "clickable", "focusable", "focused")) and
            entry.get("password") != "true"):
        # Recognizing the field is independent of granting the account/session
        # lease. Missing LIVE proof still blocks fill, confirmation and send.
        result.update(screen=ScreenState.COMMENT_INPUT_READY, comment_input_found=True)
    if not result["live_verified"]: return result | dict(reason="live_unverified")
    if entry is not None or groups.get(profile.input_id):
        if (not composer or entry is None or entry.get("class") != "android.widget.EditText" or
                any(entry.get(k) != "true" for k in ("enabled", "focusable")) or entry.get("password") == "true"):
            return result | dict(reason="unknown_screen", live_verified=False)
    result["comment_input_found"] = entry is not None
    candidate = (composer and send is not None and child is not None and send.get("class") == "android.widget.FrameLayout"
        and child.get("class") == "android.widget.ImageView" and all(n.get(k) == "true" for n in (send,child) for k in ("clickable","focusable"))
        and all(n.get("enabled") in {"true", "false"} for n in (send, child)))
    result["send_button_found"] = bool(candidate and send_confirmed)
    result["send_control_state"] = "VERIFIED" if candidate and send_confirmed else "SEND_CONTROL_CANDIDATE" if candidate else "UNAVAILABLE"
    screen = ScreenState.COMMENT_INPUT_READY if composer and entry is not None else ScreenState.LIVE_READY
    reason = "account_unverified" if not account_bound else "input_unavailable" if not composer else "send_control_unverified" if not candidate or not send_confirmed else "ready"
    return result | dict(screen=screen, reason=reason)


def resolved_send_point(xml, profile):
    """A single reviewed child, visible nonempty bounds contained by its ancestors."""
    root = parse_hierarchy(xml)
    if root is None or not composer_shape(root): return None
    def one(identity):
        matches = [n for n in root.iter() if n.get("resource-id") == identity]
        return matches[0] if len(matches) == 1 else None
    parent, child = one(profile.send_id), one(rid("vpl"))
    if parent is None or child is None or child not in list(parent): return None
    if (parent.get("class") != "android.widget.FrameLayout" or child.get("class") != "android.widget.ImageView"
            or any(n.get(k) != "true" for n in (parent, child) for k in ("enabled", "clickable"))): return None
    ancestors = {c: p for p in root.iter() for c in p}
    bounds = []
    current = child
    while current is not root:
        if current.get("visible-to-user", "true") != "true": return None
        match = re.fullmatch(r"\[(\d{1,4}),(\d{1,4})\]\[(\d{1,4}),(\d{1,4})\]", current.get("bounds", ""))
        if not match: return None
        x1,y1,x2,y2 = map(int, match.groups())
        if not (0 <= x1 < x2 <= 8192 and 0 <= y1 < y2 <= 8192): return None
        bounds.append((x1,y1,x2,y2))
        current = ancestors[current]
    for inner, outer in zip(bounds, bounds[1:]):
        if not (outer[0] <= inner[0] < inner[2] <= outer[2] and outer[1] <= inner[1] < inner[3] <= outer[3]): return None
    x1,y1,x2,y2 = bounds[0]
    return ((x1+x2)//2, (y1+y2)//2)
