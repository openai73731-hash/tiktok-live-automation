// No native desktop/COM calls: production contract and serializer, fake boundary.
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
namespace LiveStudioDiscovery {
    sealed class FakeIa2Com : IIa2Com {
        internal int providerHr, queryHr, closes;
        internal bool nullResult, throwQuery;
        internal Action queried;
        internal readonly List<string> calls = new List<string>();
        public int QueryProvider(Guid iid) { calls.Add("provider:" + iid); return providerHr; }
        public int QueryService(Guid service, Guid iid, out bool available) {
            calls.Add("service:" + service + ":" + iid);
            if (queried != null) queried();
            if (throwQuery) throw new COMException("PRIVATE COM ADDRESS 0x76543210", unchecked((int)0x80070005));
            available = queryHr == 0 && !nullResult; return queryHr;
        }
        public void Dispose() { closes++; }
    }
    public static class Ia2Harness {
        public static Dictionary<string, object> All() {
            var result = new Dictionary<string, object>();
            foreach (string mode in new string[] {"ia2_no_provider", "ia2_no_interface", "ia2_unchanged", "ia2_expands", "ia2_names",
                "ia2_protected", "ia2_unknown", "ia2_query_error", "ia2_null", "ia2_budget", "ia2_depth", "ia2_deadline", "ia2_scope", "ia2_scope_after", "ia2_priority", "ia2_primary_failure", "ia2_check_cap", "ia2_diagnostics"}) {
                var trace = new List<string>(); bool timedOut = false;
                var reads = new Dictionary<long, FakeAccessibleReads>();
                var bridges = new Dictionary<long, FakeIa2Com>();
                var desktop = new FakeDesktop();
                desktop.accessibleByHwnd = delegate(long hwnd) {
                    var node = new FakeAccessibleReads { identity = "PRIVATE ROOT ADDRESS " + hwnd, variants = new object[] {1} };
                    var bridge = new FakeIa2Com();
                    if (mode == "ia2_diagnostics") {
                        var variants = new FakeVariantApi { tags = new int[100], obtained = 100 };
                        for (int i = 0; i < 100; i++) variants.tags[i] = 8;
                        node.variants = new object[100]; node.nativeVariants = variants;
                    }
                    if (mode == "ia2_no_provider") bridge.providerHr = unchecked((int)0x80004002);
                    if (mode == "ia2_primary_failure" && hwnd == 200) bridge.providerHr = unchecked((int)0x80004002);
                    if (mode == "ia2_no_interface") bridge.queryHr = unchecked((int)0x80004002);
                    if (mode == "ia2_query_error") bridge.throwQuery = true;
                    if (mode == "ia2_null") bridge.nullResult = true;
                    if (hwnd == 200 && mode == "ia2_protected") node.protectedSelf = true;
                    if (hwnd == 200 && mode == "ia2_unknown") node.unknownSelf = true;
                    node.observe = delegate(string property, int child) { trace.Add(hwnd + ":" + property + ":" + child); };
                    bridge.queried = delegate {
                        trace.Add(hwnd + ":query");
                        if (mode == "ia2_deadline") timedOut = true;
                        if (mode == "ia2_scope") desktop.ownerOverrides[200] = 555;
                        if (mode == "ia2_scope_after" && hwnd == 200) desktop.ownerOverrides[200] = 555;
                        if (hwnd == 200 && (mode == "ia2_expands" || mode == "ia2_names" || mode == "ia2_budget" || mode == "ia2_depth"))
                            node.variants = new object[] { new FakeAccessibleReads { identity = "PRIVATE COM CHILD", variants = new object[] {1, 2} } };
                    };
                    node.ia2 = bridge; reads[hwnd] = node; bridges[hwnd] = bridge;
                    return new NativeAccessible(node, 0, true);
                };
                if (mode == "ia2_primary_failure") {
                    desktop.secondWidget = true;
                    desktop.topFactory = delegate(int pid) { return new long[] {100, 101}; };
                    desktop.stats = new RootScanStats { target_roots_total = 2, target_roots_selected = 2 };
                    desktop.childFactory = delegate(long hwnd) { return hwnd == 100 ? new long[] {200} : hwnd == 101 ? new long[] {400} : new long[0]; };
                }
                if (mode == "ia2_check_cap") desktop.childFactory = delegate(long hwnd) {
                    var children = new List<long>();
                    if (hwnd == 100) { children.Add(200); for (int i = 400; i <= 440; i++) children.Add(i); }
                    return children;
                };
                if (mode == "ia2_priority") {
                    var scan = new TopWindowScan(11300);
                    for (int i = 114; i >= 101; i--) scan.Observe(i, 11300, false);
                    scan.Observe(100, 11300, true);
                    desktop.limits = scan.Limits; desktop.stats = scan.Statistics; desktop.hiddenRoots = true;
                    desktop.topFactory = delegate(int pid) { return scan.Roots; };
                }
                var report = new Collector(desktop, 256, mode == "ia2_budget" ? 4 : 500, mode == "ia2_depth" ? 1 : 12,
                    mode == "ia2_names" || mode == "ia2_protected" || mode == "ia2_unknown", delegate { return timedOut; }).Run(11300, "ia2");
                var calls = new Dictionary<string, object>(); int closes = 0, nodeCloses = 0;
                foreach (var item in bridges) { calls[item.Key.ToString()] = item.Value.calls; closes += item.Value.closes; }
                foreach (var node in reads.Values) nodeCloses += node.closes;
                result[mode] = new Dictionary<string, object> { {"report", report}, {"calls", calls}, {"trace", trace},
                    {"bridge_closes", closes}, {"root_closes", nodeCloses}, {"accessible_calls", desktop.accessibleCalls} };
            }
            return result;
        }
    }
}
