try:
    from calibre.customize import InterfaceActionBase
except ImportError:
    InterfaceActionBase = object


class TolinoSyncPlugin(InterfaceActionBase):
    name = "Tolino Cloud Sync"
    description = "Synchronize the Calibre library with Tolino Cloud"
    version = (0, 8, 1)
    author = "moritzkeller12-beep"
    type = "InterfaceAction"
    supported_platforms = ["linux", "windows", "osx"]
    minimum_calibre_version = (5, 0, 0)
    load_on_demand = True
    actual_plugin = "calibre_plugins.tolino_cloud_sync.ui:TolinoSyncAction"
    icon = "images/tolino_cloud_sync.png"
    has_html = False
