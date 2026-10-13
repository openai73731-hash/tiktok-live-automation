// PC-SEND-1F: fixed standard object IDs only. No message, proxy creation or UI action.
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using Accessibility;

namespace LiveStudioDiscovery {
    static class ObjectIds {
        internal const int Window = 0, Client = -4;
        // https://learn.microsoft.com/en-us/windows/win32/winauto/object-identifiers
    }
    public interface IAccessibleObjectSource { ObjectAcquisition OpenAccessible(long hwnd, int objectId); }
    interface IObjectApi { int FromWindow(long hwnd, uint objectId, Guid iid, out IAccessibleReads value); }
    public sealed class ObjectAcquisition {
        internal int Hresult;
        internal IMsaaNode Node;
        internal static ObjectAcquisition Read(IObjectApi api, long hwnd, int objectId) {
            if (objectId != ObjectIds.Client && objectId != ObjectIds.Window) throw new ArgumentException("invalid_argument");
            IAccessibleReads reads = null;
            var result = new ObjectAcquisition();
            try {
                result.Hresult = api.FromWindow(hwnd, unchecked((uint)objectId), NativeDesktop.AccessibleId, out reads);
                if (result.Hresult == 0 && reads != null) { result.Node = new NativeAccessible(reads, 0, true); reads = null; }
            } catch (Exception e) { result.Hresult = e.HResult; }
            finally { if (reads != null) try { reads.Dispose(); } catch { result.Hresult = unchecked((int)0x80004005); } } // Includes non-null retval on failure.
            return result;
        }
    }
    sealed class NativeObjectApi : IObjectApi {
        // https://learn.microsoft.com/en-us/windows/win32/api/oleacc/nf-oleacc-accessibleobjectfromwindow
        [DllImport("oleacc.dll")] static extern int AccessibleObjectFromWindow(IntPtr hwnd, uint objectId, ref Guid iid, [MarshalAs(UnmanagedType.Interface)] out IAccessible value);
        public int FromWindow(long hwnd, uint objectId, Guid iid, out IAccessibleReads value) {
            value = null; IAccessible accessible = null;
            try {
                int hr = AccessibleObjectFromWindow(new IntPtr(hwnd), objectId, ref iid, out accessible);
                if (accessible != null) { value = new NativeComReads(accessible); accessible = null; }
                return hr;
            } finally { if (accessible != null && Marshal.IsComObject(accessible)) Marshal.ReleaseComObject(accessible); }
        }
    }
    // Record the actual CLIENT acquisition without substituting another object on failure.
    sealed class ClientDesktop : IReadOnlyDesktop {
        readonly IReadOnlyDesktop desktop;
        internal readonly Dictionary<long, ObjectAcquisition> Acquired = new Dictionary<long, ObjectAcquisition>();
        internal ClientDesktop(IReadOnlyDesktop value) { desktop = value; }
        public IMsaaNode Accessible(long hwnd) {
            var source = desktop as IAccessibleObjectSource;
            if (source == null) throw new InvalidOperationException();
            var result = source.OpenAccessible(hwnd, ObjectIds.Client); Acquired[hwnd] = result;
            if (result.Node == null) throw new COMException(null, result.Hresult);
            return result.Node;
        }
        public ScanLimits Limits { get { return desktop.Limits; } }
        public RootScanStats RootStats { get { return desktop.RootStats; } }
        public IEnumerable<long> TopWindows(int pid) { return desktop.TopWindows(pid); }
        public IEnumerable<long> ChildWindows(long hwnd) { return desktop.ChildWindows(hwnd); }
        public WindowRow Window(long hwnd, bool names) { return desktop.Window(hwnd, names); }
        public IEnumerable<ProcessRow> Processes() { return desktop.Processes(); }
        public string ImagePath(int pid) { return desktop.ImagePath(pid); }
        public string ProductVersion(string path) { return desktop.ProductVersion(path); }
        public int WindowProcess(long hwnd) { return desktop.WindowProcess(hwnd); }
    }
    public sealed class ObjectObservation {
        public string source_kind, status = "not_attempted", hresult_category, metadata_phase;
        public int object_id;
        public bool available, query_service_attempted;
        public int? role, state, child_count;
        public int[] bounds;
        public string name, provider_hresult_category, query_hresult_category, ia2_query_status = "not_queried";
        public bool? iserviceprovider_available, ia2_available;
        internal ObjectObservation(string kind, int id) { source_kind = kind; object_id = id; }
        internal void Metadata(MsaaRow row, string phase) {
            if (row == null) return;
            role = row.role; state = row.state; child_count = row.child_count; bounds = row.bounds;
            name = row.name; metadata_phase = phase;
        }
        internal void Capability(Ia2Capability capability, bool client) {
            iserviceprovider_available = capability.provider_hresult_category == null ? null : (bool?)capability.iserviceprovider_available;
            provider_hresult_category = capability.provider_hresult_category;
            query_service_attempted = capability.query_service_attempted;
            query_hresult_category = capability.query_hresult_category;
            ia2_available = client && capability.query_service_attempted ? (bool?)capability.ia2_available : null;
            ia2_query_status = client ? capability.status : "not_queried_window";
        }
    }
    public sealed class ObjectPair {
        public long source_hwnd, root_hwnd;
        public int source_process_id;
        public bool source_visible, visible_root;
        public string chromium_kind;
        public ObjectObservation objid_client = new ObjectObservation("objid_client", ObjectIds.Client);
        public ObjectObservation objid_window = new ObjectObservation("objid_window", ObjectIds.Window) { ia2_query_status = "not_queried_window" };
    }
    internal sealed class ClientEvidence {
        internal Ia2Evidence Client;
        internal readonly MsaaSnapshot Window = new MsaaSnapshot();
        internal readonly List<ObjectPair> Pairs = new List<ObjectPair>();
        internal readonly List<string> Errors = new List<string>();
        internal bool Truncated; internal int Unreadable;
        internal Dictionary<string, object> Section() {
            bool available = false, supported = false; int nodes = 0;
            foreach (var pair in Pairs) if (pair.root_hwnd == Client.PrimaryRoot && pair.visible_root && pair.source_visible && pair.chromium_kind == "render_widget") {
                available |= pair.objid_client.available; supported |= pair.objid_client.ia2_available == true;
                foreach (var n in Client.After.Nodes) if (n.source_hwnd == pair.source_hwnd) nodes++;
            }
            return new Dictionary<string, object> {{"primary_root_hwnd", Client.PrimaryRoot}, {"checks", Pairs},
                {"window_msaa", Window.Section()}, {"visible_renderer_client_available", available},
                {"visible_renderer_client_nodes", nodes}, {"visible_renderer_client_ia2_supported", supported}};
        }
    }
    internal static class ClientExperiment {
        internal static ClientEvidence Run(IReadOnlyDesktop desktop, List<WindowRow> windows, int pid, int maxNodes, int depth, bool names, Func<bool> expired) {
            var result = new ClientEvidence(); var client = new ClientDesktop(desktop);
            int count = Math.Min(32, windows.FindAll(delegate(WindowRow w) { return w.chromium_kind != null; }).Count);
            int windowBudget = Math.Min(count, maxNodes / 4);
            // CLIENT gets >=75% of the joint visit budget, its before/after pair retained.
            result.Client = Ia2Experiment.Run(client, windows, pid, maxNodes - windowBudget, depth, names, expired);
            result.Truncated = result.Client.Truncated; result.Unreadable = result.Client.Unreadable;
            result.Errors.AddRange(result.Client.Errors);
            foreach (var check in result.Client.Checks) {
                var pair = new ObjectPair { source_hwnd = check.source_hwnd, root_hwnd = check.root_hwnd,
                    source_process_id = check.source_process_id, source_visible = check.source_visible,
                    visible_root = check.visible_root, chromium_kind = check.chromium_kind };
                result.Pairs.Add(pair);
                ObjectAcquisition acquired;
                if (client.Acquired.TryGetValue(check.source_hwnd, out acquired)) {
                    pair.objid_client.available = acquired.Node != null;
                    pair.objid_client.hresult_category = VariantReader.Hresult(acquired.Hresult);
                    pair.objid_client.status = pair.objid_client.available ? "available" : "unavailable";
                } else pair.objid_client.status = check.capability.status == "pending" ? "not_attempted" : check.capability.status;
                pair.objid_client.Capability(check.capability, true);
                var root = result.Client.Before.Nodes.Find(delegate(MsaaRow n) { return n.source_hwnd == check.source_hwnd && n.parent_id == null; });
                pair.objid_client.Metadata(root, "before");
                ReadWindow(desktop, pair, result, pid, depth, names, expired, windowBudget);
            }
            return result;
        }
        static bool Scope(IReadOnlyDesktop desktop, ObjectPair pair, int pid) {
            try { return desktop.WindowProcess(pair.source_hwnd) == pair.source_process_id && desktop.WindowProcess(pair.root_hwnd) == pid; }
            catch { return false; }
        }
        static void ReadWindow(IReadOnlyDesktop desktop, ObjectPair pair, ClientEvidence result, int pid, int depth, bool names, Func<bool> expired, int budget) {
            var observation = pair.objid_window;
            if (expired()) { observation.status = "timeout"; result.Truncated = true; Add(result, "timeout"); return; }
            if (!Scope(desktop, pair, pid)) { observation.status = "out_of_scope"; result.Unreadable++; Add(result, "ia2_scope_changed"); return; }
            // A protected HWND never acquires either source; a protected CLIENT object
            // also suppresses WINDOW probing rather than falling back around protection.
            if (pair.objid_client.ia2_query_status == "protected" || pair.objid_client.ia2_query_status == "unknown_protection" && pair.objid_client.available) {
                observation.status = "protected"; return;
            }
            var source = desktop as IAccessibleObjectSource;
            if (source == null) { observation.status = "adapter_unavailable"; result.Unreadable++; return; }
            IMsaaNode node = null;
            try {
                var acquired = source.OpenAccessible(pair.source_hwnd, ObjectIds.Window); node = acquired.Node;
                observation.available = node != null; observation.hresult_category = VariantReader.Hresult(acquired.Hresult);
                observation.status = observation.available ? "available" : "unavailable";
                if (node == null) return;
                if (expired()) { observation.status = "timeout"; result.Truncated = true; Add(result, "timeout"); return; }
                if (!Scope(desktop, pair, pid)) { observation.status = "out_of_scope"; result.Unreadable++; Add(result, "ia2_scope_changed"); return; }
                if (result.Window.Attempts >= budget) { result.Truncated = true; Add(result, "msaa_node_limit"); }
                else {
                    var snapshot = new Collector(desktop, 1, 1, depth, names, expired).Snapshot(node, pair.source_hwnd, true);
                    foreach (var row in snapshot.Nodes) { row.source_kind = "objid_window"; row.object_id = ObjectIds.Window; }
                    observation.Metadata(snapshot.Nodes.Count == 0 ? null : snapshot.Nodes[0], "window");
                    result.Window.Merge(snapshot, Math.Max(0, 64 - result.Client.Before.Diagnostics.Count - result.Client.After.Diagnostics.Count));
                    result.Truncated |= snapshot.Truncated; result.Unreadable += snapshot.Unreadable;
                    foreach (string code in snapshot.Errors) Add(result, code);
                    if (snapshot.Nodes.Count == 0) return;
                }
                if (expired()) { result.Truncated = true; Add(result, "timeout"); return; }
                if (!Scope(desktop, pair, pid)) { observation.status = "out_of_scope"; result.Unreadable++; Add(result, "ia2_scope_changed"); return; }
                int state = node.State();
                if (state < 0) throw new InvalidOperationException();
                if ((state & 0x20000000) != 0) return;
                if (expired()) { result.Truncated = true; Add(result, "timeout"); return; }
                if (!Scope(desktop, pair, pid)) { observation.status = "out_of_scope"; result.Unreadable++; Add(result, "ia2_scope_changed"); return; }
                var provider = node as IIa2ProviderNode;
                if (provider != null) observation.Capability(provider.ProviderCapability(), false);
            } catch { result.Unreadable++; Add(result, "msaa_unreadable"); }
            finally { if (node != null) try { node.Dispose(); } catch { result.Unreadable++; } }
        }
        static void Add(ClientEvidence result, string code) { if (result.Errors.Count < 16 && !result.Errors.Contains(code)) result.Errors.Add(code); }
    }
}
