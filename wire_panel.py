"""
wire_panel.py

Wire list panel showing all wires in the harness with their properties.
Provides highlighting of wire routes and the ability to add new wires.
"""

from __future__ import annotations

from typing import Optional, List
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QTabWidget,
    QListWidget, QListWidgetItem, QPushButton,
    QLabel, QLineEdit, QDialog, QDialogButtonBox,
    QFormLayout, QComboBox, QDoubleSpinBox,
    QCheckBox, QGroupBox, QScrollArea,
)
from PyQt5.QtCore import Qt, pyqtSignal, QSize
from PyQt5.QtGui import QColor, QPalette, QIcon, QPixmap

from harness_model import Harness, Wire, Node
from harness_controller import HarnessController
from harness_view import HarnessGraphicsView

# --------------------------------------------------------------------------
# Wire List Item Widget
# --------------------------------------------------------------------------

class WireListItemWidget(QWidget):
    """Custom widget for a wire item in the list, showing color swatch and details."""
    
    wire_highlighted = pyqtSignal(str, bool)  # wire_id, highlighted
    
    def __init__(self, wire: Wire, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.wire = wire
        self.highlighted = False
        
        layout = QHBoxLayout(self)
        layout.setContentsMargins(5, 2, 5, 2)
        layout.setSpacing(8)
        
        # Color swatch
        self.color_swatch = QLabel()
        self.color_swatch.setFixedSize(20, 20)
        self._update_color_swatch()
        layout.addWidget(self.color_swatch)
        
        # Wire info
        info_text = f"{wire.wire_id} | {wire.gauge_mm2}mm² | {wire.from_node_id}:{wire.from_pin} → {wire.to_node_id}:{wire.to_pin}"
        self.info_label = QLabel(info_text)
        self.info_label.setWordWrap(True)
        layout.addWidget(self.info_label, 1)
        
        # Highlight toggle button
        self.highlight_btn = QPushButton("🔦")
        self.highlight_btn.setFixedSize(28, 28)
        self.highlight_btn.setCheckable(True)
        self.highlight_btn.setToolTip("Toggle route highlighting")
        self.highlight_btn.toggled.connect(self._on_highlight_toggled)
        layout.addWidget(self.highlight_btn)
        
        self.setLayout(layout)
    
    def _update_color_swatch(self):
        """Update the color swatch based on the wire's color."""
        color = QColor(self.wire.color if self.wire.color else "#808080")
        pixmap = QPixmap(18, 18)
        pixmap.fill(color)
        self.color_swatch.setPixmap(pixmap)
    
    def _on_highlight_toggled(self, checked: bool):
        self.highlighted = checked
        self.wire_highlighted.emit(self.wire.wire_id, checked)
    
    def refresh(self, wire: Wire):
        """Refresh the widget with updated wire data."""
        self.wire = wire
        self._update_color_swatch()
        info_text = f"{wire.wire_id} | {wire.gauge_mm2}mm² | {wire.from_node_id}:{wire.from_pin} → {wire.to_node_id}:{wire.to_pin}"
        self.info_label.setText(info_text)


# --------------------------------------------------------------------------
# Add Wire Dialog
# --------------------------------------------------------------------------

class AddWireDialog(QDialog):
    """Dialog for creating a new wire."""
    
    def __init__(self, view: HarnessGraphicsView, controller: HarnessController, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.view = view
        # self.harness = harness
        self.controller = controller
        self.setWindowTitle("Add New Wire")
        self.setModal(True)
        self.resize(400, 300)
        
        layout = QVBoxLayout(self)
        
        # Form
        form_layout = QFormLayout()
        
        # Wire ID
        self.id_edit = QLineEdit()
        self.id_edit.setPlaceholderText("e.g., W001")
        form_layout.addRow("Wire ID:", self.id_edit)
        
        # Gauge
        self.gauge_spin = QDoubleSpinBox()
        self.gauge_spin.setRange(0.01, 100.0)
        self.gauge_spin.setValue(0.75)
        self.gauge_spin.setSingleStep(0.25)
        self.gauge_spin.setSuffix(" mm²")
        form_layout.addRow("Gauge:", self.gauge_spin)
        
        # Color
        self.color_edit = QLineEdit()
        self.color_edit.setPlaceholderText("e.g., red/black, blue")
        self.color_edit.setText("red/black")
        form_layout.addRow("Color:", self.color_edit)
        
        # From Node
        self.from_node_combo = QComboBox()
        self.from_pin_edit = QLineEdit()
        self.from_pin_edit.setPlaceholderText("Pin number")
        
        from_layout = QHBoxLayout()
        from_layout.addWidget(self.from_node_combo, 2)
        from_layout.addWidget(self.from_pin_edit, 1)
        form_layout.addRow("From:", from_layout)
        
        # To Node
        self.to_node_combo = QComboBox()
        self.to_pin_edit = QLineEdit()
        self.to_pin_edit.setPlaceholderText("Pin number")
        
        to_layout = QHBoxLayout()
        to_layout.addWidget(self.to_node_combo, 2)
        to_layout.addWidget(self.to_pin_edit, 1)
        form_layout.addRow("To:", to_layout)
        
        layout.addLayout(form_layout)
        
        # Route selection (show available edges)
        route_group = QGroupBox("Route (optional)")
        route_layout = QVBoxLayout()
        self.route_checkboxes = []
        if not self.view.harness:
            print(" no harness at innit")
            return 
        for edge in self.view.harness.edges.values():
            cb = QCheckBox(f"{edge.edge_id} ({edge.start_node_id} → {edge.end_node_id})")
            route_layout.addWidget(cb)
            self.route_checkboxes.append(cb)
        route_group.setLayout(route_layout)
        layout.addWidget(route_group)
        
        # Populate node dropdowns
        self._populate_nodes()
        
        # Buttons
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
    
    def _populate_nodes(self):
        """Populate node combo boxes."""
        for node in self.view.harness.nodes.values():
            label = f"{node.node_id} ({node.label})" if node.label else node.node_id
            self.from_node_combo.addItem(label, node.node_id)
            self.to_node_combo.addItem(label, node.node_id)
    
    def get_wire_data(self) -> dict:
        """Get the wire data from the dialog."""
        selected_edges = [
            cb.text().split()[0]  # Extract edge_id from checkbox text
            for cb in self.route_checkboxes
            if cb.isChecked()
        ]
        
        return {
            "wire_id": self.id_edit.text().strip(),
            "gauge_mm2": self.gauge_spin.value(),
            "color": self.color_edit.text().strip(),
            "from_node_id": self.from_node_combo.currentData(),
            "from_pin": self.from_pin_edit.text().strip(),
            "to_node_id": self.to_node_combo.currentData(),
            "to_pin": self.to_pin_edit.text().strip(),
            "route_edge_ids": selected_edges,
        }


# --------------------------------------------------------------------------
# Wire Panel
# --------------------------------------------------------------------------

class WirePanel(QWidget):
    """Panel showing the list of wires with highlighting and add functionality."""
    
    wire_highlighted = pyqtSignal(str, bool)  # wire_id, highlighted
    wire_added = pyqtSignal()  # signal when a new wire is added
    
    def __init__(self, view: HarnessGraphicsView, controller: HarnessController, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.view = view
        # self.harness = harness
        self.controller = controller
        self.wire_items: dict[str, WireListItemWidget] = {}
        
        self.setMinimumWidth(350)
        self.setMaximumWidth(500)
        
        self._setup_ui()
        self._populate_list()
    
    def _setup_ui(self):
        """Set up the user interface."""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(5, 5, 5, 5)
        layout.setSpacing(8)
        
        # Header with count and add button
        header_layout = QHBoxLayout()
        self.count_label = QLabel("Wires: 0")
        self.count_label.setStyleSheet("font-weight: bold;")
        header_layout.addWidget(self.count_label)
        
        header_layout.addStretch()
        
        self.add_button = QPushButton("+ Add Wire")
        self.add_button.setFixedHeight(30)
        self.add_button.setToolTip("Add a new wire to the harness")
        self.add_button.clicked.connect(self._on_add_clicked)
        header_layout.addWidget(self.add_button)
        
        layout.addLayout(header_layout)
        
        # Wire list
        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        
        self.list_widget = QListWidget()
        self.list_widget.setSpacing(2)
        self.list_widget.setSelectionMode(QListWidget.SingleSelection)
        
        self.scroll_area.setWidget(self.list_widget)
        layout.addWidget(self.scroll_area)
        
        # Clear highlights button
        clear_btn = QPushButton("Clear All Highlights")
        clear_btn.clicked.connect(self._clear_all_highlights)
        layout.addWidget(clear_btn)
    
    def _populate_list(self):
        """Populate the wire list from the harness."""
        self.list_widget.clear()
        self.wire_items.clear()
        
        if not self.view.harness:
            print("no harness at populate")
            return
        
        for wire in self.view.harness.wires.values():
            self._add_wire_item(wire)
        
        self.count_label.setText(f"Wires: {len(self.view.harness.wires)}")
    
    def _add_wire_item(self, wire: Wire):
        """Add a single wire to the list."""
        item = QListWidgetItem()
        item.setData(Qt.UserRole, wire.wire_id)
        
        widget = WireListItemWidget(wire)
        widget.wire_highlighted.connect(self._on_wire_highlighted)
        
        self.list_widget.addItem(item)
        self.list_widget.setItemWidget(item, widget)
        self.wire_items[wire.wire_id] = widget
    
    def _on_wire_highlighted(self, wire_id: str, highlighted: bool):
        """Handle wire highlight toggling."""
        self.wire_highlighted.emit(wire_id, highlighted)
        
        # Unhighlight other wires if this one is being highlighted
        if highlighted:
            for wid, widget in self.wire_items.items():
                if wid != wire_id and widget.highlighted:
                    widget.highlight_btn.setChecked(False)
    
    def _clear_all_highlights(self):
        """Clear all wire highlights."""
        for widget in self.wire_items.values():
            if widget.highlighted:
                widget.highlight_btn.setChecked(False)
    
    def _on_add_clicked(self):
        """Open dialog to add a new wire."""
        if not self.view.harness:
            print("no harness on click")
            # return
        dialog = AddWireDialog(self.view, self.controller, self)
        if dialog.exec_() == QDialog.Accepted:
            data = dialog.get_wire_data()
            if not data["wire_id"]:
                return
            
            # Create the wire
            wire = Wire(
                wire_id=data["wire_id"],
                gauge_mm2=data["gauge_mm2"],
                color=data["color"],
                from_node_id=data["from_node_id"],
                from_pin=data["from_pin"],
                to_node_id=data["to_node_id"],
                to_pin=data["to_pin"],
                route_edge_ids=data["route_edge_ids"],
            )
            
            # Add to harness through controller
            self.view.harness.add_wire(wire)
            self._add_wire_item(wire)
            self.count_label.setText(f"Wires: {len(self.view.harness.wires)}")
            self.wire_added.emit()
            
            # Notify model changed
            self.controller.modelChanged.emit("wire", wire.wire_id)
    
    def refresh(self):
        """Refresh the wire list (called when harness changes)."""
        # Update existing items
        for wire_id, widget in self.wire_items.items():
            if wire_id in self.view.harness.wires:
                widget.refresh(self.view.harness.wires[wire_id])
        
        # Check for new wires
        current_ids = set(self.wire_items.keys())
        harness_ids = set(self.view.harness.wires.keys())
        new_ids = harness_ids - current_ids
        for wire_id in new_ids:
            self._add_wire_item(self.view.harness.wires[wire_id])
        
        # Remove wires that no longer exist
        for wire_id in current_ids - harness_ids:
            for i in range(self.list_widget.count()):
                item = self.list_widget.item(i)
                if item.data(Qt.UserRole) == wire_id:
                    self.list_widget.takeItem(i)
                    break
            del self.wire_items[wire_id]
        
        self.count_label.setText(f"Wires: {len(self.view.harness.wires)}")
    
    def highlight_wire(self, wire_id: str, highlighted: bool):
        """Programmatically highlight a wire."""
        if wire_id in self.wire_items:
            widget = self.wire_items[wire_id]
            if widget.highlight_btn.isChecked() != highlighted:
                widget.highlight_btn.setChecked(highlighted)


# --------------------------------------------------------------------------
# Main Panel with Tabs
# --------------------------------------------------------------------------

class HarnessPanel(QTabWidget):
    """Main panel with tabs for different views (Wires, etc.)."""
    
    wire_highlighted = pyqtSignal(str, bool)
    wire_added = pyqtSignal()
    
    def __init__(self, view: HarnessGraphicsView, controller: HarnessController, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.view = view
        self.controller = controller
        
        # Wire tab
        self.wire_panel = WirePanel(self.view, self.controller)
        self.wire_panel.wire_highlighted.connect(self.wire_highlighted.emit)
        self.wire_panel.wire_added.connect(self.wire_added.emit)
        self.addTab(self.wire_panel, "Wires")
        
        # Placeholder for future tabs
        # self.addTab(PlaceholderTab(), "Nodes")
        # self.addTab(PlaceholderTab(), "Edges")
        # self.addTab(PlaceholderTab(), "Properties")
    
    def refresh(self):
        """Refresh all panels."""
        self.wire_panel.refresh()
    def highlight_wire(self, wire_id: str, highlighted: bool):
        """Programmatically highlight a wire."""
        self.wire_panel.highlight_wire(wire_id, highlighted)
