import os
from PyQt5.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, 
    QLineEdit, QPushButton, QDialogButtonBox, QFileDialog,QCheckBox
)
from PyQt5.QtCore import QSettings

class SettingsDialog(QDialog):
    """A skeleton dialog for application settings."""
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.resize(400, 150)
        
        # Initialize QSettings (Replace with your actual organization and app name)
        self.settings = QSettings("Arjol", "HarnessApp")
        
        self._setup_ui()
        self._load_settings()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)

        # --- Save Location Setting ---
        save_layout = QHBoxLayout()
        self.path_label = QLabel("Default Save Location:")
        self.path_input = QLineEdit()
        self.path_input.setReadOnly(True) # Force users to use the browse button
        
        self.browse_btn = QPushButton("Browse...")
        self.browse_btn.clicked.connect(self._browse_directory)
        
        save_layout.addWidget(self.path_label)
        save_layout.addWidget(self.path_input)
        save_layout.addWidget(self.browse_btn)
        
        layout.addLayout(save_layout)
        
        layout.addStretch() # Push everything to the top
        # --- Grid Toggle Setting ---
        grid_layout = QHBoxLayout()
        self.grid_checkbox = QCheckBox("Enable Background CAD Grid")
        grid_layout.addWidget(self.grid_checkbox)
        layout.addLayout(grid_layout)
        
        layout.addStretch()
        # --- Dialog Buttons ---
        self.button_box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.button_box.accepted.connect(self._save_settings)
        self.button_box.rejected.connect(self.reject)
        layout.addWidget(self.button_box)

    def _browse_directory(self) -> None:
        """Opens a file dialog to select a new save directory."""
        current_path = self.path_input.text()
        if not os.path.exists(current_path):
            current_path = os.path.expanduser("~")
            
        directory = QFileDialog.getExistingDirectory(self, "Select Save Location", current_path)
        if directory:
            self.path_input.setText(directory)

    def _load_settings(self) -> None:
        """Populates the UI with saved settings."""
        # Default to the user's home directory if no setting exists
        default_path = os.path.expanduser("~")
        saved_path = self.settings.value("default_save_location", default_path)
        self.path_input.setText(saved_path)
        # Load grid visibility flag (defaults to True)
        show_grid = self.settings.value("show_grid", True, type=bool)
        self.grid_checkbox.setChecked(show_grid)
    def _save_settings(self) -> None:
        """Saves the UI values back to QSettings and closes the dialog."""
        self.settings.setValue("default_save_location", self.path_input.text())
        self.settings.setValue("show_grid", self.grid_checkbox.isChecked())
        self.accept()