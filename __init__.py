try:
    from calibre.customize import InterfaceActionBase
except ImportError:
    InterfaceActionBase = object


class TolinoSyncPlugin(InterfaceActionBase):
    name = "Tolino Cloud Sync"
    description = "Synchronize the Calibre library with Tolino Cloud"
    version = (0, 1, 0)
    author = "poesterlin"
    minimum_calibre_version = (5, 0, 0)
    actual_plugin = "ui:TolinoSyncAction"
