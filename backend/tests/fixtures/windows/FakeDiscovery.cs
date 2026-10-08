// Pure in-memory boundaries. Never instantiates NativeDesktop or calls a Windows API.
using System;
using System.Collections.Generic;
namespace LiveStudioDiscovery {
    public sealed class FakeNode : IMsaaNode {
        public string id, label = "PRIVATE UI NAME";
        public int role = 15, state, badCount;
        public bool broken, simple;
        public int nameReads, protectedReads, closes;
        public FakeNode[] children = new FakeNode[0];
        public string Identity { get { return id; } }
        public bool SimpleChild { get { return simple; } }
        public int State() { if (broken) throw new Exception("PRIVATE EXCEPTION"); return state; }
        public int Role() { return role; }
        public int ChildCount() { return badCount != 0 ? badCount : children.Length; }
        public string Name() { nameReads++; if ((state & 0x20000000) != 0) { protectedReads++; throw new Exception("PRIVATE PASSWORD"); } return label; }
        public int[] Location() { return new int[] {1, 2, 30, 40}; }
        public long? OwnerWindow() { return id == "unknown_owner" ? (long?)null : 200; }
        public IEnumerable<IMsaaNode> Children(int count) { for (int i = 0; i < Math.Min(count, children.Length); i++) yield return children[i]; }
        public void Dispose() { closes++; }
        // Traps exist on the fake but are absent from the production read interface.
        public void Invoke() { throw new Exception("ACTION FORBIDDEN"); }
        public void SetValue() { throw new Exception("ACTION FORBIDDEN"); }
    }
    public sealed class FakeDesktop : IReadOnlyDesktop {
        public bool LimitReached { get { return false; } }
        public bool noPath, badWindow, noMsaa, pathThrows, protectedWindow, longPath;
        public int accessibleCalls;
        public FakeNode root;
        public bool hwndCycle;
        public IEnumerable<long> TopWindows(int pid) { if (pid == 11300) yield return 100; }
        public IEnumerable<long> ChildWindows(long hwnd) {
            if (hwnd == 100) yield return 200;
            if (hwnd == 200) yield return hwndCycle ? 100 : 300;
        }
        public int WindowProcess(long hwnd) { return hwnd == 100 ? 11300 : hwnd == 200 ? 11301 : 11302; }
        public WindowRow Window(long hwnd, bool names) {
            if (badWindow) throw new Exception("PRIVATE WINDOW");
            return new WindowRow { hwnd = hwnd, parent_hwnd = hwnd == 100 ? (long?)null : hwnd == 200 ? 100 : 200,
                process_id = WindowProcess(hwnd), class_name = hwnd == 100 ? "Chrome_WidgetWin_1" : hwnd == 200 ? "Chrome_RenderWidgetHostHWND" : "Chrome_Other",
                name = names ? "PRIVATE WINDOW NAME" : null, protected_content = protectedWindow,
                enabled = true, visible = true, bounds = new int[] {0, 0, 10, 20} };
        }
        public IEnumerable<ProcessRow> Processes() {
            yield return new ProcessRow {process_id = 11300, parent_process_id = 50, filename = "SyntheticStudio.exe"};
            yield return new ProcessRow {process_id = 11301, parent_process_id = 11300, filename = "SyntheticChild.exe"};
            yield return new ProcessRow {process_id = 11302, parent_process_id = 11301, filename = "SyntheticChild.exe"};
            yield return new ProcessRow {process_id = 555, parent_process_id = 50, filename = "Unrelated.exe"};
        }
        public string ImagePath(int pid) { if (pathThrows) throw new Exception("PRIVATE PATH"); return noPath ? null : longPath ? "X:/" + new string('a', 1100) + "/studio.exe" : "X:/synthetic/studio.exe"; }
        public string ProductVersion(string path) { if (!path.EndsWith("/studio.exe")) throw new Exception("Shortened path"); return "1.36.6"; }
        public IMsaaNode Accessible(long hwnd) { accessibleCalls++; if (noMsaa) throw new Exception("PRIVATE COM"); return root; }
    }
    public static class Harness {
        public static object All() {
            var result = new Dictionary<string, object>();
            foreach (string mode in new string[] {"hwnd", "msaa", "names", "password", "protected_hwnd", "unreadable_state", "cycle", "hwnd_cycle", "depth", "nodes", "hwnd_limit", "malformed", "bad_count", "unknown_owner", "no_path", "denied_path", "long_path", "bad_window", "no_msaa", "invalid_pid"}) {
                var button = new FakeNode {id = "button", role = 43};
                var edit = new FakeNode {id = "edit", role = 42, state = 0x100004, children = new FakeNode[] {button}};
                var root = new FakeNode {id = "root", children = new FakeNode[] {edit,
                    new FakeNode {id = "text", role = 41}, new FakeNode {id = "list", role = 33},
                    new FakeNode {id = "listitem", role = 34, simple = true}, new FakeNode {id = "pane", role = 16}}};
                var api = new FakeDesktop {root = root};
                if (mode == "password") edit.state |= 0x20000000;
                if (mode == "cycle") edit.children = new FakeNode[] {root};
                if (mode == "hwnd_cycle") api.hwndCycle = true;
                if (mode == "malformed") edit.broken = true;
                if (mode == "unreadable_state") root.broken = true;
                if (mode == "protected_hwnd") api.protectedWindow = true;
                if (mode == "bad_count") edit.badCount = -1;
                if (mode == "unknown_owner") edit.id = "unknown_owner";
                if (mode == "no_path") api.noPath = true;
                if (mode == "denied_path") api.pathThrows = true;
                if (mode == "long_path") api.longPath = true;
                if (mode == "bad_window") api.badWindow = true;
                if (mode == "no_msaa") api.noMsaa = true;
                var report = new Collector(api, mode == "hwnd_limit" ? 1 : 256, mode == "nodes" ? 1 : 500,
                    mode == "depth" ? 1 : 12, mode == "names" || mode == "password" || mode == "protected_hwnd" || mode == "unreadable_state").Run(mode == "invalid_pid" ? 999 : 11300,
                    mode == "hwnd" || mode == "no_path" || mode == "denied_path" ? "hwnd" : "msaa");
                result[mode] = new Dictionary<string, object> {{"report", report}, {"name_reads", edit.nameReads},
                    {"protected_reads", edit.protectedReads}, {"accessible_calls", api.accessibleCalls}, {"root_closes", root.closes},
                    {"root_name_reads", root.nameReads}, {"physical_action", false}};
            }
            return result;
        }
    }
}
