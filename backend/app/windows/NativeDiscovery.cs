// Compiled in the isolated PowerShell worker. Only stock Win32/MSAA reads.
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Text;
using Accessibility;

namespace LiveStudioDiscovery {
    public sealed class WindowRow {
        public long hwnd, root_hwnd;
        public long? parent_hwnd;
        public int depth, process_id;
        public string class_name, name, chromium_kind;
        public bool visible, enabled, protected_content;
        public int[] bounds;
    }
    public sealed class ProcessRow {
        public int process_id, parent_process_id;
        public string filename, executable_path;
        public bool has_windows;
        public List<long> hwnds = new List<long>();
    }
    public sealed class MsaaRow {
        public int node_id, depth, state;
        public int? parent_id, process_id, role, child_count, child_id;
        public long source_hwnd;
        public long? owner_hwnd;
        public string name, semantic_hint, owner_kind;
        public bool focusable, focused, disabled, invisible, offscreen, protected_content;
        public int[] bounds;
    }
    public interface IMsaaNode : IDisposable {
        string Identity { get; }
        bool SimpleChild { get; }
        int? ChildId { get; }
        int State(); int Role(); int ChildCount();
        string Name(); int[] Location(); long? OwnerWindow();
        IEnumerable<IMsaaNode> Children(int count);
        IMsaaNode ChildObject();
    }
    [Flags] public enum ScanLimits { None = 0, Desktop = 1, TargetRoots = 2, TargetChildren = 4, Processes = 8 }
    public interface IReadOnlyDesktop {
        ScanLimits Limits { get; }
        IEnumerable<long> TopWindows(int processId);
        IEnumerable<long> ChildWindows(long hwnd);
        WindowRow Window(long hwnd, bool includeNames);
        IEnumerable<ProcessRow> Processes();
        string ImagePath(int processId); string ProductVersion(string path);
        IMsaaNode Accessible(long hwnd);
        int WindowProcess(long hwnd);
    }
    public sealed class Collector {
        readonly IReadOnlyDesktop api;
        readonly int maxHwnd, maxNodes, maxDepth;
        readonly bool names;
        int hwndAttempts, msaaAttempts, unreadable;
        bool truncated;
        readonly List<string> errors = new List<string>();
        readonly List<WindowRow> windows = new List<WindowRow>();
        readonly List<MsaaRow> nodes = new List<MsaaRow>();
        readonly HashSet<long> hwndSeen = new HashSet<long>();
        public Collector(IReadOnlyDesktop desktop, int hwndLimit, int nodeLimit, int depthLimit, bool includeNames) {
            if (hwndLimit < 1 || hwndLimit > 2000 || nodeLimit < 1 || nodeLimit > 2000 || depthLimit < 1 || depthLimit > 20)
                throw new ArgumentException("invalid_argument");
            api = desktop; maxHwnd = hwndLimit; maxNodes = nodeLimit; maxDepth = depthLimit; names = includeNames;
        }
        public static string Short(string text, int limit = 160) {
            if (text == null) return null;
            var result = new StringBuilder();
            foreach (char c in text) { if (result.Length == limit) break; result.Append(char.IsControl(c) ? ' ' : c); }
            return result.ToString();
        }
        void Note(string code, bool bad = true, bool cut = false) {
            if (bad) unreadable++;
            if (cut) truncated = true;
            if (errors.Count < 16 && !errors.Contains(code)) errors.Add(code);
        }
        void Hwnd(long handle, long root, int depth) {
            if (hwndAttempts >= maxHwnd) { Note("hwnd_limit", false, true); return; }
            hwndAttempts++;
            if (!hwndSeen.Add(handle)) { Note("hwnd_repeated", false, true); return; }
            try {
                WindowRow row = api.Window(handle, names);
                if (row.hwnd != handle || row.process_id <= 0) throw new InvalidOperationException();
                row.root_hwnd = root; row.depth = depth;
                row.name = names && !row.protected_content ? Short(row.name) : null;
                row.class_name = Short(row.class_name);
                row.chromium_kind = row.class_name == "Chrome_WidgetWin_1" ? "widget" :
                    row.class_name == "Chrome_RenderWidgetHostHWND" ? "render_widget" :
                    (row.class_name != null && (row.class_name.StartsWith("Chrome_") || row.class_name.StartsWith("Cef"))) ? "other" : null;
                windows.Add(row);
                if (row.protected_content) return;
                foreach (long child in api.ChildWindows(handle)) {
                    if (depth >= maxDepth) { Note("hwnd_depth_limit", false, true); break; }
                    if (hwndAttempts >= maxHwnd) { Note("hwnd_limit", false, true); break; }
                    Hwnd(child, root, depth + 1);
                }
            } catch { Note("hwnd_unreadable"); }
        }
        static string Hint(int? role) {
            if (!role.HasValue) return "unknown";
            switch (role) {
                case 42: return "edit"; case 41: return "text"; case 43: return "button";
                case 15: return "document"; case 33: return "list"; case 34: return "listitem";
                case 16: return "pane"; default: return "other";
            }
        }
        void Msaa(IMsaaNode node, int? parent, int depth, long source, HashSet<string> seen) {
            using (node) {
                if (msaaAttempts >= maxNodes) { Note("msaa_node_limit", false, true); return; }
                msaaAttempts++;
                try {
                    if (!seen.Add(node.Identity)) { Note("msaa_repeated", false, true); return; }
                    // State is mandatory before Name/child access: unknown protection fails closed.
                    int state;
                    try { state = node.State(); if (state < 0) throw new InvalidOperationException(); }
                    catch { Note("msaa_state_unavailable"); return; }
                    bool secret = (state & 0x20000000) != 0;
                    var row = new MsaaRow { node_id = msaaAttempts, parent_id = parent, depth = depth,
                        source_hwnd = source, state = state, child_id = node.ChildId, protected_content = secret,
                        focusable = (state & 0x100000) != 0, focused = (state & 4) != 0,
                        disabled = (state & 1) != 0, invisible = (state & 0x8000) != 0, offscreen = (state & 0x10000) != 0,
                        owner_kind = node.SimpleChild ? "simple_child" : "accessible_object" };
                    try { int role = node.Role(); if (role < 0) throw new InvalidOperationException(); row.role = role; }
                    catch { Note("msaa_role_unavailable"); }
                    row.semantic_hint = secret ? "protected" : Hint(row.role);
                    try {
                        row.owner_hwnd = node.OwnerWindow();
                        if (row.owner_hwnd.HasValue) {
                            int owner = api.WindowProcess(row.owner_hwnd.Value);
                            if (owner > 0) row.process_id = owner;
                        }
                    } catch { Note("msaa_owner_unavailable"); }
                    try { row.bounds = node.Location(); } catch { Note("msaa_location_unavailable"); }
                    if (!secret) {
                        if (names) { try { row.name = Short(node.Name()); } catch { Note("msaa_name_unavailable"); } }
                        try {
                            int count = node.ChildCount(); if (count < 0) throw new InvalidOperationException(); row.child_count = count;
                        } catch { Note("msaa_child_count_unavailable"); }
                    } else {
                        row.child_count = 0;
                    }
                    nodes.Add(row);
                    if (secret) return;
                    if (node.SimpleChild) {
                        // S_FALSE/null is an ordinary leaf. Preserve the child-ID node first.
                        IMsaaNode expanded;
                        try { expanded = node.ChildObject(); } catch { Note("msaa_child_object_unavailable"); return; }
                        if (expanded == null) return;
                        if (depth >= maxDepth) { expanded.Dispose(); Note("msaa_depth_limit", false, true); return; }
                        Msaa(expanded, row.node_id, depth + 1, source, seen);
                        return;
                    }
                    if (!row.child_count.HasValue || row.child_count == 0) return;
                    if (depth >= maxDepth) { Note("msaa_depth_limit", false, true); return; }
                    int budget = maxNodes - msaaAttempts;
                    if (budget <= 0) { Note("msaa_node_limit", false, true); return; }
                    int requested = Math.Min(row.child_count.Value, budget), obtained = 0;
                    if (requested < row.child_count.Value) Note("msaa_node_limit", false, true);
                    try {
                        foreach (IMsaaNode child in node.Children(requested)) {
                            obtained++;
                            if (obtained > requested || msaaAttempts >= maxNodes) {
                                child.Dispose(); Note("msaa_node_limit", false, true); break;
                            }
                            Msaa(child, row.node_id, depth + 1, source, seen);
                        }
                    } catch (UnsupportedChildException) { Note("msaa_child_unsupported"); return; }
                    catch { Note("msaa_children_unavailable"); return; }
                    if (obtained < requested) Note("msaa_children_incomplete");
                } catch (UnsupportedChildException) { Note("msaa_child_unsupported"); }
                catch { Note("msaa_unreadable"); }
            }
        }
        public Dictionary<string, object> Run(int processId, string backend) {
            if (processId <= 0 || (backend != "hwnd" && backend != "msaa" && backend != "all"))
                throw new ArgumentException("invalid_argument");
            var snapshot = new Dictionary<int, ProcessRow>();
            try { foreach (var p in api.Processes()) { if (snapshot.Count >= 4096) { Note("process_limit", false, true); break; } snapshot[p.process_id] = p; } }
            catch { Note("process_metadata_unavailable"); }
            try {
                int roots = 0;
                foreach (long root in api.TopWindows(processId)) {
                    if (roots++ >= 8) { Note("window_limit", false, true); break; }
                    if (hwndAttempts >= maxHwnd) { Note("hwnd_limit", false, true); break; }
                    Hwnd(root, root, 0);
                }
            } catch { Note("hwnd_unreadable"); }
            if ((api.Limits & ScanLimits.Desktop) != 0) Note("desktop_scan_limit", false, true);
            if ((api.Limits & ScanLimits.TargetRoots) != 0) Note("target_root_limit", false, true);
            if ((api.Limits & ScanLimits.TargetChildren) != 0) Note("target_hwnd_scan_limit", false, true);
            if ((api.Limits & ScanLimits.Processes) != 0) Note("process_scan_limit", false, true);
            var processes = new List<ProcessRow>();
            string rootImagePath = null;
            foreach (var p in snapshot.Values) {
                int ancestor = p.process_id; var seen = new HashSet<int>(); bool owned = false;
                for (int depth = 0; depth <= 20 && seen.Add(ancestor); depth++) {
                    if (ancestor == processId) { owned = true; break; }
                    ProcessRow up; if (!snapshot.TryGetValue(ancestor, out up)) break; ancestor = up.parent_process_id;
                }
                if (!owned) continue;
                if (processes.Count >= 64) { Note("process_limit", false, true); break; }
                p.filename = Short(p.filename);
                try {
                    string path = api.ImagePath(p.process_id);
                    p.executable_path = Short(path, 1024);
                    if (p.process_id == processId) rootImagePath = path;
                } catch { p.executable_path = null; }
                foreach (var w in windows) if (w.process_id == p.process_id) p.hwnds.Add(w.hwnd);
                p.has_windows = p.hwnds.Count > 0; processes.Add(p);
            }
            if (!snapshot.ContainsKey(processId)) Note("process_not_found", false);
            string version = null;
            if (rootImagePath != null) {
                // Read the actual image, never the shortened diagnostic path.
                try { version = api.ProductVersion(rootImagePath); } catch { }
                if (version != null && !System.Text.RegularExpressions.Regex.IsMatch(version, @"^[0-9]+(\.[0-9]+){1,3}$")) version = null;
            }
            if (backend != "hwnd") foreach (var w in windows) {
                if (msaaAttempts >= maxNodes) { Note("msaa_node_limit", false, true); break; }
                if (w.protected_content) continue;
                try { Msaa(api.Accessible(w.hwnd), null, 0, w.hwnd, new HashSet<string>()); } catch { Note("msaa_unavailable"); }
            }
            string status = truncated || unreadable > 0 ? "unknown_ui_state" : windows.Count == 0 ? "window_not_found" : "discovered";
            return new Dictionary<string, object> {
                {"schema_version", 3}, {"backend", backend}, {"status", status}, {"names_included", names},
                {"process_id", processId}, {"studio_version", version}, {"processes", processes},
                {"hwnd", new Dictionary<string, object> {{"windows", windows}}},
                {"msaa", backend == "hwnd" ? null : new Dictionary<string, object> {{"nodes", nodes}}},
                {"truncated", truncated}, {"unreadable", unreadable}, {"errors", errors}, {"physical_action", false}
            };
        }
    }

    // A separate bounded scan distinguishes desktop exhaustion from target roots.
    public sealed class TopWindowScan {
        readonly int target; int visited;
        public readonly List<long> Roots = new List<long>();
        public ScanLimits Limits { get; private set; }
        public bool CanReadOwner { get { return visited < 512; } }
        public TopWindowScan(int processId) { target = processId; }
        public bool Observe(long hwnd, int owner) {
            if (++visited > 512) { Limits |= ScanLimits.Desktop; return false; }
            if (owner != target) return true;
            if (Roots.Count >= 8) { Limits |= ScanLimits.TargetRoots; return false; }
            Roots.Add(hwnd); return true;
        }
    }
    // Narrow native read adapter. No generic COM dispatch or action wrapper.
    public sealed class NativeDesktop : IReadOnlyDesktop {
        public ScanLimits Limits { get; private set; }
        delegate bool EnumProc(IntPtr hwnd, IntPtr state);
        [StructLayout(LayoutKind.Sequential)] struct Rect { public int left, top, right, bottom; }
        [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)] struct ProcessEntry {
            public uint size, usage, processId; public UIntPtr heap;
            public uint module, threads, parentId; public int priority; public uint flags;
            [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 260)] public string filename;
        }
        [DllImport("user32.dll")] static extern bool EnumWindows(EnumProc callback, IntPtr state);
        [DllImport("user32.dll")] static extern IntPtr GetWindow(IntPtr hwnd, uint command);
        [DllImport("user32.dll")] static extern IntPtr GetParent(IntPtr hwnd);
        [DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(IntPtr hwnd, out uint pid);
        [DllImport("user32.dll", CharSet = CharSet.Unicode)] static extern int GetClassName(IntPtr hwnd, StringBuilder text, int count);
        [DllImport("user32.dll", CharSet = CharSet.Unicode)] static extern int GetWindowText(IntPtr hwnd, StringBuilder text, int count);
        [DllImport("user32.dll", EntryPoint = "GetWindowLongW")] static extern int GetWindowLong(IntPtr hwnd, int index);
        [DllImport("user32.dll")] static extern bool IsWindowVisible(IntPtr hwnd);
        [DllImport("user32.dll")] static extern bool IsWindowEnabled(IntPtr hwnd);
        [DllImport("user32.dll")] static extern bool GetWindowRect(IntPtr hwnd, out Rect rect);
        [DllImport("kernel32.dll")] static extern IntPtr CreateToolhelp32Snapshot(uint flags, uint processId);
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode)] static extern bool Process32FirstW(IntPtr snapshot, ref ProcessEntry row);
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode)] static extern bool Process32NextW(IntPtr snapshot, ref ProcessEntry row);
        [DllImport("kernel32.dll")] static extern IntPtr OpenProcess(uint access, bool inherit, uint pid);
        [DllImport("kernel32.dll")] static extern bool CloseHandle(IntPtr handle);
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode)] static extern bool QueryFullProcessImageName(IntPtr process, uint flags, StringBuilder path, ref int length);
        [DllImport("oleacc.dll")] static extern int AccessibleObjectFromWindow(IntPtr hwnd, uint objectId, ref Guid iid, [MarshalAs(UnmanagedType.Interface)] out IAccessible value);
        internal static readonly Guid AccessibleId = new Guid("618736E0-3C3D-11CF-810C-00AA00389B71");
        public int WindowProcess(long hwnd) { uint pid; GetWindowThreadProcessId(new IntPtr(hwnd), out pid); return checked((int)pid); }
        public IEnumerable<long> TopWindows(int processId) {
            var scan = new TopWindowScan(processId);
            bool completed = EnumWindows(delegate(IntPtr h, IntPtr unused) {
                return scan.Observe(h.ToInt64(), scan.CanReadOwner ? WindowProcess(h.ToInt64()) : 0);
            }, IntPtr.Zero);
            if (!completed && scan.Limits == ScanLimits.None) throw new InvalidOperationException();
            Limits |= scan.Limits;
            return scan.Roots;
        }
        public IEnumerable<long> ChildWindows(long hwnd) {
            IntPtr h = GetWindow(new IntPtr(hwnd), 5); var seen = new HashSet<long>(); int visited = 0;
            while (h != IntPtr.Zero) {
                if (++visited > 2000 || !seen.Add(h.ToInt64())) { Limits |= ScanLimits.TargetChildren; yield break; }
                yield return h.ToInt64(); h = GetWindow(h, 2);
            }
        }
        public WindowRow Window(long hwnd, bool includeNames) {
            IntPtr h = new IntPtr(hwnd); var cls = new StringBuilder(161); Rect r;
            if (GetClassName(h, cls, cls.Capacity) == 0 || !GetWindowRect(h, out r)) throw new InvalidOperationException();
            string className = cls.ToString();
            bool secret = className.IndexOf("edit", StringComparison.OrdinalIgnoreCase) >= 0 && (GetWindowLong(h, -16) & 0x20) != 0;
            string name = null;
            if (includeNames && !secret) { var text = new StringBuilder(161); GetWindowText(h, text, text.Capacity); name = text.ToString(); }
            long parent = GetParent(h).ToInt64();
            return new WindowRow { hwnd = hwnd, parent_hwnd = parent == 0 ? (long?)null : parent, process_id = WindowProcess(hwnd),
                class_name = className, name = name, visible = IsWindowVisible(h), enabled = IsWindowEnabled(h), protected_content = secret,
                bounds = new int[] {r.left, r.top, checked(r.right - r.left), checked(r.bottom - r.top)} };
        }
        public IEnumerable<ProcessRow> Processes() {
            IntPtr handle = CreateToolhelp32Snapshot(2, 0);
            if (handle == new IntPtr(-1)) throw new InvalidOperationException();
            try {
                var row = new ProcessEntry { size = (uint)Marshal.SizeOf(typeof(ProcessEntry)) }; int count = 0;
                bool next = Process32FirstW(handle, ref row);
                while (next) {
                    if (++count > 4096) { Limits |= ScanLimits.Processes; yield break; }
                    yield return new ProcessRow { process_id = checked((int)row.processId), parent_process_id = checked((int)row.parentId), filename = row.filename };
                    next = Process32NextW(handle, ref row);
                }
            } finally { CloseHandle(handle); }
        }
        public string ImagePath(int processId) {
            IntPtr process = OpenProcess(0x1000, false, checked((uint)processId));
            if (process == IntPtr.Zero) return null; // Optional metadata, never a fatal error.
            try { var text = new StringBuilder(32768); int length = text.Capacity;
                return QueryFullProcessImageName(process, 0, text, ref length) ? text.ToString() : null;
            } finally { CloseHandle(process); }
        }
        public string ProductVersion(string path) { return FileVersionInfo.GetVersionInfo(path).ProductVersion; }
        public IMsaaNode Accessible(long hwnd) {
            IAccessible value; Guid iid = AccessibleId;
            if (AccessibleObjectFromWindow(new IntPtr(hwnd), unchecked((uint)-4), ref iid, out value) < 0 || value == null) throw new InvalidOperationException();
            return new NativeAccessible(new NativeComReads(value), 0, true);
        }
    }
    sealed class UnsupportedChildException : Exception { }
    // Raw variant/property boundary is injectable; fake tests exercise the real wrapper.
    interface IAccessibleReads : IDisposable {
        string Identity { get; }
        int State(int child); int Role(int child); string Name(int child);
        int ChildCount(); int[] Location(int child); long? OwnerWindow();
        IEnumerable<object> Children(int count);
        IAccessibleReads ChildObject(int child);
    }
    sealed class NativeComReads : IAccessibleReads {
        IAccessible accessible;
        [DllImport("oleacc.dll")] static extern int WindowFromAccessibleObject(IAccessible value, out IntPtr hwnd);
        [DllImport("oleacc.dll")] static extern int AccessibleChildren(IAccessible value, int start, int count,
            [Out, MarshalAs(UnmanagedType.LPArray, ArraySubType = UnmanagedType.Struct, SizeParamIndex = 2)] object[] children, out int obtained);
        public NativeComReads(IAccessible value) { accessible = value; }
        public string Identity { get {
            IntPtr identity = Marshal.GetIUnknownForObject(accessible);
            try { return identity.ToInt64().ToString("x"); } finally { Marshal.Release(identity); }
        } }
        public int State(int child) { object value = accessible.get_accState(child); if (!(value is int)) throw new InvalidOperationException(); return (int)value; }
        public int Role(int child) { object value = accessible.get_accRole(child); if (!(value is int)) throw new InvalidOperationException(); return (int)value; }
        public int ChildCount() { return accessible.accChildCount; }
        public string Name(int child) { return accessible.get_accName(child); }
        public int[] Location(int child) { int x, y, w, h; accessible.accLocation(out x, out y, out w, out h, child); return new int[] {x, y, w, h}; }
        public long? OwnerWindow() { IntPtr h; return WindowFromAccessibleObject(accessible, out h) >= 0 && h != IntPtr.Zero ? (long?)h.ToInt64() : null; }
        static IAccessibleReads Object(object value) {
            if (value == null) return null; // S_FALSE is nonnegative and marshals a null retval.
            if (value is IAccessible) return new NativeComReads((IAccessible)value);
            if (Marshal.IsComObject(value)) Marshal.ReleaseComObject(value);
            throw new UnsupportedChildException();
        }
        public IAccessibleReads ChildObject(int child) { return Object(accessible.get_accChild(child)); }
        public IEnumerable<object> Children(int count) {
            var values = new object[count]; int obtained; int index = 0;
            try {
                if (AccessibleChildren(accessible, 0, count, values, out obtained) < 0 || obtained < 0 || obtained > count) throw new InvalidOperationException();
                for (; index < obtained; index++) {
                    object value = values[index]; values[index] = null;
                    if (value is int && (int)value > 0) yield return value;
                    else {
                        var child = Object(value);
                        if (child == null) throw new UnsupportedChildException();
                        yield return child;
                    }
                }
            } finally {
                foreach (object value in values) if (value != null && Marshal.IsComObject(value)) Marshal.ReleaseComObject(value);
            }
        }
        public void Dispose() {
            if (accessible != null && Marshal.IsComObject(accessible)) Marshal.ReleaseComObject(accessible);
            accessible = null;
        }
    }
    sealed class NativeAccessible : IMsaaNode {
        IAccessibleReads accessible, resolved;
        readonly int childId; readonly bool owned;
        bool resolutionChecked, resolutionFailure, resolvedMetadata;
        public NativeAccessible(IAccessibleReads value, int child, bool owns) { accessible = value; childId = child; owned = owns; }
        public bool SimpleChild { get { return childId != 0; } }
        public int? ChildId { get { return SimpleChild ? (int?)childId : null; } }
        public string Identity { get { return accessible.Identity + ":" + childId; } }
        IAccessibleReads Resolve() {
            if (!resolutionChecked) {
                resolutionChecked = true;
                try { resolved = accessible.ChildObject(childId); } catch { resolutionFailure = true; throw; }
            }
            if (resolutionFailure) throw new InvalidOperationException();
            return resolved;
        }
        IAccessibleReads Metadata { get { return resolvedMetadata ? resolved : accessible; } }
        int MetadataChild { get { return resolvedMetadata ? 0 : childId; } }
        public int State() {
            if (!SimpleChild) return accessible.State(0);
            int? parentState = null;
            try { parentState = accessible.State(childId); } catch { }
            if (parentState.HasValue && (parentState.Value & 0x20000000) != 0) return parentState.Value;
            IAccessibleReads child;
            try { child = Resolve(); }
            catch { if (parentState.HasValue) return parentState.Value; throw; }
            if (child != null) {
                // A child ID can resolve to a full object whose parent supplies no properties.
                // Its own mandatory protection state precedes all name/child reads.
                int state = child.State(0); resolvedMetadata = true; return state;
            }
            if (!parentState.HasValue) throw new InvalidOperationException();
            return parentState.Value;
        }
        public int Role() { return Metadata.Role(MetadataChild); }
        public string Name() { return Metadata.Name(MetadataChild); }
        public int[] Location() { return Metadata.Location(MetadataChild); }
        public long? OwnerWindow() { return Metadata.OwnerWindow(); }
        public int ChildCount() { return SimpleChild ? 0 : accessible.ChildCount(); }
        public IMsaaNode ChildObject() {
            if (!SimpleChild || Resolve() == null) return null;
            var result = new NativeAccessible(resolved, 0, true); resolved = null; return result;
        }
        public IEnumerable<IMsaaNode> Children(int count) {
            foreach (object value in accessible.Children(count)) {
                if (value is int && (int)value > 0) yield return new NativeAccessible(accessible, (int)value, false);
                else if (value is IAccessibleReads) yield return new NativeAccessible((IAccessibleReads)value, 0, true);
                else throw new UnsupportedChildException();
            }
        }
        public void Dispose() {
            if (resolved != null) resolved.Dispose();
            if (owned && accessible != null) accessible.Dispose();
            resolved = null; accessible = null;
        }
    }
    public static class Entry {
        public static object Run(int pid, string backend, int maxHwnd, int maxNodes, int maxDepth, bool names) {
            return new Collector(new NativeDesktop(), maxHwnd, maxNodes, maxDepth, names).Run(pid, backend);
        }
    }
}
