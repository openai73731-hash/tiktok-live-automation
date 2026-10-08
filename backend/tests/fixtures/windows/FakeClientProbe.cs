// Exact production acquisition/serializer/provider boundaries; never calls oleacc.
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
namespace LiveStudioDiscovery {
    sealed class FakeClientDesktop : FakeDesktop, IAccessibleObjectSource {
        internal string mode;
        internal bool expired;
        internal readonly List<string> trace = new List<string>();
        internal readonly List<FakeAccessibleReads> reads = new List<FakeAccessibleReads>();
        internal readonly Dictionary<string, FakeIa2Com> bridges = new Dictionary<string, FakeIa2Com>();
        public ObjectAcquisition OpenAccessible(long hwnd, int objectId) { return ObjectAcquisition.Read(new FakeObjectApi(this), hwnd, objectId); }
    }
    sealed class FakeObjectApi : IObjectApi {
        readonly FakeClientDesktop desktop;
        internal FakeObjectApi(FakeClientDesktop value) { desktop = value; }
        public int FromWindow(long hwnd, uint objectId, Guid iid, out IAccessibleReads value) {
            bool client = objectId == unchecked((uint)-4);
            if (!client && objectId != 0) throw new Exception("BAD OBJECT ID");
            string key = hwnd + ":" + (client ? "client" : "window");
            desktop.trace.Add(key + ":open:" + objectId + ":" + iid);
            var bridge = new FakeIa2Com { providerHr = client ? 0 : unchecked((int)0x80004002) };
            if (desktop.mode == "dual_window_provider" && !client) bridge.providerHr = 0;
            var root = new FakeAccessibleReads { identity = "PRIVATE COM ADDRESS " + key, selfRole = client ? 15 : 9,
                variants = client ? new object[] {1, 2, 3, new FakeAccessibleReads { identity = key + ":pane", selfRole = 16, variants = new object[] {1} }} : new object[] {1, 2}, ia2 = bridge };
            if (desktop.mode == "dual_shell") { root.selfRole = 9; root.variants = new object[] {1}; bridge.queryHr = unchecked((int)0x80070057); }
            if (desktop.mode == "dual_protected_client" && client && hwnd == 200) root.protectedSelf = true;
            if (desktop.mode == "dual_unknown_client" && client && hwnd == 200) root.unknownSelf = true;
            if (desktop.mode == "dual_protected_window" && !client && hwnd == 200) root.protectedSelf = true;
            root.observe = delegate(string property, int child) { desktop.trace.Add(key + ":" + property + ":" + child); };
            bridge.queried = delegate {
                desktop.trace.Add(key + ":query");
                if (desktop.mode == "dual_deadline") desktop.expired = true;
                if (desktop.mode == "dual_scope_after" && hwnd == 200) desktop.ownerOverrides[200] = 555;
            };
            desktop.reads.Add(root); desktop.bridges[key] = bridge; value = root;
            if (client && desktop.mode == "dual_client_error") throw new COMException("PRIVATE EXCEPTION ADDRESS 0x76543210", unchecked((int)0x80070005));
            if (client && (desktop.mode == "dual_client_no_interface" || desktop.mode == "dual_both_failure")) return unchecked((int)0x80004002);
            if (!client && (desktop.mode == "dual_window_failure" || desktop.mode == "dual_both_failure")) return unchecked((int)0x80004005);
            return 0;
        }
    }
    public static class ClientHarness {
        public static Dictionary<string, object> All() {
            var result = new Dictionary<string, object>();
            foreach (string mode in new string[] {"dual_tree", "dual_shell", "dual_client_no_interface", "dual_client_error", "dual_window_failure", "dual_window_provider",
                "dual_both_failure", "dual_names", "dual_protected_client", "dual_unknown_client", "dual_protected_window", "dual_budget", "dual_depth", "dual_deadline", "dual_priority", "dual_hidden_renderer", "dual_scope_after"}) {
                var desktop = new FakeClientDesktop { mode = mode };
                if (mode == "dual_hidden_renderer") {
                    desktop.childFactory = delegate(long hwnd) { var children = new List<long>(); if (hwnd == 100) { for (int i = 150; i < 190; i++) children.Add(i); children.Add(200); } return children; };
                    desktop.windowFactory = delegate(long hwnd) { return new WindowRow { hwnd = hwnd, process_id = desktop.WindowProcess(hwnd),
                        parent_hwnd = hwnd == 100 ? (long?)null : 100, class_name = hwnd == 100 ? "Chrome_WidgetWin_1" : "Chrome_RenderWidgetHostHWND",
                        visible = hwnd == 100 || hwnd == 200, enabled = true, bounds = new int[] {0, 0, 10, 20} }; };
                }
                if (mode == "dual_priority") {
                    var scan = new TopWindowScan(11300);
                    for (int i = 114; i >= 101; i--) scan.Observe(i, 11300, false);
                    scan.Observe(100, 11300, true);
                    desktop.stats = scan.Statistics; desktop.limits = scan.Limits; desktop.hiddenRoots = true;
                    desktop.topFactory = delegate(int pid) { return scan.Roots; };
                }
                var report = new Collector(desktop, 256, mode == "dual_budget" ? 4 : 500, mode == "dual_depth" ? 1 : 12,
                    mode == "dual_names" || mode == "dual_protected_client" || mode == "dual_unknown_client" || mode == "dual_protected_window", delegate { return desktop.expired; }).Run(11300, "client");
                var calls = new Dictionary<string, object>(); int closes = 0;
                foreach (var pair in desktop.bridges) calls[pair.Key] = pair.Value.calls;
                foreach (var read in desktop.reads) closes += read.closes;
                result[mode] = new Dictionary<string, object> {{"report", report}, {"trace", desktop.trace}, {"calls", calls},
                    {"acquired_count", desktop.reads.Count}, {"closes", closes}};
            }
            return result;
        }
    }
}
