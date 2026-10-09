"""Select platform I/O without changing the comment source or AI pipeline."""


def create_delivery_service(settings, suggestions, context, *, serial=None):
    if settings.tiktok_transport == "windows_live_studio":
        from app.windows.service import WindowsManualDeliveryService
        return WindowsManualDeliveryService(settings, suggestions, context)
    if settings.tiktok_transport != "android":
        raise ValueError("Unsupported delivery transport")
    from app.delivery.service import ManualDeliveryService
    return ManualDeliveryService(settings, suggestions, context, serial=serial)
