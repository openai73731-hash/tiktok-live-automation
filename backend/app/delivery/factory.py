"""Select platform I/O without changing the comment source or AI pipeline."""


def create_delivery_service(settings, suggestions, context, *, serial=None):
    from app.delivery.registry import UnifiedDeliveryOperationStore
    journal_factory = lambda: UnifiedDeliveryOperationStore(settings.state_dir)
    if settings.tiktok_transport == "windows_live_studio":
        from app.windows.service import WindowsManualDeliveryService
        return WindowsManualDeliveryService(settings, suggestions, context, journal_factory=journal_factory,
            journal_path=settings.state_dir / "unified-delivery.sqlite3")
    if settings.tiktok_transport != "android":
        raise ValueError("Unsupported delivery transport")
    from app.delivery.service import ManualDeliveryService
    if suggestions is not None: suggestions.auto_registration_epoch = lambda envelope: None
    return ManualDeliveryService(settings, suggestions, context, serial=serial, journal_factory=journal_factory)
