try:
    from calibre.customize import InterfaceActionBase
except ImportError:
    InterfaceActionBase = object


PLUGIN_VERSION = (0, 9, 38)


class TolinoSyncPlugin(InterfaceActionBase):
    name = "Tolino Cloud Sync"
    description = "Synchronisiert die Calibre-Bibliothek mit der Tolino Cloud"
    version = PLUGIN_VERSION
    author = "moritzkeller12-beep"
    type = "InterfaceAction"
    supported_platforms = ["linux", "windows", "osx"]
    minimum_calibre_version = (5, 0, 0)
    load_on_demand = True
    actual_plugin = "calibre_plugins.tolino_cloud_sync.ui:TolinoSyncAction"
    icon = "images/tolino_cloud_sync.png"
    has_html = False
