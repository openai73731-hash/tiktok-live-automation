"""One reviewed two-step host path: More -> Leave comment. No coordinate fallback."""
import re
from app.android.models import ScreenState
from app.android.tiktok_ui import parse_hierarchy, rid, TIKTOK_YOU_4683, inspect_hierarchy


def contained_point(root, node):
    parents = {c:p for p in root.iter() for c in p}
    bounds = []
    while node is not root:
        if node.get("visible-to-user", "true") != "true" or node.get("enabled") != "true": return None
        match = re.fullmatch(r"\[(\d{1,4}),(\d{1,4})\]\[(\d{1,4}),(\d{1,4})\]", node.get("bounds", ""))
        if not match: return None
        x1,y1,x2,y2 = map(int, match.groups())
        if not (0 <= x1 < x2 <= 8192 and 0 <= y1 < y2 <= 8192): return None
        bounds.append((x1,y1,x2,y2))
        node = parents[node]
    for inner, outer in zip(bounds, bounds[1:]):
        if not (outer[0] <= inner[0] < inner[2] <= outer[2] and outer[1] <= inner[1] < inner[3] <= outer[3]): return None
    x1,y1,x2,y2 = bounds[0]
    return (x1+x2)//2, (y1+y2)//2


def resolved_composer_open_point(xml, profile, *, menu=False):
    if profile != TIKTOK_YOU_4683: return None
    root = parse_hierarchy(xml)
    if root is None: return None
    nodes = list(root.iter('node'))
    if any(n.get('package') != profile.package or n.get('class') == 'android.app.Dialog' for n in nodes): return None
    parsed = inspect_hierarchy(xml, profile.package, None, profile)
    if parsed['security_state']: return None
    parents = {c:p for p in root.iter() for c in p}
    def one(identity, klass):
        values = [n for n in nodes if n.get('resource-id') == rid(identity) and n.get('class') == klass]
        return values[0] if len(values) == 1 else None
    if not menu:
        if parsed['screen'] != ScreenState.LIVE_READY or not parsed['live_verified']: return None
        chain = [one(identity.rsplit(':id/', 1)[-1], klass) for identity, klass in profile.composer_host_path]
        if not chain: return None
        if any(n is None for n in chain) or any(parents[b] is not a for a,b in zip(chain,chain[1:])): return None
        target = chain[-1]
        for name, klass in [('z_6','android.widget.FrameLayout'),('z_7','android.view.ViewGroup'),('z9z','android.widget.ImageView')]:
            children = [n for n in target if n.get('resource-id') == rid(name) and n.get('class') == klass]
            if len(children) != 1: return None
            target = children[0]
        if contained_point(root, target) is None: return None
        target = chain[-1]
    else:
        allowed = {'android:id/content', *(rid(n) for n in ('a3d','byz','f0t','f41','f7x','g0z','k9n','m79','ngx',
            'q0_','u7r','ujj','ukp','v34','vve','vxy','w46','z9z','z_m','z_o','zcs','zjn','zzy'))}
        if any(n.get('resource-id') and n.get('resource-id') not in allowed for n in nodes): return None
        labels = [n for n in nodes if n.get('resource-id') == rid('z_o') and n.get('class') == 'android.widget.TextView'
            and n.get('text') == profile.composer_menu_label and n.get('enabled') == 'true' and n.get('clickable') == 'false']
        if len(labels) != 1: return None
        target = parents[labels[0]]
        if target.get('resource-id') != rid('zzy') or target.get('class') != 'android.view.ViewGroup': return None
        if sorted((n.get('resource-id'),n.get('class')) for n in target) != sorted([
                (rid('z9z'),'android.widget.ImageView'),(rid('z_o'),'android.widget.TextView')]): return None
        if any(contained_point(root, n) is None for n in target): return None
        ancestor = target
        for name, klass in [('byz','android.widget.LinearLayout'),('f41','android.widget.LinearLayout'),('v34','android.widget.ScrollView')]:
            ancestor = parents[ancestor]
            if ancestor is not one(name,klass): return None
        # Exact reviewed menu ancestry, including its inert anonymous wrapper.
        ancestor = parents[ancestor]
        if ancestor.get('resource-id') or ancestor.get('class') != 'android.view.ViewGroup': return None
        for name, klass in [('k9n','android.widget.LinearLayout'),('w46','android.widget.FrameLayout'),
                ('g0z','android.view.ViewGroup'),('f7x','android.view.ViewGroup'),('f0t','android.widget.FrameLayout')]:
            ancestor = parents[ancestor]
            if ancestor is not one(name,klass): return None
    if target.get('clickable') != 'true' or target.get('enabled') != 'true': return None
    return contained_point(root, target)
