"""Einstellungsdialog für Übertragungsoptionen und verknüpfte Konten."""
from .ui_common import (QDialog, QFormLayout, QGroupBox, QHBoxLayout,
                        QPushButton, QVBoxLayout)


class SettingsDialog(QDialog):
    """Bündelt Übertragungsoptionen und Kontoverwaltung."""

    def __init__(self, dashboard):
        QDialog.__init__(self, dashboard)
        self.dashboard = dashboard
        self.setWindowTitle("Einstellungen")
        self.setMinimumWidth(620)
        layout = QVBoxLayout(self)

        self.transfer = QGroupBox("Einstellungen")
        self.transfer_form = QFormLayout(self.transfer)
        layout.addWidget(self.transfer)

        self.accounts = QGroupBox("Konten und Verbindung")
        self.accounts_form = QFormLayout(self.accounts)
        layout.addWidget(self.accounts)

        self.close_button = QPushButton("Speichern und schließen")
        self.close_button.setToolTip(
            "Speichert die Änderungen und schließt die Einstellungen.")
        self.close_button.clicked.connect(self.dashboard.close_settings)
        bottom = QHBoxLayout()
        bottom.addStretch(1)
        bottom.addWidget(self.close_button)
        layout.addLayout(bottom)
