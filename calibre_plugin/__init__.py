try:
    from calibre.customize import InterfaceActionBase
except ImportError:  # Allows deterministic helper tests outside a Calibre install.
    InterfaceActionBase = object


class TolinoSyncPlugin(InterfaceActionBase):
    name = "Tolino Cloud Sync"
    description = "Synchronize the Calibre library with Tolino Cloud"
    version = (0, 9, 22)
    author = "moritzkeller12-beep"
    type = "InterfaceAction"
    supported_platforms = ["linux", "windows", "osx"]
    minimum_calibre_version = (5, 0, 0)
    load_on_demand = True
    actual_plugin = "calibre_plugins.tolino_cloud_sync.ui:TolinoSyncAction"
    icon = "images/tolino_cloud_sync.png"
    has_html = False
    
    def initialize(self):
        self.actual_plugin_ = self.load_actual_plugin(self.actual_plugin)
