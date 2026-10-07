"""Bounded reviewed host-feed evidence. UI display names never bind an account."""
from dataclasses import dataclass
from app.android.tiktok_ui import parse_hierarchy, rid


def normalized(text):
    # The real f41 captures prepend one LRM. Preserve internal spaces, case and punctuation.
    return text.removeprefix("\u200e").strip()


@dataclass(frozen=True)
class HostFeed:
    host: str
    pairs: frozenset[tuple[str, str]]


def host_feed(xml):
    root = parse_hierarchy(xml)
    def one(name):
        matches = [n for n in root.iter() if n.get("resource-id") == rid(name)] if root is not None else []
        return matches[0] if len(matches) == 1 else None
    header, host, feed = (one(n) for n in ("user_info_container", "user_name", "oyh"))
    if root is None or header is None or host is None or feed is None: return None
    if (header.get("class") != "android.view.ViewGroup" or header.get("enabled") != "true"
            or feed.get("class") != "androidx.recyclerview.widget.RecyclerView" or feed.get("enabled") != "true"
            or host not in header.iter() or host.get("class") != "android.widget.TextView"
            or host.get("enabled") != "true" or not normalized(host.get("text", ""))): return None
    pairs = set()
    for row in feed:
        if row.get("resource-id") != rid("msx") or row.get("class") != "android.view.ViewGroup": continue
        names = [n for n in row.iter() if n.get("resource-id") == rid("poq")]
        texts = [n for n in row.iter() if n.get("resource-id") == rid("f41")]
        wrappers = [n for n in row if n.get("resource-id") == rid("pov")]
        if len(names) != 1 or len(texts) != 1 or len(wrappers) != 1: continue
        name, text = names[0], texts[0]
        if (row.get("enabled") != "true" or wrappers[0].get("class") != "android.view.ViewGroup"
                or wrappers[0].get("enabled") != "true" or name not in list(wrappers[0]) or text not in list(row) or
                any(n.get("class") != "android.widget.TextView" or n.get("enabled") != "true"
                    for n in (name, text))): continue
        pairs.add((normalized(name.get("text", "")), normalized(text.get("text", ""))))
    return HostFeed(normalized(host.get("text", "")), frozenset(pairs))


def newly_confirmed(before, after, reply):
    pair = (before.host, normalized(reply)) if before else None
    return bool(before and after and before.host == after.host and pair not in before.pairs and pair in after.pairs)
