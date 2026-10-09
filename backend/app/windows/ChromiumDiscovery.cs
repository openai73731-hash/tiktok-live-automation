// Opt-in diagnostic activation and out-of-context observation; never physical input.
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Threading;
using Accessibility;

namespace LiveStudioDiscovery {
    public sealed class WindowArchitecture {
        public long? primary_main_window;
        public string primary_selection_basis = "first_selected_visible_widget";
        public List<long> secondary_visible_chromium_windows = new List<long>(), secondary_renderer_hwnds = new List<long>();
        internal static WindowArchitecture Describe(List<WindowRow> windows, int pid) {
            var result = new WindowArchitecture();
            foreach (var root in windows) if (root.depth == 0 && root.process_id == pid && root.chromium_kind == "widget") {
                root.direct_renderer_hwnds.Clear();
                foreach (var child in windows) if (child.parent_hwnd == root.hwnd && child.chromium_kind == "render_widget") root.direct_renderer_hwnds.Add(child.hwnd);
                if (!root.visible) continue;
                if (!result.primary_main_window.HasValue) { result.primary_main_window = root.hwnd; continue; }
                result.secondary_visible_chromium_windows.Add(root.hwnd);
                foreach (var child in windows) if (root.direct_renderer_hwnds.Contains(child.hwnd) && child.visible) { root.chat_window_candidate = true; result.secondary_renderer_hwnds.Add(child.hwnd); }
            }
            return result;
        }
        internal static List<string> Style(uint style, bool extended) {
            uint[] bits = extended ? new uint[] {8, 0x80, 0x100, 0x40000, 0x8000000} : new uint[] {0x80000000, 0x40000000, 0x10000000, 0x8000000, 0xC00000, 0x40000, 0x80000};
            string[] names = extended ? new string[] {"topmost", "toolwindow", "windowedge", "appwindow", "noactivate"} : new string[] {"popup", "child", "visible", "disabled", "caption", "thickframe", "sysmenu"};
            var result = new List<string>(); for (int i = 0; i < bits.Length; i++) if ((style & bits[i]) == bits[i]) result.Add(names[i]); return result;
        }
    }
    public interface IAxDesktop { IAxApi AxApi(); }
    public interface IAxApi : IDisposable {
        long Milliseconds { get; }
        void Pause(int milliseconds);
        AxSend HoneyPot(long hwnd, int pid, int timeoutMs);
        FocusEvidence Watch(int seconds, int pid, IReadOnlyDesktop desktop, List<WindowRow> windows, bool names, Func<bool> expired);
    }
    public sealed class AxSend { public bool attempted; public string status = "scope_rejected"; }
    public sealed class AxCheck {
        public long source_hwnd, root_hwnd;
        public int target_pid, message = 0x3D, honeypot_object_id = 1, wparam_object_id = -4, timeout_ms = 250, poll_attempts;
        public bool visible, secondary_window, attempted, comparison_complete;
        public string send_status = "not_attempted", baseline_hresult_category, after_hresult_category;
        public int? baseline_role, baseline_child_count, after_role, after_child_count;
        public bool? tree_changed;
        public long activation_elapsed_ms;
        public Ia2Capability ia2 = new Ia2Capability { status = "not_queried_shell" };
    }
    internal sealed class AxEvidence {
        internal readonly List<AxCheck> Checks = new List<AxCheck>();
        internal readonly MsaaSnapshot Before = new MsaaSnapshot(), After = new MsaaSnapshot();
        internal FocusEvidence Focus = new FocusEvidence();
        internal bool Truncated, Performed; internal int Unreadable;
        internal readonly List<string> Errors = new List<string>();
        internal void Note(string code) { if (Errors.Count < 16 && !Errors.Contains(code)) Errors.Add(code); }
        internal void Merge(MsaaSnapshot snapshot, bool before) {
            (before ? Before : After).Merge(snapshot, Math.Max(0, 64 - (before ? After : Before).Diagnostics.Count));
            Truncated |= snapshot.Truncated; Unreadable += snapshot.Unreadable;
            foreach (string code in snapshot.Errors) Note(code);
        }
        internal Dictionary<string, object> Section() { return new Dictionary<string, object> {{"checks", Checks}, {"before_msaa", Before.Section()}, {"after_msaa", After.Section()}}; }
    }
    internal static class AxExperiment {
        static bool Scope(IReadOnlyDesktop desktop, WindowRow row, int pid) {
            try { return desktop.WindowProcess(row.hwnd) == pid && desktop.WindowProcess(row.root_hwnd) == pid && desktop.Window(row.hwnd, false).class_name == "Chrome_RenderWidgetHostHWND"; }
            catch { return false; }
        }
        internal static AxEvidence Run(IReadOnlyDesktop desktop, List<WindowRow> windows, int pid, int nodes, int depth, bool names, Func<bool> expired, int watchSeconds) {
            var result = new AxEvidence(); var source = desktop as IAccessibleObjectSource; var native = desktop as IAxDesktop;
            if (source == null || native == null) { result.Unreadable++; result.Note("native_unavailable"); return result; }
            var architecture = WindowArchitecture.Describe(windows, pid);
            var renderers = windows.FindAll(delegate(WindowRow w) { return w.chromium_kind == "render_widget"; });
            renderers.Sort(delegate(WindowRow a, WindowRow b) { int x = Rank(a, architecture), y = Rank(b, architecture); return x == y ? a.hwnd.CompareTo(b.hwnd) : x.CompareTo(y); });
            if (renderers.Count > 32) { renderers.RemoveRange(32, renderers.Count - 32); result.Truncated = true; result.Note("activation_check_limit"); }
            using (var api = native.AxApi()) {
                var seen = new HashSet<long>();
                for (int i = 0; i < renderers.Count; i++) {
                    var row = renderers[i]; var check = new AxCheck { source_hwnd = row.hwnd, root_hwnd = row.root_hwnd, target_pid = pid,
                        visible = row.visible, secondary_window = architecture.secondary_visible_chromium_windows.Contains(row.root_hwnd) };
                    result.Checks.Add(check);
                    if (!seen.Add(row.hwnd) || !Scope(desktop, row, pid)) { check.send_status = "scope_rejected"; continue; }
                    if (expired()) { check.send_status = "deadline"; result.Truncated = true; result.Note("timeout"); continue; }
                    int beforeTotal = nodes / 2, afterTotal = nodes - beforeTotal;
                    int beforeQuota = beforeTotal / renderers.Count + (i < beforeTotal % renderers.Count ? 1 : 0);
                    int afterQuota = afterTotal / renderers.Count + (i < afterTotal % renderers.Count ? 1 : 0);
                    MsaaSnapshot baseline = null;
                    var acquired = source.OpenAccessible(row.hwnd, ObjectIds.Client);
                    check.baseline_hresult_category = VariantReader.Hresult(acquired.Hresult);
                    if (acquired.Node != null) using (var node = acquired.Node) {
                        try {
                            int state = node.State();
                            if (state < 0 || (state & 0x20000000) != 0) { check.send_status = "protected"; continue; }
                            if (beforeQuota > 0) { baseline = new Collector(desktop, 1, beforeQuota, depth, names, expired).Snapshot(node, row.hwnd); Root(baseline, check, true); }
                        } catch { check.send_status = "unknown_protection"; result.Unreadable++; continue; }
                    }
                    if (baseline == null) {
                        if (beforeQuota == 0) { result.Truncated = true; result.Note("msaa_node_limit"); }
                        else { result.Unreadable++; result.Note("msaa_unavailable"); }
                    }
                    string shape = baseline == null ? null : baseline.Shape();
                    if (baseline != null) result.Merge(baseline, true);
                    if (!Scope(desktop, row, pid) || expired()) { check.send_status = "scope_rejected"; continue; }
                    long started = api.Milliseconds;
                    AxSend sent;
                    try { sent = api.HoneyPot(row.hwnd, pid, 250); }
                    catch { sent = new AxSend { attempted = true, status = "failed" }; }
                    check.attempted = sent.attempted; check.send_status = sent.status; result.Performed |= sent.attempted;
                    if (sent.status != "completed") { if (sent.attempted) { result.Unreadable++; result.Note("activation_failed"); } check.activation_elapsed_ms = api.Milliseconds - started; continue; }
                    IMsaaNode final = null;
                    try {
                        while (!expired() && api.Milliseconds - started <= 2000 && check.poll_attempts < 21) {
                            if (!Scope(desktop, row, pid)) { check.send_status = "renderer_disappeared"; result.Unreadable++; break; }
                            if (final != null) { final.Dispose(); final = null; }
                            var next = source.OpenAccessible(row.hwnd, ObjectIds.Client); check.poll_attempts++;
                            check.after_hresult_category = VariantReader.Hresult(next.Hresult); final = next.Node;
                            if (final != null) {
                                int state = final.State();
                                if (state < 0 || (state & 0x20000000) != 0) { check.send_status = "protected"; break; }
                                int role = final.Role(), count = final.ChildCount();
                                if (role < 0 || count < 0) throw new InvalidOperationException();
                                if (role != check.baseline_role || count != check.baseline_child_count || (role == 15 && count > 0)) break;
                            }
                            if (api.Milliseconds - started >= 2000) break;
                            api.Pause(100);
                        }
                        if (final != null && check.send_status == "completed" && Scope(desktop, row, pid) && !expired() && afterQuota > 0) {
                            var after = new Collector(desktop, 1, afterQuota, depth, names, expired).Snapshot(final, row.hwnd);
                            Root(after, check, false); string afterShape = after.Shape();
                            check.comparison_complete = baseline != null && baseline.Complete && after.Complete;
                            if (check.comparison_complete) check.tree_changed = shape != afterShape;
                            result.Merge(after, false);
                            if ((check.after_role == 15 || check.after_role == 14) && Scope(desktop, row, pid) && !expired()) {
                                int state = final.State(); if (state >= 0 && (state & 0x20000000) == 0 && Scope(desktop, row, pid) && !expired()) {
                                    var capability = final as IIa2CapabilityNode; if (capability != null) check.ia2 = capability.Capability();
                                }
                            }
                        } else if (afterQuota == 0 || expired()) { result.Truncated = true; result.Note(expired() ? "timeout" : "msaa_node_limit"); }
                        else if (final == null && check.send_status == "completed") { result.Unreadable++; result.Note("msaa_unavailable"); }
                    } catch { result.Unreadable++; result.Note("msaa_unreadable"); }
                    finally { if (final != null) try { final.Dispose(); } catch { result.Unreadable++; } }
                    check.activation_elapsed_ms = api.Milliseconds - started;
                }
                result.Focus = watchSeconds == 0 ? new FocusEvidence() : api.Watch(watchSeconds, pid, desktop, windows, names, expired);
                if (result.Focus.status != "disabled" && result.Focus.status != "complete") { result.Unreadable++; result.Note("focus_watch_incomplete"); }
            }
            return result;
        }
        static int Rank(WindowRow row, WindowArchitecture architecture) { return !row.visible ? 2 : row.root_hwnd == architecture.primary_main_window ? 0 : 1; }
        static void Root(MsaaSnapshot snapshot, AxCheck check, bool before) {
            if (snapshot.Nodes.Count == 0) return; var root = snapshot.Nodes[0];
            if (before) { check.baseline_role = root.role; check.baseline_child_count = root.child_count; }
            else { check.after_role = root.role; check.after_child_count = root.child_count; }
        }
    }
    public sealed class GuiEvidence {
        public long? hwndActive, hwndFocus, hwndCaret;
        public int[] rcCaret;
        public uint? flags;
        internal void Filter(IReadOnlyDesktop desktop, int pid) {
            hwndActive = Owned(desktop, pid, hwndActive); hwndFocus = Owned(desktop, pid, hwndFocus); hwndCaret = Owned(desktop, pid, hwndCaret);
            if (!hwndCaret.HasValue) rcCaret = null;
        }
        static long? Owned(IReadOnlyDesktop desktop, int pid, long? hwnd) { try { return hwnd.HasValue && hwnd.Value > 0 && desktop.WindowProcess(hwnd.Value) == pid ? hwnd : null; } catch { return null; } }
    }
    public sealed class FocusRow {
        public long hwnd, root_hwnd; public int process_id;
        public string id_object_category, id_child_kind, accessible_kind = "unavailable", hresult_category, status = "unavailable";
        public bool related_secondary_window, related_secondary_renderer, accessible_descendant;
        public int? role, state, child_count; public int[] bounds; public string name;
        public GuiEvidence gui = new GuiEvidence();
    }
    public sealed class FocusEvidence {
        public int requested_seconds, dropped;
        public string status = "disabled", hook_mode = "out_of_context";
        public List<FocusRow> events = new List<FocusRow>();
    }
    public sealed class FocusObject { internal IMsaaNode Node; internal int Hresult; internal string Kind = "unavailable"; }
    public interface IFocusApi {
        long Root(long hwnd);
        bool Descendant(long parent, long hwnd);
        FocusObject EventObject(long hwnd, int objectId, int childId);
        GuiEvidence Gui(int pid);
    }
    internal static class FocusCollector {
        internal static void Observe(IFocusApi api, FocusEvidence evidence, IReadOnlyDesktop desktop, List<WindowRow> windows, int pid, bool names, long hwnd, int objectId, int childId, Func<bool> expired) {
            if (expired()) return;
            if (evidence.events.Count >= 64) { evidence.dropped++; return; }
            FocusRow observed = null;
            try {
                if (desktop.WindowProcess(hwnd) != pid) return;
                long root = api.Root(hwnd);
                if (!windows.Exists(delegate(WindowRow w) { return w.depth == 0 && w.hwnd == root && w.process_id == pid; }) || desktop.WindowProcess(root) != pid) return;
                var architecture = WindowArchitecture.Describe(windows, pid);
                var row = new FocusRow { hwnd = hwnd, root_hwnd = root, process_id = pid,
                    id_child_kind = childId == 0 ? "zero" : childId > 0 ? "positive" : "negative",
                    id_object_category = objectId == -4 ? "objid_client" : objectId == 0 ? "objid_window" : NativeAccessible.ReservedObjectId(objectId) ? "reserved_system_objid" : objectId > 0 ? "custom_positive" : "other_negative",
                    related_secondary_window = architecture.secondary_visible_chromium_windows.Contains(root) };
                foreach (long renderer in architecture.secondary_renderer_hwnds) if (renderer == hwnd || api.Descendant(renderer, hwnd)) row.related_secondary_renderer = true;
                evidence.events.Add(row); observed = row;
                var acquired = api.EventObject(hwnd, objectId, childId); row.hresult_category = VariantReader.Hresult(acquired.Hresult); row.accessible_kind = acquired.Kind;
                if (acquired.Node != null) using (var node = acquired.Node) {
                    if (desktop.WindowProcess(hwnd) != pid || expired()) { row.status = "out_of_scope"; return; }
                    int state = node.State(); if (state < 0) throw new InvalidOperationException(); row.state = state;
                    if ((state & 0x20000000) != 0) row.status = "protected";
                    else {
                        int role = node.Role(), count = node.ChildCount();
                        if (role < 0 || count < 0) throw new InvalidOperationException();
                        row.role = role; row.child_count = count; row.bounds = node.Location();
                        if (names) row.name = Collector.Short(node.Name());
                        row.status = "available"; row.accessible_descendant = row.role != 9 && row.role != 10 && (childId != 0 || row.role == 42 || row.role == 41);
                    }
                }
                row.gui = api.Gui(pid); row.gui.Filter(desktop, pid);
            } catch { if (observed != null) { observed.status = "read_failed"; observed.accessible_descendant = false; } }
        }
    }
    sealed class NativeAxApi : IAxApi, IFocusApi {
        readonly NativeDesktop desktop; readonly Stopwatch timer = Stopwatch.StartNew();
        internal NativeAxApi(NativeDesktop value) { desktop = value; }
        public long Milliseconds { get { return timer.ElapsedMilliseconds; } }
        public void Pause(int milliseconds) { Thread.Sleep(milliseconds); }
        // Chromium browser test: WM_GETOBJECT, WPARAM OBJID_CLIENT, LPARAM honey-pot 1.
        // Current LegacyRenderWidgetHostHWND::OnGetObject recognizes low 32-bit object ID 1.
        [DllImport("user32.dll", SetLastError = true, CharSet = CharSet.Unicode)] static extern IntPtr SendMessageTimeout(IntPtr hwnd, uint message, IntPtr wparam, IntPtr lparam, uint flags, uint timeout, out UIntPtr result);
        public AxSend HoneyPot(long hwnd, int pid, int timeoutMs) {
            if (timeoutMs != 250 || desktop.WindowProcess(hwnd) != pid || desktop.Window(hwnd, false).class_name != "Chrome_RenderWidgetHostHWND") return new AxSend();
            UIntPtr ignored;
            IntPtr success = SendMessageTimeout(new IntPtr(hwnd), 0x3D, new IntPtr(-4), new IntPtr(1), 0x0002 | 0x0020, 250, out ignored); // ABORTIFHUNG | ERRORONEXIT; never NOTIMEOUTIFNOTHUNG.
            return new AxSend { attempted = true, status = success != IntPtr.Zero ? "completed" : Marshal.GetLastWin32Error() == 1460 ? "timeout" : "failed" };
        }
        delegate void WinEventProc(IntPtr hook, uint eventId, IntPtr hwnd, int objectId, int childId, uint thread, uint time);
        [DllImport("user32.dll")] static extern IntPtr SetWinEventHook(uint min, uint max, IntPtr module, WinEventProc callback, uint process, uint thread, uint flags);
        [DllImport("user32.dll")] static extern bool UnhookWinEvent(IntPtr hook);
        [StructLayout(LayoutKind.Sequential)] struct Point { public int x, y; }
        [StructLayout(LayoutKind.Sequential)] struct Message { public IntPtr hwnd; public uint message; public UIntPtr wparam; public IntPtr lparam; public uint time; public Point point; public uint reserved; }
        [DllImport("user32.dll")] static extern bool PeekMessage(out Message message, IntPtr hwnd, uint min, uint max, uint remove);
        [DllImport("user32.dll")] static extern IntPtr DispatchMessage(ref Message message);
        [DllImport("user32.dll")] static extern IntPtr GetAncestor(IntPtr hwnd, uint flags);
        [DllImport("user32.dll")] static extern bool IsChild(IntPtr parent, IntPtr hwnd);
        [DllImport("user32.dll")] static extern IntPtr GetForegroundWindow();
        [DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(IntPtr hwnd, out uint pid);
        [StructLayout(LayoutKind.Sequential)] struct Rect { public int left, top, right, bottom; }
        [StructLayout(LayoutKind.Sequential)] struct GuiInfo { public uint size, flags; public IntPtr active, focus, capture, menuOwner, moveSize, caret; public Rect caretRect; }
        [DllImport("user32.dll")] static extern bool GetGUIThreadInfo(uint thread, ref GuiInfo info);
        [DllImport("oleacc.dll")] static extern int AccessibleObjectFromEvent(IntPtr hwnd, uint objectId, uint childId, [MarshalAs(UnmanagedType.Interface)] out IAccessible accessible, [MarshalAs(UnmanagedType.Struct)] out object child);
        public long Root(long hwnd) { return GetAncestor(new IntPtr(hwnd), 2).ToInt64(); }
        public bool Descendant(long parent, long hwnd) { return IsChild(new IntPtr(parent), new IntPtr(hwnd)); }
        public GuiEvidence Gui(int pid) {
            var result = new GuiEvidence(); IntPtr foreground = GetForegroundWindow();
            if (foreground == IntPtr.Zero || desktop.WindowProcess(foreground.ToInt64()) != pid) return result;
            uint owner; uint thread = GetWindowThreadProcessId(foreground, out owner); if (owner != pid) return result;
            var info = new GuiInfo { size = (uint)Marshal.SizeOf(typeof(GuiInfo)) };
            if (GetGUIThreadInfo(thread, ref info)) { result.hwndActive = Handle(info.active); result.hwndFocus = Handle(info.focus); result.hwndCaret = Handle(info.caret);
                result.flags = info.flags & 31; result.rcCaret = new int[] {info.caretRect.left, info.caretRect.top, info.caretRect.right, info.caretRect.bottom}; }
            return result;
        }
        static long? Handle(IntPtr hwnd) { return hwnd == IntPtr.Zero ? null : (long?)hwnd.ToInt64(); }
        public FocusObject EventObject(long hwnd, int objectId, int childId) {
            IAccessible accessible = null; IAccessibleReads reads = null; var result = new FocusObject();
            try {
                object child; result.Hresult = AccessibleObjectFromEvent(new IntPtr(hwnd), unchecked((uint)objectId), unchecked((uint)childId), out accessible, out child);
                if (result.Hresult != 0 || accessible == null || !(child is int)) return result;
                reads = new NativeComReads(accessible); accessible = null; int id = (int)child;
                if (id != 0) {
                    var resolved = reads.ChildObject(id);
                    if (resolved != null) { reads.Dispose(); reads = resolved; id = 0; }
                    else if (id < 0) { result.Hresult = 1; return result; }
                }
                result.Kind = id == 0 ? "accessible_object" : "simple_child";
                result.Node = new NativeAccessible(reads, id, true); reads = null;
            } catch (Exception e) { result.Hresult = e.HResult; }
            finally { if (reads != null) reads.Dispose(); if (accessible != null && Marshal.IsComObject(accessible)) Marshal.ReleaseComObject(accessible); }
            return result;
        }
        public FocusEvidence Watch(int seconds, int pid, IReadOnlyDesktop source, List<WindowRow> windows, bool names, Func<bool> expired) {
            var result = new FocusEvidence { requested_seconds = seconds, status = "hook_failed" }; bool handling = false;
            if (expired()) { result.status = "deadline"; return result; }
            long until = Milliseconds + seconds * 1000;
            WinEventProc callback = delegate(IntPtr hook, uint eventId, IntPtr hwnd, int objectId, int childId, uint thread, uint time) {
                if (handling) { result.dropped++; return; }
                if (eventId != 0x8005 || hwnd == IntPtr.Zero || Milliseconds >= until || expired()) return;
                handling = true;
                try { FocusCollector.Observe(this, result, source, windows, pid, names, hwnd.ToInt64(), objectId, childId, expired); }
                finally { handling = false; }
            };
            IntPtr owned = SetWinEventHook(0x8005, 0x8005, IntPtr.Zero, callback, (uint)pid, 0, 0); // WINEVENT_OUTOFCONTEXT=0; no DLL/module.
            if (owned == IntPtr.Zero) return result;
            try {
                Console.Error.WriteLine("LIVE_STUDIO_FOCUS_WATCH_READY");
                result.status = "complete";
                while (Milliseconds < until && !expired()) { Message message; int drained = 0;
                    while (drained++ < 64 && PeekMessage(out message, IntPtr.Zero, 0, 0, 1)) DispatchMessage(ref message);
                    Pause(20);
                }
                if (expired()) result.status = "deadline";
            } finally { if (!UnhookWinEvent(owned)) result.status = "unhook_failed"; GC.KeepAlive(callback); }
            return result;
        }
        public void Dispose() { }
    }
}
