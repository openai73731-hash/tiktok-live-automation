// Tests the production VARIANT buffer reader, not the CLR object[] representation.
// Fake Init/Fetch/QI/Clear never calls native libraries or constructs NativeDesktop.
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
namespace LiveStudioDiscovery {
    sealed class FakeVariantApi : IVariantApi {
        public int[] tags = new int[] {3, 3, 3, 3, 3, 3, 3};
        public int hr, obtained = 7, inits, clears, queries, badIdValue = -1;
        public bool badId, nullDispatch, failQi, throwFetch, invalidObtained, failClear;
        public readonly List<FakeAccessibleReads> objects = new List<FakeAccessibleReads>();
        public void Init(IntPtr slot) {
            if (Marshal.ReadInt16(slot) != 0) throw new Exception("UNINITIALIZED");
            Marshal.WriteInt16(slot, 0); inits++;
        }
        public int Clear(IntPtr slot) { clears++; Marshal.WriteInt16(slot, 0); return failClear && clears % 7 == 1 ? unchecked((int)0x80070057) : 0; }
        public int Fetch(IntPtr buffer, int count, out int actual) {
            actual = invalidObtained ? obtained : Math.Min(count, obtained);
            if (throwFetch) throw new COMException("PRIVATE FETCH", unchecked((int)0x80070005));
            for (int i = 0; i < Math.Min(count, tags.Length); i++) {
                IntPtr slot = IntPtr.Add(buffer, i * VariantReader.Size);
                Marshal.WriteInt16(slot, unchecked((short)tags[i]));
                if (tags[i] == 3) Marshal.WriteInt32(slot, 8, badId && i == 0 ? badIdValue : i + 1);
                else if (tags[i] == 9) Marshal.WriteIntPtr(slot, 8, nullDispatch && i == 0 ? IntPtr.Zero : new IntPtr(0x12345678 + i));
                else if (tags[i] == 8 || tags[i] == 0x4003 || tags[i] == 0x2003)
                    // Deliberately untrusted fake address: the reader must never dereference it.
                    Marshal.WriteIntPtr(slot, 8, new IntPtr(0x76543210));
            }
            return hr;
        }
        public IAccessibleReads Dispatch(IntPtr borrowed, out int result) {
            queries++;
            if (failQi) { result = unchecked((int)0x80004002); return null; }
            result = 0;
            var child = new FakeAccessibleReads { identity = "object-" + borrowed.ToInt64(), variants = new object[0] };
            objects.Add(child); return child;
        }
    }
    public static class NativeVariantHarness {
        public static Dictionary<string, object> All() {
            var result = new Dictionary<string, object>();
            foreach (string mode in new string[] {"native_i4", "native_dispatch", "native_mixed", "native_partial", "native_empty",
                "native_qi", "native_unsupported", "native_malformed", "native_bad_id", "native_self", "native_null_dispatch",
                "native_bad_obtained", "native_fetch_failure", "native_diag_cap", "native_diag_budget", "native_clear_failure", "root_priority"}) {
                var api = new FakeVariantApi();
                if (mode == "native_dispatch" || mode == "native_qi" || mode == "native_null_dispatch") api.tags = new int[] {9, 9, 9, 9, 9, 9, 9};
                if (mode == "native_mixed") api.tags = new int[] {3, 9, 3, 9, 3, 9, 3};
                if (mode == "native_partial") { api.tags = new int[] {3, 9}; api.obtained = 2; api.hr = 1; }
                if (mode == "native_empty") api.tags = new int[] {3, 0, 3, 9, 3, 9, 3};
                if (mode == "native_qi") api.failQi = true;
                if (mode == "native_unsupported") api.tags = new int[] {8, 0x4003, 0x2003, 3, 9, 3, 9};
                if (mode == "native_malformed") api.tags = new int[] {0xffff, 3, 9, 3, 9, 3, 9};
                if (mode == "native_bad_id") api.badId = true;
                if (mode == "native_self") { api.badId = true; api.badIdValue = 0; }
                if (mode == "native_null_dispatch") api.nullDispatch = true;
                if (mode == "native_bad_obtained") { api.obtained = 8; api.invalidObtained = true; }
                if (mode == "native_fetch_failure") api.throwFetch = true;
                if (mode == "native_diag_cap") { api.tags = new int[100]; for (int i = 0; i < 100; i++) api.tags[i] = 8; api.obtained = 100; }
                if (mode == "native_diag_budget") api.tags = new int[] {8, 8, 8, 8, 8, 8, 8};
                if (mode == "native_clear_failure") api.failClear = true;
                var reads = new FakeAccessibleReads { nativeVariants = api, variants = new object[api.tags.Length == 100 ? 100 : 7] };
                var desktop = new FakeDesktop { accessibleFactory = delegate { return new NativeAccessible(reads, 0, false); } };
                if (mode == "root_priority") {
                    var scan = new TopWindowScan(11300);
                    for (int i = 112; i >= 101; i--) scan.Observe(i, 11300, false);
                    scan.Observe(100, 11300, true); // Visible GUI arrives after all hidden roots.
                    desktop.limits = scan.Limits; desktop.stats = scan.Statistics;
                    desktop.topFactory = delegate(int pid) { return scan.Roots; };
                    desktop.hiddenRoots = true;
                }
                var report = new Collector(desktop, 256, mode == "native_diag_budget" ? 2 : 500, 12, false).Run(11300, mode == "root_priority" ? "hwnd" : "msaa");
                int closes = 0; foreach (var child in api.objects) closes += child.closes;
                result[mode] = new Dictionary<string, object> {{"report", report}, {"inits", api.inits}, {"clears", api.clears},
                    {"queries", api.queries}, {"closes", closes}, {"slot_size", VariantReader.Size},
                    {"payload_offset", Marshal.OffsetOf(typeof(NativeVariant), "payload").ToInt32()}, {"pointer_size", IntPtr.Size}};
            }
            var early = new FakeVariantApi();
            using (var iterator = VariantReader.Read(early, 7).GetEnumerator()) { iterator.MoveNext(); }
            result["early_disposal"] = new Dictionary<string, object> {{"inits", early.inits}, {"clears", early.clears}};
            return result;
        }
    }
}
