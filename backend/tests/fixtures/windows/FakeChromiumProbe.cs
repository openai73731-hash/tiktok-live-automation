// In-memory activation/focus boundaries only: no NativeDesktop, HWND, hook or message API.
using System;
using System.Collections.Generic;
namespace LiveStudioDiscovery {
    sealed class FakeAxDesktop : FakeDesktop, IAccessibleObjectSource, IAxDesktop {
        internal string mode;
        internal readonly List<string> trace = new List<string>();
        internal readonly List<FakeAccessibleReads> reads = new List<FakeAccessibleReads>();
        internal readonly Dictionary<long, int> opens = new Dictionary<long, int>();
        internal readonly FakeAxApi api;
        internal bool expired;
        internal FakeAxDesktop(string value) {
            mode = value; api = new FakeAxApi(this);
            ownerOverrides[200] = 11300; ownerOverrides[400] = 11300; ownerOverrides[401] = 11300;
            topFactory = delegate(int pid) { return new long[] {100, 101}; };
            stats = new RootScanStats { target_roots_total = 2, target_roots_selected = 2 };
            childFactory = delegate(long hwnd) { return hwnd == 100 ? new long[] {200, 300} : hwnd == 101 ? new long[] {400} : new long[0]; };
            windowFactory = delegate(long hwnd) {
                bool root = hwnd == 100 || hwnd == 101;
                string cls = root ? "Chrome_WidgetWin_1" : (hwnd >= 150 && hwnd <= 200) || hwnd == 400 ? "Chrome_RenderWidgetHostHWND" : "Chrome_Other";
                if (mode == "ax_class_changed" && hwnd == 200 && opens.ContainsKey(hwnd)) cls = "Chrome_Other";
                return new WindowRow { hwnd = hwnd, process_id = WindowProcess(hwnd), parent_hwnd = root ? (long?)null : hwnd == 400 ? 101 : 100,
                    visible = !(hwnd >= 150 && hwnd < 200) && (mode != "ax_hidden" || (hwnd != 101 && hwnd != 400)), enabled = true, class_name = cls,
                    bounds = new int[] {17, 23, 901, 407}, owner_hwnd = root ? (long?)100 : null,
                    ancestor_root = root ? (long?)hwnd : null, ancestor_rootowner = root ? (long?)100 : null,
                    style = root ? (uint?)0x10C40000 : null, exstyle = root ? (uint?)0x40000 : null,
                    style_flags = root ? WindowArchitecture.Style(0x10C40000, false) : new List<string>(),
                    exstyle_flags = root ? WindowArchitecture.Style(0x40000, true) : new List<string>(),
                    client_rect = root ? new int[] {0, 0, 901, 407} : null, window_rect = root ? new int[] {17, 23, 918, 430} : null,
                    z_order_index = root ? (int?)(hwnd - 100) : null, cloaked = root ? (int?)0 : null };
            };
            if (mode == "ax_foreign") ownerOverrides[200] = 11301;
            if (mode == "ax_check_cap") {
                for (int i = 150; i < 190; i++) ownerOverrides[i] = 11300;
                childFactory = delegate(long hwnd) { var children = new List<long>(); if (hwnd == 100) { for (int i = 150; i < 190; i++) children.Add(i); children.Add(200); } if (hwnd == 101) children.Add(400); return children; };
            }
        }
        public IAxApi AxApi() { return api; }
        public ObjectAcquisition OpenAccessible(long hwnd, int objectId) {
            if (objectId != -4) throw new Exception("WRONG OBJECT ID");
            int count; opens.TryGetValue(hwnd, out count); opens[hwnd] = ++count;
            trace.Add(hwnd + ":open:" + objectId + ":" + count);
            if (mode == "ax_before_unavailable" && count == 1 || mode == "ax_after_unavailable" && count > 1)
                return new ObjectAcquisition { Hresult = unchecked((int)0x80004002) };
            bool expanded = count >= (mode == "ax_delayed" ? 4 : 2) && (mode == "ax_expands" || mode == "ax_delayed" || mode == "ax_names" || mode == "ax_negative" || mode == "ax_pane");
            var bridge = new FakeIa2Com(); bridge.queried = delegate { trace.Add(hwnd + ":ia2:fresh:" + count); };
            var node = new FakeAccessibleReads { identity = "PRIVATE ADDRESS " + hwnd + ":" + count, selfRole = expanded ? (mode == "ax_pane" ? 16 : 15) : 10, ia2 = bridge,
                variants = expanded ? new object[] {1} : new object[0] };
            if (expanded && mode == "ax_negative") {
                node.nativeVariants = new FakeVariantApi { tags = new int[] {3}, obtained = 1, badId = true, badIdValue = -100005 };
                node.objects[-100005] = new FakeAccessibleReads { identity = "negative-" + hwnd, selfRole = 42 };
            }
            if (mode == "ax_protected") node.protectedSelf = true;
            if (mode == "ax_unknown_state") node.unknownSelf = true;
            node.observe = delegate(string property, int child) { trace.Add(hwnd + ":" + count + ":" + property + ":" + child); };
            reads.Add(node);
            if (mode == "ax_scope_changed" && hwnd == 200) ownerOverrides[200] = 555;
            return new ObjectAcquisition { Node = new NativeAccessible(node, 0, true), Hresult = 0 };
        }
    }
    sealed class FakeAxApi : IAxApi, IFocusApi {
        readonly FakeAxDesktop desktop;
        internal long clock;
        internal int closes, eventOpens, guiReads;
        internal FakeAxApi(FakeAxDesktop value) { desktop = value; }
        public long Milliseconds { get { return clock; } }
        public void Pause(int milliseconds) { clock += milliseconds; if (desktop.mode == "ax_disappears") desktop.ownerOverrides[200] = 555; }
        public AxSend HoneyPot(long hwnd, int pid, int timeoutMs) {
            desktop.trace.Add(hwnd + ":activate:" + pid + ":" + timeoutMs);
            if (desktop.WindowProcess(hwnd) != pid || desktop.Window(hwnd, false).class_name != "Chrome_RenderWidgetHostHWND" || timeoutMs != 250) throw new Exception("BAD SCOPE");
            if (desktop.mode == "ax_deadline") desktop.expired = true;
            if (desktop.mode == "ax_timeout") { clock += timeoutMs; return new AxSend { attempted = true, status = "timeout" }; }
            return new AxSend { attempted = true, status = "completed" };
        }
        public long Root(long hwnd) { return hwnd == 400 || hwnd == 401 ? 101 : hwnd == 200 || hwnd == 100 ? 100 : 999; }
        public bool Descendant(long parent, long hwnd) { return parent == 400 && hwnd == 401; }
        public FocusObject EventObject(long hwnd, int objectId, int childId) {
            eventOpens++; desktop.trace.Add("event:" + hwnd + ":" + objectId + ":" + childId);
            if (desktop.mode == "focus_failure") return new FocusObject { Hresult = unchecked((int)0x80070005) };
            var node = new FakeAccessibleReads { selfRole = childId == 0 && desktop.mode != "focus_self_edit" ? 10 : 42,
                protectedSelf = desktop.mode == "focus_protected", unknownSelf = desktop.mode == "focus_unknown" };
            node.observe = delegate(string property, int child) { desktop.trace.Add("focus:" + property); };
            desktop.reads.Add(node);
            return new FocusObject { Node = new NativeAccessible(node, 0, true), Hresult = 0, Kind = "accessible_object" };
        }
        public GuiEvidence Gui(int pid) {
            guiReads++;
            return new GuiEvidence { hwndActive = 101, hwndFocus = 401,
                hwndCaret = desktop.mode == "focus_caret" ? (long?)401 : desktop.mode == "focus_foreign_gui" ? (long?)555 : null,
                rcCaret = new int[] {3, 4, 4, 19}, flags = 1 };
        }
        public FocusEvidence Watch(int seconds, int pid, IReadOnlyDesktop source, List<WindowRow> windows, bool names, Func<bool> expired) {
            var evidence = new FocusEvidence { requested_seconds = seconds, status = "complete" };
            if (desktop.mode == "focus_foreign_gui") { desktop.ownerOverrides[401] = 555; }
            FocusCollector.Observe(this, evidence, source, windows, pid, names, 555, -4, -100009, expired);
            FocusCollector.Observe(this, evidence, source, windows, pid, names, 400, -4, desktop.mode == "focus_shell" || desktop.mode == "focus_self_edit" ? 0 : -100009, expired);
            if (desktop.mode == "focus_cap") for (int i = 0; i < 70; i++) FocusCollector.Observe(this, evidence, source, windows, pid, names, 400, -4, 1, expired);
            if (desktop.mode == "focus_descendant") FocusCollector.Observe(this, evidence, source, windows, pid, names, 401, -4, 1, expired);
            return evidence;
        }
        public void Dispose() { closes++; }
    }
    public static class AxHarness {
        public static Dictionary<string, object> All() {
            var result = new Dictionary<string, object>();
            result["honeypot_api_result"] = new Dictionary<string, object> {
                {"zero_target_result", NativeAxApi.InterpretDelivery(new IntPtr(1), UIntPtr.Zero, 1460)},
                {"nonzero_target_result", NativeAxApi.InterpretDelivery(new IntPtr(1), new UIntPtr(7), 0)},
                {"api_failed", NativeAxApi.InterpretDelivery(IntPtr.Zero, new UIntPtr(7), 1460)}};
            foreach (string mode in new string[] {"ax_expands", "ax_delayed", "ax_unchanged", "ax_timeout", "ax_disappears", "ax_foreign", "ax_scope_changed", "ax_class_changed", "ax_hidden", "ax_names", "ax_negative", "ax_protected", "ax_unknown_state", "ax_budget", "ax_deadline", "ax_check_cap", "ax_before_unavailable", "ax_after_unavailable", "ax_pane",
                "ax_normal_hwnd", "ax_normal_msaa", "ax_normal_client", "ax_normal_ia2", "focus_inner", "focus_shell", "focus_self_edit", "focus_failure", "focus_caret", "focus_foreign_gui", "focus_names", "focus_protected", "focus_unknown", "focus_cap", "focus_descendant"}) {
                var desktop = new FakeAxDesktop(mode);
                string backend = mode.StartsWith("ax_normal_") ? mode.Substring(10) : "chromium-ax";
                bool watch = mode.StartsWith("focus_");
                var data = new Collector(desktop, 256, mode == "ax_budget" ? 2 : 500, 12,
                    mode == "ax_names" || mode == "focus_names" || mode == "focus_protected" || mode == "focus_unknown",
                    delegate { return desktop.expired; }, watch ? 15 : 0).Run(11300, backend);
                int closes = 0; foreach (var read in desktop.reads) closes += read.closes;
                result[mode] = new Dictionary<string, object> {{"report", data}, {"trace", desktop.trace}, {"acquired", desktop.reads.Count}, {"closes", closes},
                    {"api_closes", desktop.api.closes}, {"event_opens", desktop.api.eventOpens}, {"gui_reads", desktop.api.guiReads}};
            }
            return result;
        }
    }
}
