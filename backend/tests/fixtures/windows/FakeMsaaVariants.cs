// Fake HRESULT/property/VARIANT boundary: no native API and no real UI.
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
namespace LiveStudioDiscovery {
    sealed class FakeAccessibleReads : IAccessibleReads, IIa2Reads {
        public string identity = "parent";
        public object[] variants = new object[0];
        public readonly Dictionary<int, IAccessibleReads> objects = new Dictionary<int, IAccessibleReads>();
        public readonly List<string> calls = new List<string>();
        public bool protectedChild, protectedSelf, unknownState, unknownSelf, roleFailure, nameFailure, locationFailure, countFailure, objectFailure, childrenFailure;
        public int closes;
        public string Identity { get { return identity; } }
        public Action<string, int> observe;
        public IIa2Com ia2;
        public Ia2Capability Capability() { return Ia2Contract.Check(ia2 ?? new FakeIa2Com { providerHr = unchecked((int)0x80004002) }); }
        void Read(string property, int child) { calls.Add(property + ":" + child); if (observe != null) observe(property, child); }
        static Exception Failure() { return new COMException("PRIVATE HRESULT EXCEPTION", unchecked((int)0x80004005)); }
        public int State(int child) {
            Read("state", child);
            if ((child == 1 && unknownState) || (child == 0 && unknownSelf)) throw Failure();
            return (child == 1 && protectedChild) || (child == 0 && protectedSelf) ? 0x20000000 : child > 0 ? 0x100004 : 0;
        }
        public int Role(int child) { Read("role", child); if (child == 1 && roleFailure) throw Failure(); return child == 0 ? 9 : child == 1 ? 42 : child == 2 ? 43 : 41; }
        public string Name(int child) { Read("name", child); if (child == 1 && nameFailure) throw Failure(); return "Label " + child; }
        public int[] Location(int child) { Read("location", child); if (child == 1 && locationFailure) throw Failure(); return new int[] {child, 2, 30, 40}; }
        public int ChildCount() { Read("count", 0); if (countFailure) throw Failure(); return variants.Length; }
        public long? OwnerWindow() { return 200; }
        public IVariantApi nativeVariants;
        public IEnumerable<ChildRead> Children(int count) {
            if (nativeVariants != null) { foreach (var read in VariantReader.Read(nativeVariants, count)) yield return read; yield break; }
            if (childrenFailure) throw Failure(); for (int i = 0; i < Math.Min(count, variants.Length); i++) yield return ChildRead.Managed(variants[i], i);
        }
        public IAccessibleReads ChildObject(int child) {
            Read("child", child);
            if (objectFailure) throw Failure();
            IAccessibleReads value;
            if (objects.TryGetValue(child, out value)) return value;
            // A nonnegative S_FALSE from COM produces a null managed retval.
            Read("child_S_FALSE", child); return null;
        }
        public void Dispose() { closes++; }
    }
    public static class VariantHarness {
        public static Dictionary<string, object> All() {
            var result = new Dictionary<string, object>();
            foreach (string mode in new string[] {"seven_ids", "seven_names", "simple_protected", "simple_unknown", "role_failure", "name_failure", "location_failure", "count_failure", "object_failure", "children_failure", "child_object", "object_properties", "protected_object", "object_protected_parent_clear", "object_unknown_state", "dispatch", "object_cycle", "simple_repeat", "variant_depth", "variant_nodes", "unsupported_variant", "eight_roots", "nine_roots", "desktop_limit", "target_limit", "process_scan_limit"}) {
                var reads = new FakeAccessibleReads { variants = new object[] {1, 2, 3, 4, 5, 6, 7} };
                var child = new FakeAccessibleReads { identity = "child-object", variants = new object[] {1} };
                if (mode == "simple_protected") reads.protectedChild = true;
                if (mode == "simple_unknown") reads.unknownState = true;
                if (mode == "role_failure") reads.roleFailure = true;
                if (mode == "name_failure") reads.nameFailure = true;
                if (mode == "location_failure") reads.locationFailure = true;
                if (mode == "count_failure") reads.countFailure = true;
                if (mode == "object_failure") reads.objectFailure = true;
                if (mode == "children_failure") reads.childrenFailure = true;
                if (mode == "child_object" || mode == "object_properties" || mode == "protected_object" || mode == "object_protected_parent_clear" || mode == "object_unknown_state" || mode == "variant_depth") {
                    reads.variants = new object[] {1}; reads.objects[1] = child;
                    if (mode == "object_properties" || mode == "protected_object") reads.unknownState = true;
                    if (mode == "protected_object" || mode == "object_protected_parent_clear") child.protectedSelf = true;
                    if (mode == "object_unknown_state") child.unknownSelf = true;
                }
                if (mode == "dispatch") reads.variants = new object[] {child};
                if (mode == "object_cycle") reads.variants = new object[] {reads};
                if (mode == "simple_repeat") reads.variants = new object[] {1, 1};
                if (mode == "unsupported_variant") reads.variants = new object[] {"unsupported"};
                var api = new FakeDesktop { accessibleFactory = delegate { return new NativeAccessible(reads, 0, false); } };
                bool hwnd = mode == "eight_roots" || mode == "nine_roots" || mode == "desktop_limit" || mode == "target_limit" || mode == "process_scan_limit";
                if (mode == "eight_roots" || mode == "nine_roots" || mode == "desktop_limit") {
                    var scan = new TopWindowScan(11300);
                    int targets = mode == "desktop_limit" ? 1 : mode == "nine_roots" ? 9 : 8;
                    for (int i = 0; i < targets; i++) if (!scan.Observe(100 + i, 11300)) break;
                    if (mode != "nine_roots") for (int i = 0; i < (mode == "desktop_limit" ? 512 : 14); i++) if (!scan.Observe(1000 + i, 999)) break;
                    api.limits = scan.Limits; api.stats = scan.Statistics;
                    api.topFactory = delegate(int pid) { return scan.Roots; };
                }
                if (mode == "target_limit") api.limits = ScanLimits.TargetChildren;
                if (mode == "process_scan_limit") api.limits = ScanLimits.Processes;
                var report = new Collector(api, 256, mode == "variant_nodes" ? 2 : 500, mode == "variant_depth" ? 1 : 12,
                    mode != "seven_ids").Run(11300, hwnd ? "hwnd" : "msaa");
                result[mode] = new Dictionary<string, object> { {"report", report}, {"calls", reads.calls}, {"object_calls", child.calls}, {"object_closes", child.closes} };
            }
            return result;
        }
    }
}
