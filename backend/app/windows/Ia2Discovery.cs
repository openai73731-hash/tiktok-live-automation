// PC-SEND-1E capability only. No IAccessible2 vtable, metadata or action methods.
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.Text;
using Accessibility;

namespace LiveStudioDiscovery {
    // Microsoft COM IServiceProvider: IUnknown, single preserved HRESULT method.
    // https://learn.microsoft.com/en-us/dotnet/api/microsoft.visualstudio.ole.interop.iserviceprovider
    // https://learn.microsoft.com/en-us/windows/win32/api/servprov/nf-servprov-iserviceprovider-queryservice(refguid_refiid_void)
    [ComImport, Guid("6D5140C1-7436-11CE-8034-00AA006009FA"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface INativeServiceProvider {
        [PreserveSig] int QueryService(ref Guid service, ref Guid iid, out IntPtr result);
    }
    interface IIa2Com : IDisposable {
        int QueryProvider(Guid iid);
        int QueryService(Guid service, Guid iid, out bool available);
    }
    public sealed class Ia2Capability {
        public bool iserviceprovider_available, query_service_attempted, ia2_available;
        public string provider_hresult_category, query_hresult_category, status = "capability_failed";
    }
    interface IIa2Reads { Ia2Capability Capability(); }
    interface IIa2CapabilityNode { Ia2Capability Capability(); }
    static class Ia2Contract {
        // Chromium third_party/iaccessible2/ia2_api_all.idl, blob c8b3f354d96d8e5317428ed6dab2b8fd3082aee9:
        // https://chromium.googlesource.com/chromium/src/+/refs/heads/main/third_party/iaccessible2/ia2_api_all.idl
        // _changingInterfaces requires IServiceProvider and service IID_IAccessible, not QI(IA2).
        internal static readonly Guid ProviderId = new Guid("6D5140C1-7436-11CE-8034-00AA006009FA");
        internal static readonly Guid ServiceId = new Guid("618736E0-3C3D-11CF-810C-00AA00389B71");
        internal static readonly Guid InterfaceId = new Guid("E89F726E-C4F4-4C19-BB19-B647D7FA8478");
        internal static Ia2Capability Check(IIa2Com api) {
            var result = new Ia2Capability();
            try {
                int hr = api.QueryProvider(ProviderId);
                result.provider_hresult_category = VariantReader.Hresult(hr);
                result.iserviceprovider_available = hr == 0;
                if (hr != 0) result.status = "provider_unavailable";
                else {
                    result.query_service_attempted = true;
                    bool available; hr = api.QueryService(ServiceId, InterfaceId, out available);
                    result.query_hresult_category = VariantReader.Hresult(hr);
                    result.ia2_available = hr == 0 && available;
                    result.status = result.ia2_available ? "supported" : "ia2_unavailable";
                }
            } catch (Exception e) {
                if (result.query_service_attempted) result.query_hresult_category = VariantReader.Hresult(e.HResult);
                else result.provider_hresult_category = VariantReader.Hresult(e.HResult);
                result.status = "capability_failed";
            } finally {
                try { api.Dispose(); } catch { result.status = "capability_failed"; result.ia2_available = false; }
            }
            return result;
        }
    }
    sealed class NativeIa2Com : IIa2Com {
        readonly IAccessible accessible; INativeServiceProvider provider;
        internal NativeIa2Com(IAccessible borrowed) { accessible = borrowed; }
        public int QueryProvider(Guid iid) {
            IntPtr unknown = IntPtr.Zero, service = IntPtr.Zero; object rcw = null;
            try {
                unknown = Marshal.GetIUnknownForObject(accessible);
                int hr = Marshal.QueryInterface(unknown, ref iid, out service);
                if (hr != 0) return hr;
                if (service == IntPtr.Zero) return unchecked((int)0x80004005);
                rcw = Marshal.GetObjectForIUnknown(service);
                provider = rcw as INativeServiceProvider;
                if (provider == null) return unchecked((int)0x80004002);
                rcw = null; return 0;
            } finally {
                try { if (rcw != null && Marshal.IsComObject(rcw)) Marshal.ReleaseComObject(rcw); }
                finally { try { if (service != IntPtr.Zero) Marshal.Release(service); }
                    finally { if (unknown != IntPtr.Zero) Marshal.Release(unknown); } }
            }
        }
        public int QueryService(Guid service, Guid iid, out bool available) {
            available = false; IntPtr ia2 = IntPtr.Zero;
            try {
                int hr = provider.QueryService(ref service, ref iid, out ia2);
                available = hr == 0 && ia2 != IntPtr.Zero;
                return hr;
            } finally { if (ia2 != IntPtr.Zero) Marshal.Release(ia2); }
        }
        public void Dispose() {
            var owned = provider; provider = null;
            if (owned != null && Marshal.IsComObject(owned)) Marshal.ReleaseComObject(owned);
        }
    }
    // Keep the exact root RCW alive across both snapshots and QueryService. Child
    // wrappers still have their normal owned lifetime inside the existing serializer.
    sealed class BorrowedMsaaNode : IMsaaNode {
        readonly IMsaaNode node;
        internal BorrowedMsaaNode(IMsaaNode value) { node = value; }
        public string Identity { get { return node.Identity; } }
        public bool SimpleChild { get { return node.SimpleChild; } }
        public int? ChildId { get { return node.ChildId; } }
        public int State() { return node.State(); } public int Role() { return node.Role(); }
        public int ChildCount() { return node.ChildCount(); } public string Name() { return node.Name(); }
        public int[] Location() { return node.Location(); } public long? OwnerWindow() { return node.OwnerWindow(); }
        public IEnumerable<ChildRead> Children(int count) { return node.Children(count); }
        public IMsaaNode ChildObject() { return node.ChildObject(); }
        public void Dispose() { }
    }
    internal sealed class MsaaSnapshot {
        internal List<MsaaRow> Nodes = new List<MsaaRow>();
        internal List<ChildDiagnostic> Diagnostics = new List<ChildDiagnostic>();
        internal List<string> Errors = new List<string>();
        internal int Attempts, Dropped, Unreadable;
        internal bool Complete, Truncated;
        internal Dictionary<string, object> Section() { return new Dictionary<string, object> {
            {"nodes", Nodes}, {"child_diagnostics", Diagnostics}, {"diagnostics_dropped", Dropped} }; }
        internal string Shape() {
            var text = new StringBuilder();
            foreach (var n in Nodes) text.Append(n.parent_id).Append('/').Append(n.depth).Append('/').Append(n.child_id)
                .Append('/').Append(n.owner_kind).Append('/').Append(n.role).Append('/').Append(n.state).Append('/').Append(n.child_count).Append(';');
            return text.ToString(); // Internal only; no names, text or COM identities.
        }
        internal void Merge(MsaaSnapshot snapshot, int diagnosticLimit = 64) {
            int offset = Attempts;
            foreach (var n in snapshot.Nodes) { n.node_id += offset; if (n.parent_id.HasValue) n.parent_id += offset; Nodes.Add(n); }
            foreach (var d in snapshot.Diagnostics) {
                if (d.parent_id.HasValue) d.parent_id += offset;
                if (Diagnostics.Count < diagnosticLimit) Diagnostics.Add(d); else Dropped++;
            }
            Attempts += snapshot.Attempts; Dropped += snapshot.Dropped;
            Truncated |= snapshot.Truncated; Unreadable += snapshot.Unreadable;
            foreach (string code in snapshot.Errors) if (Errors.Count < 16 && !Errors.Contains(code)) Errors.Add(code);
        }
    }
    public sealed class Ia2Check {
        public int check_id, source_process_id;
        public long source_hwnd, root_hwnd;
        public long? owner_hwnd; public int? process_id;
        public bool source_visible, visible_root, comparison_complete;
        public string chromium_kind;
        public Ia2Capability capability = new Ia2Capability();
        public int? before_msaa_nodes, after_msaa_nodes, before_child_count, after_child_count;
        public bool? tree_changed_after_ia2_query;
    }
    internal sealed class Ia2Evidence {
        internal readonly List<Ia2Check> Checks = new List<Ia2Check>();
        internal readonly MsaaSnapshot Before = new MsaaSnapshot(), After = new MsaaSnapshot();
        internal readonly List<string> Errors = new List<string>();
        internal bool Truncated; internal int Unreadable;
        internal long? PrimaryRoot;
        internal Dictionary<string, object> Section() {
            int success = 0, failure = 0; bool supported = false, checkedRenderer = false, changed = false, complete = true, compared = false;
            foreach (var c in Checks) {
                complete &= c.comparison_complete;
                if (c.capability.query_service_attempted) { if (c.capability.ia2_available) success++; else failure++; }
                if (c.root_hwnd == PrimaryRoot && c.visible_root && c.source_visible && c.chromium_kind == "render_widget") {
                    checkedRenderer |= c.capability.provider_hresult_category != null;
                    supported |= c.capability.ia2_available;
                }
                if (c.capability.query_service_attempted) {
                    compared = true;
                    changed |= c.tree_changed_after_ia2_query == true;
                }
            }
            return new Dictionary<string, object> {{"primary_root_hwnd", PrimaryRoot}, {"capability_checks", Checks}, {"query_success_count", success}, {"query_failure_count", failure},
                {"visible_renderer_supported", supported}, {"visible_renderer_checked", checkedRenderer},
                {"before_msaa_nodes", Before.Nodes.Count}, {"after_msaa_nodes", After.Nodes.Count},
                {"tree_changed_after_ia2_query", compared && complete ? (bool?)changed : null},
                {"comparison_complete", compared && complete}, {"before_msaa", Before.Section()}};
        }
    }
    internal static class Ia2Experiment {
        sealed class Work {
            internal WindowRow Window; internal IMsaaNode Node; internal Ia2Check Check;
            internal string BeforeShape; internal bool BeforeComplete;
            internal int BeforeQuota, AfterQuota;
        }
        static int Priority(WindowRow w, Dictionary<long, bool> visible, long? primary) {
            bool rootVisible; visible.TryGetValue(w.root_hwnd, out rootVisible);
            if (w.hwnd == primary) return 0;
            if (w.root_hwnd == primary && w.chromium_kind == "render_widget") return 1;
            return rootVisible ? 2 : 3;
        }
        static bool InScope(IReadOnlyDesktop api, Work item, int pid) {
            try { return api.WindowProcess(item.Window.hwnd) == item.Window.process_id && api.WindowProcess(item.Window.root_hwnd) == pid; }
            catch { return false; }
        }
        internal static Ia2Evidence Run(IReadOnlyDesktop api, List<WindowRow> windows, int pid, int maxNodes, int maxDepth, bool names, Func<bool> expired) {
            var result = new Ia2Evidence(); var visible = new Dictionary<long, bool>();
            foreach (var w in windows) if (w.depth == 0) visible[w.hwnd] = w.visible;
            var primary = windows.Find(delegate(WindowRow w) { return w.depth == 0 && w.visible && w.process_id == pid && w.chromium_kind == "widget"; });
            result.PrimaryRoot = primary == null ? null : (long?)primary.hwnd;
            var candidates = windows.FindAll(delegate(WindowRow w) { return w.chromium_kind != null; });
            candidates.Sort(delegate(WindowRow a, WindowRow b) {
                int priority = Priority(a, visible, result.PrimaryRoot).CompareTo(Priority(b, visible, result.PrimaryRoot));
                return priority != 0 ? priority : a.hwnd.CompareTo(b.hwnd);
            });
            if (candidates.Count > 32) { candidates.RemoveRange(32, candidates.Count - 32); result.Truncated = true; result.Errors.Add("ia2_check_limit"); }
            var work = new List<Work>();
            try {
                foreach (var w in candidates) {
                    bool rootVisible; visible.TryGetValue(w.root_hwnd, out rootVisible);
                    var item = new Work { Window = w, Check = new Ia2Check { check_id = work.Count + 1, source_hwnd = w.hwnd,
                        root_hwnd = w.root_hwnd, source_process_id = w.process_id, source_visible = w.visible, visible_root = rootVisible, chromium_kind = w.chromium_kind } };
                    work.Add(item); result.Checks.Add(item.Check);
                    if (expired()) { item.Check.capability.status = "timeout"; result.Truncated = true; continue; }
                    try {
                        if (w.protected_content) { item.Check.capability.status = "protected"; continue; }
                        if (!InScope(api, item, pid)) { item.Check.capability.status = "out_of_scope"; result.Unreadable++; continue; }
                        item.Node = api.Accessible(w.hwnd);
                        int state = item.Node.State();
                        if (state < 0) throw new InvalidOperationException();
                        if ((state & 0x20000000) != 0) { item.Check.capability.status = "protected"; continue; }
                        item.Check.owner_hwnd = item.Node.OwnerWindow();
                        if (item.Check.owner_hwnd.HasValue) { int owner = api.WindowProcess(item.Check.owner_hwnd.Value); if (owner > 0) item.Check.process_id = owner; }
                        item.Check.capability.status = "pending";
                    } catch { item.Check.capability.status = "unknown_protection"; result.Unreadable++; }
                }
                // Reserve both phases before queries, with the visible renderer's pair
                // first for scarce node budgets. Capability priority remains widget/renderer.
                var snapshots = work.FindAll(delegate(Work w) { return w.Check.capability.status == "pending"; });
                snapshots.Sort(delegate(Work a, Work b) {
                    bool ar = a.Window.root_hwnd == result.PrimaryRoot && a.Window.chromium_kind == "render_widget";
                    bool br = b.Window.root_hwnd == result.PrimaryRoot && b.Window.chromium_kind == "render_widget";
                    return ar == br ? a.Check.check_id.CompareTo(b.Check.check_id) : br.CompareTo(ar);
                });
                for (int i = 0; i < snapshots.Count; i++) {
                    int beforeBudget = maxNodes / 2, afterBudget = maxNodes - beforeBudget;
                    var item = snapshots[i];
                    item.BeforeQuota = beforeBudget / snapshots.Count + (i < beforeBudget % snapshots.Count ? 1 : 0);
                    item.AfterQuota = afterBudget / snapshots.Count + (i < afterBudget % snapshots.Count ? 1 : 0);
                    Capture(api, item, result, pid, maxDepth, names, expired, true);
                }
                // All baseline trees precede the first QueryService; retain every exact
                // source object, and reread it once immediately after its capability query.
                foreach (var item in work) {
                    if (item.Check.capability.status != "pending") continue;
                    if (expired()) { item.Check.capability.status = "timeout"; result.Truncated = true; continue; }
                    try {
                        if (!InScope(api, item, pid)) { item.Check.capability.status = "out_of_scope"; result.Unreadable++; continue; }
                        // Recheck protection immediately before the query as well as in each serializer.
                        int state = item.Node.State();
                        if (state < 0) throw new InvalidOperationException();
                        if ((state & 0x20000000) != 0) { item.Check.capability.status = "protected"; continue; }
                        var reader = item.Node as IIa2CapabilityNode;
                        item.Check.capability = reader == null ? new Ia2Capability { status = "adapter_unavailable" } : reader.Capability();
                        if (item.Check.capability.status == "capability_failed" || item.Check.capability.status == "adapter_unavailable") result.Unreadable++;
                    } catch { item.Check.capability.status = "capability_failed"; result.Unreadable++; }
                    Capture(api, item, result, pid, maxDepth, names, expired, false);
                }
            } finally {
                foreach (var item in work) if (item.Node != null) try { item.Node.Dispose(); } catch { result.Unreadable++; }
            }
            result.Truncated |= result.Before.Truncated || result.After.Truncated;
            result.Unreadable += result.Before.Unreadable + result.After.Unreadable;
            foreach (var section in new MsaaSnapshot[] { result.Before, result.After }) foreach (string code in section.Errors)
                if (result.Errors.Count < 16 && !result.Errors.Contains(code)) result.Errors.Add(code);
            if (result.Checks.Exists(delegate(Ia2Check c) { return c.capability.status == "timeout"; }) && !result.Errors.Contains("timeout")) result.Errors.Add("timeout");
            return result;
        }
        static void Capture(IReadOnlyDesktop api, Work item, Ia2Evidence result, int pid, int depth, bool names, Func<bool> expired, bool before) {
            int quota = before ? item.BeforeQuota : item.AfterQuota;
            if (quota == 0 || expired()) { result.Truncated = true; if (!result.Errors.Contains(quota == 0 ? "msaa_node_limit" : "timeout")) result.Errors.Add(quota == 0 ? "msaa_node_limit" : "timeout"); return; }
            if (!InScope(api, item, pid)) {
                if (before) item.Check.capability.status = "out_of_scope";
                result.Unreadable++; if (!result.Errors.Contains("ia2_scope_changed")) result.Errors.Add("ia2_scope_changed"); return;
            }
            var snapshot = new Collector(api, 1, quota, depth, names, expired).Snapshot(item.Node, item.Window.hwnd);
            int? childCount = snapshot.Nodes.Count == 0 ? null : snapshot.Nodes[0].child_count;
            string shape = snapshot.Shape();
            if (before) {
                item.Check.before_msaa_nodes = snapshot.Nodes.Count; item.Check.before_child_count = childCount;
                item.BeforeShape = shape; item.BeforeComplete = snapshot.Complete; result.Before.Merge(snapshot);
            } else {
                item.Check.after_msaa_nodes = snapshot.Nodes.Count; item.Check.after_child_count = childCount;
                item.Check.comparison_complete = item.BeforeComplete && snapshot.Complete && item.Check.capability.query_service_attempted;
                if (item.Check.comparison_complete) item.Check.tree_changed_after_ia2_query = item.BeforeShape != shape;
                result.After.Merge(snapshot, 64 - result.Before.Diagnostics.Count);
            }
        }
    }
}
