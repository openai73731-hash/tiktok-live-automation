"""Android 13 ASCII entry + selector-resolved activation, no guessed coordinates.

Uses the normal focused EditText. No IME installation, root, clipboard or Unicode
claims. AOSP input converts %s to spaces; exact UI readback is always required.
"""
import logging
from itertools import zip_longest
from time import monotonic
import unicodedata
from app.android.adb import AdbCommand, AdbError, ascii_supported
from app.android.tiktok_ui import unique_node, resolved_send_point
from app.logging_config import LOGGER_NAME

logger = logging.getLogger(LOGGER_NAME + ".android")


class TextEntryError(RuntimeError):
    def __init__(self, reason="rejected"):
        super().__init__("Text entry requires operator review")
        self.reason = reason


class ASCIITextEntryBackend:
    def __init__(self, controller):
        self.controller = controller
        self.target = None
        self.guard = lambda: False
        self.expected = None
        self._attempted_text = None

    def supports(self, text): return ascii_supported(text)

    def bind(self, target, guard, *, require_send=True, inspection=None, before_send=None):
        self.target, self.guard = target, guard
        self.require_send = require_send
        self.inspection = inspection
        self.before_send = before_send
        self.expected = None
        self._attempted_text = None
        self.diagnostic = dict(text_entry_method="adb_ascii_focused",
            input_target_resource_id=self.controller._profile.input_id if self.controller._profile else None,
            target_focused_before_input=None, post_readback_result="not_attempted", rejection_reason="none",
            command=None, category="not_attempted", exit_code=None, stdout_present=False, stderr_present=False,
            stderr_miui_uiautomator_noise=False)

    def report(self, reason="none", **fields):
        self.diagnostic.update(fields, rejection_reason=reason)
        logger.info("android_text_entry" if self.require_send else "android_manual_text_entry", extra={"fields": dict(self.diagnostic)})

    def reject(self, reason, **fields):
        self.report(reason, **fields)
        raise TextEntryError(reason)

    async def command(self, command, serial, **kwargs):
        try:
            await self.controller.transport.run(command, serial=serial, guard=self.guard, **kwargs)
        except AdbError:
            self.report("adb_failed", **self.command_result(command))
            raise
        self.report(**self.command_result(command))

    def command_result(self, command):
        result = getattr(self.controller.transport, "last_command_result", None)
        return result if result and result.get("command") == command.value else dict(command=command.value, category="unreported")

    def record_readback(self, text, entry):
        actual = entry.get("text", "")
        # Boolean/length evidence only. Never log approved or read-back text.
        self.diagnostic.update(readback_expected_length=len(text), readback_actual_length=len(actual),
            readback_text_present=entry.get("text") is not None, readback_exact_match=actual == text,
            readback_trim_match=actual.strip() == text, readback_casefold_match=actual.casefold() == text.casefold(),
            readback_is_prefix=text.startswith(actual),
            readback_leading_whitespace=len(actual) - len(actual.lstrip()),
            readback_trailing_whitespace=len(actual) - len(actual.rstrip()))
        def category(c):
            if c is None: return 'missing'
            if c.isspace(): return 'space'
            kind = unicodedata.category(c)[0]
            return {'L':'letter','N':'number','P':'punctuation','S':'symbol','M':'mark','C':'control'}.get(kind,'other')
        differences = [(i, category(expected), category(found)) for i, (expected, found) in
            enumerate(zip_longest(text, actual)) if expected != found]
        self.diagnostic.update(readback_first_mismatch_index=differences[0][0] if differences else None,
            readback_mismatch_count=len(differences), readback_changed_categories=sorted({a+'->'+b for _, a, b in differences}),
            readback_non_ascii_count=sum(ord(c)>127 for c in actual),
            readback_case_only_count=sum(a is not None and b is not None and a != b and a.casefold()==b.casefold()
                for a,b in zip_longest(text,actual)))

    async def checked(self, serial, *, focused=False):
        c = self.controller
        host_evidence = c._live_evidence
        state = await self.inspection.read() if not self.require_send and self.inspection else await c._inspect(self.target)
        entry = unique_node(c._xml, c._profile.input_id) if c._profile else None
        ready = c.ready_for(self.target.live_session_id, self.target.account) if self.require_send else c.fill_ready_for(self.target.live_session_id, self.target.account)
        if not self.guard(): self.reject("guard_rejected")
        if not ready:
            result = getattr(c.transport, "last_command_result", None) or {}
            # Inspection clears the previous XML/profile before acquiring fresh
            # evidence. Report the observed field even when LIVE authority has
            # expired; diagnostics never authorize another input or Send.
            observed = self._attempted_text is not None and entry is not None
            if observed: self.record_readback(self._attempted_text, entry)
            self.reject("fresh_evidence_rejected", **result,
                post_readback_result="observed_without_authority" if observed else "unavailable",
                classified_screen=state.screen.value, inspection_reason=state.reason,
                account_verified=state.account_verified, live_verified=state.live_verified,
                profile_verified=state.profile_verified,
                readback_target_focused=entry.get("focused") == "true" if entry is not None else None,
                prior_host_evidence_age_seconds=round(monotonic() - host_evidence[3], 3) if host_evidence else None,
                current_host_evidence_present=c._live_evidence is not None)
        if state.serial != serial: self.reject("device_mismatch")
        if entry is None: self.reject("input_missing")
        if focused and entry.get("focused") != "true": self.reject("input_not_focused")
        self.diagnostic["target_focused_before_input"] = entry.get("focused") == "true"
        return entry

    async def clear(self, serial):
        await self.checked(serial, focused=True)
        if not self.require_send and self.inspection: self.inspection.input_started = True
        await self.command(AdbCommand.SELECT_ALL, serial)
        await self.checked(serial, focused=True)
        await self.command(AdbCommand.DELETE, serial)
        entry = await self.checked(serial, focused=True)
        if entry.get("text") != "": self.reject("clear_readback_nonempty", post_readback_result="nonblank")
        self.report(post_readback_result="blank")

    async def prepare_exact(self, serial, resource_id, text):
        c = self.controller
        if not self.supports(text): self.reject("unsupported_text")
        if not c._profile or resource_id != c._profile.input_id: self.reject("input_target_mismatch")
        self.report()
        entry = unique_node(c._xml, resource_id)
        if entry is not None and entry.get("text") == "":
            # The cached field only selects this path. Fresh full inspection and
            # the native guard must still prove the exact empty focused target.
            entry = await self.checked(serial, focused=True)
        if entry is None or entry.get("text") != "":
            await self.clear(serial)
            await self.checked(serial, focused=True)
        if not self.require_send and self.inspection: self.inspection.input_started = True
        self._attempted_text = text
        await self.command(AdbCommand.TEXT, serial, text=text)
        entry = await self.checked(serial, focused=True)
        self.record_readback(text, entry)
        if entry.get("text") == text + " ":
            # Some IMEs add one space after punctuation. Correct that exact
            # observed suffix once; never normalize or accept a mismatched draft.
            self.report(post_readback_result="trailing_space", readback_correction="delete_one_trailing_space")
            entry = await self.checked(serial, focused=True)
            if entry.get("text") == text + " ":
                await self.command(AdbCommand.DELETE, serial)
                entry = await self.checked(serial, focused=True)
            self.record_readback(text, entry)
        if entry.get("text") != text:
            self.report("text_readback_mismatch", post_readback_result="mismatch")
            # Clear only after another full safe focused-field inspection. Never send.
            await self.clear(serial)
            self.reject("text_readback_mismatch", post_readback_result="mismatch")
        self.report(post_readback_result="match")
        self.expected = text
        return entry.get("text")

    async def click_send(self, serial, resource_id, guard):
        c = self.controller
        if not c._profile or resource_id != c._profile.send_id or self.expected is None: return False
        entry = await self.checked(serial, focused=True)
        if entry.get("text") != self.expected: raise TextEntryError()
        point = resolved_send_point(c._xml, c._profile)
        if point is None or not guard(): return False
        if self.before_send: self.before_send()
        # Bounds come ONLY from the unique confirmed reviewed vp8/vpl relationship.
        # The transport repeats the synchronous guard immediately before spawning.
        await c.transport.run(AdbCommand.TAP_SEND, serial=serial, point=point, guard=guard)
        return True

    async def aclose(self):
        self.target = self.expected = self._attempted_text = None
