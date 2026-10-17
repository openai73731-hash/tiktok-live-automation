"""Fixed local safe overlay. CLI/project/knowledge/security are never edited."""
import os
from pathlib import Path
import tempfile
import stat
from typing import Literal

import yaml
from pydantic import Field, field_validator

from app.ai.knowledge import ConfigModel, ProjectConfig
from app.config import Settings
from app.ai.profile import AIProfile


class LocalConfigError(ValueError):
    pass


class TikTokOverride(ConfigModel):
    username: str | None = Field(default=None, strict=True)
    transport: Literal["android", "windows_live_studio"] | None = None

    @field_validator("username")
    @classmethod
    def handle(cls, value):
        if value is None:
            return None
        return Settings.normalize_username(value)


class AIOverride(ConfigModel):
    provider: Literal["fake", "lmstudio"] | None = None
    model: str | None = Field(default=None, min_length=1, max_length=100,
                              pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$", strict=True)
    temperature: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False, strict=True)
    timeout_seconds: float | None = Field(default=None, ge=1, le=120, allow_inf_nan=False, strict=True)


class ReplyOverride(ConfigModel):
    mode: Literal["focused", "friendly"] | None = None
    max_sentences: int | None = Field(default=None, ge=1, le=3, strict=True)
    max_chars: int | None = Field(default=None, ge=40, le=400, strict=True)


class AndroidOverride(ConfigModel):
    device_serial: str | None = Field(default=None, strict=True, min_length=1, max_length=100,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")


class OperatorOverlay(ConfigModel):
    tiktok: TikTokOverride = Field(default_factory=TikTokOverride)
    ai: AIOverride = Field(default_factory=AIOverride)
    reply: ReplyOverride = Field(default_factory=ReplyOverride)
    android: AndroidOverride = Field(default_factory=AndroidOverride)
    ai_profile: AIProfile | None = None


class StrictLoader(yaml.SafeLoader):
    """Reject duplicate keys rather than silently discarding earlier settings."""

    def __init__(self, stream):
        super().__init__(stream)
        self.depth = self.nodes = 0

    def compose_node(self, parent, index):
        self.depth += 1
        self.nodes += 1
        try:
            if self.depth > 12 or self.nodes > 256 or self.check_event(yaml.AliasEvent):
                raise LocalConfigError("Invalid local settings")
            return super().compose_node(parent, index)
        finally:
            self.depth -= 1


def mapping(loader, node):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node)
        if not isinstance(key, str) or key in result:
            raise LocalConfigError("Invalid local settings")
        result[key] = loader.construct_object(value_node)
    return result


StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, mapping)


def effective(base: ProjectConfig, overlay: OperatorOverlay) -> ProjectConfig:
    values = base.model_dump()
    values["ai"].update(overlay.ai.model_dump(exclude_none=True))
    names = {"mode": "reply_policy", "max_sentences": "max_reply_sentences", "max_chars": "max_reply_chars"}
    values["ai"].update({names[key]: value for key, value in overlay.reply.model_dump(exclude_none=True).items()})
    if overlay.ai_profile is not None: values["ai_profile"] = overlay.ai_profile.model_dump()
    if values.get("ai_profile"):
        profile = AIProfile.model_validate(values["ai_profile"])
        values["ai"].update(provider="lmstudio", model=profile.model.model,
            lmstudio_base_url=profile.model.base_url, temperature=profile.model.temperature,
            max_model_output_tokens=profile.model.max_tokens)
        values['stream']['language'] = profile.language
        values['product'] = dict(name=profile.name, platform='TikTok LIVE')
    return ProjectConfig.model_validate(values)


class OperatorStore:
    def __init__(self, base: ProjectConfig, path: Path):
        self.base = base
        self.path = path

    def validate(self, overlay: OperatorOverlay) -> ProjectConfig:
        return effective(self.base, overlay)

    def guard_path(self):
        # Detect existing links/junctions on Windows and POSIX, including parents.
        # The containing directory must still be trusted against concurrent replacement.
        for path in (self.path, *self.path.absolute().parents):
            try:
                info = path.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise LocalConfigError("Invalid local settings; check operator.local.yaml")

    def load(self) -> OperatorOverlay:
        try:
            self.guard_path()
            with self.path.open("rb") as stream:
                data_bytes = stream.read(16_385)
            if len(data_bytes) > 16_384:
                raise LocalConfigError("Invalid local settings")
            data = yaml.load(data_bytes.decode("utf-8-sig"), Loader=StrictLoader)
            if not isinstance(data, dict):
                raise LocalConfigError("Invalid local settings")
            overlay = OperatorOverlay.model_validate(data)
            self.validate(overlay)
            return overlay
        except FileNotFoundError:
            return OperatorOverlay()
        except (ValueError, TypeError, yaml.YAMLError, OSError, RecursionError):
            raise LocalConfigError("Invalid local settings; check operator.local.yaml") from None

    def save(self, overlay: OperatorOverlay) -> None:
        self.guard_path()
        self.validate(overlay)
        data = yaml.safe_dump(overlay.model_dump(exclude_none=True), allow_unicode=True, sort_keys=False)
        if len(data.encode("utf-8")) > 16_384: raise LocalConfigError("Local settings exceed 16 KiB")
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent,
                                            prefix=".operator-", suffix=".tmp", delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            self.guard_path()
            os.replace(temporary, self.path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def reset(self) -> None:
        self.guard_path()
        self.path.unlink(missing_ok=True)
