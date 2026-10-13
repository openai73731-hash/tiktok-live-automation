"""Android 13 ASCII entry + selector-resolved activation, no guessed coordinates.

Uses the normal focused EditText. No IME installation, root, clipboard or Unicode
claims. AOSP input converts %s to spaces; exact UI readback is always required.
"""
from app.android.adb import AdbCommand, ascii_supported
from app.android.tiktok_ui import unique_node, resolved_send_point


class TextEntryError(RuntimeError):
    def __init__(self): super().__init__("Text entry requires operator review")


class ASCIITextEntryBackend:
    def __init__(self, controller):
        self.controller = controller
        self.target = None
        self.guard = lambda: False
        self.expected = None

    def supports(self, text): return ascii_supported(text)

    def bind(self, target, guard, *, require_send=True):
        self.target, self.guard = target, guard
        self.require_send = require_send
        self.expected = None

    async def checked(self, serial, *, focused=False):
        c = self.controller
        state = await c._inspect(self.target)
        entry = unique_node(c._xml, c._profile.input_id) if c._profile else None
        ready = state.ready if self.require_send else c.fill_ready_for(self.target.live_session_id, self.target.account)
        if (not self.guard() or not ready or state.serial != serial or entry is None or
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
        entry = await self.checked(serial, focused=True)
        if entry.get("text") != self.expected: raise TextEntryError()
        point = resolved_send_point(c._xml, c._profile)
        if point is None or not guard(): return False
        # Bounds come ONLY from the unique confirmed reviewed vp8/vpl relationship.
        # The transport repeats the synchronous guard immediately before spawning.
        await c.transport.run(AdbCommand.TAP_SEND, serial=serial, point=point, guard=guard)
        return True

    async def aclose(self):
        self.target = self.expected = None
