"""Android 13 ASCII entry + selector-verified keyboard activation, no coordinates.

Uses the normal focused EditText. No IME installation, root, clipboard or Unicode
claims. AOSP input converts %s to spaces; exact UI readback is always required.
"""
from app.android.adb import AdbCommand, ascii_supported
from app.android.tiktok_ui import unique_node, rid, parse_hierarchy


class TextEntryError(RuntimeError):
    def __init__(self): super().__init__("Text entry requires operator review")


class ASCIITextEntryBackend:
    def __init__(self, controller):
        self.controller = controller
        self.target = None
        self.guard = lambda: False
        self.expected = None

    def supports(self, text): return ascii_supported(text)

    def bind(self, target, guard):
        self.target, self.guard = target, guard
        self.expected = None

    async def checked(self, serial, *, focused=False):
        c = self.controller
        state = await c._inspect(self.target)
        entry = unique_node(c._xml, c._profile.input_id) if c._profile else None
        if (not self.guard() or not state.ready or state.serial != serial or entry is None or
                focused and entry.get("focused") != "true"):
            raise TextEntryError()
        return entry

    async def clear(self, serial):
        await self.checked(serial, focused=True)
        await self.controller.transport.run(AdbCommand.SELECT_ALL, serial=serial, guard=self.guard)
        await self.checked(serial, focused=True)
        await self.controller.transport.run(AdbCommand.DELETE, serial=serial, guard=self.guard)
        entry = await self.checked(serial, focused=True)
        if entry.get("text") != "": raise TextEntryError()

    async def prepare_exact(self, serial, resource_id, text):
        c = self.controller
        if not self.supports(text) or not c._profile or resource_id != c._profile.input_id: raise TextEntryError()
        await self.clear(serial)
        await self.checked(serial, focused=True)
        await c.transport.run(AdbCommand.TEXT, serial=serial, text=text, guard=self.guard)
        entry = await self.checked(serial, focused=True)
        if entry.get("text") != text:
            # Clear only after another full safe focused-field inspection. Never send.
            await self.clear(serial)
            raise TextEntryError()
        self.expected = text
        return entry.get("text")

    async def click_send(self, serial, resource_id, guard):
        c = self.controller
        if not c._profile or resource_id != c._profile.send_id or self.expected is None: return False
        # TAB only changes focus. No Enter/IME action on an input field and no tap fallback.
        # Both vp8 and its clickable vpl child belong to the one confirmed candidate.
        seen = set()
        for _ in range(8):
            entry = await self.checked(serial)
            if entry.get("text") != self.expected: raise TextEntryError()
            root = parse_hierarchy(c._xml)
            focused = [n for n in root.iter() if n.get("focused") == "true"]
            if len(focused) != 1: return False
            identity = focused[0].get("resource-id")
            if identity in {resource_id, rid("vpl")}:
                if any(unique_node(c._xml, name).get("enabled") != "true" for name in (resource_id, rid("vpl"))): return False
                if not guard(): return False
                # One activation only. Exceptions/cancellation after dispatch stay UNKNOWN.
                await c.transport.run(AdbCommand.ACTIVATE, serial=serial, guard=guard)
                return True
            if identity in seen or identity not in {rid(n) for n in ("h4k", "qde", "qcv", "lbz")}:
                return False
            seen.add(identity)
            await c.transport.run(AdbCommand.TAB, serial=serial, guard=guard)
        return False

    async def aclose(self):
        self.target = self.expected = None
