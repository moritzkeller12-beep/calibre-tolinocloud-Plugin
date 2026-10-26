try:
    from calibre.customize import InterfaceActionBase
except ImportError:
    InterfaceActionBase = object


PLUGIN_VERSION = (0, 1, 2)

# GPL-3.0-or-later: siehe LICENSE (kompatibel zu Calibre, das unter GPL-3 steht).
__license__ = "GPL-3.0-or-later"


class TolinoSyncPlugin(InterfaceActionBase):
    name = "Tolino Cloud Sync"
    description = "Gleicht die Calibre-Bibliothek mit der tolino Cloud ab"
    version = PLUGIN_VERSION
    author = "moritzkeller12-beep"
    type = "InterfaceAction"
    supported_platforms = ["linux", "windows", "osx"]
    minimum_calibre_version = (5, 0, 0)
    load_on_demand = True
    actual_plugin = "calibre_plugins.tolino_cloud_sync.ui:TolinoSyncAction"
