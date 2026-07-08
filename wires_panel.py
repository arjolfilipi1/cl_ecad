"""
wires_panel.py

Left-side panel content. Currently a single tab ("Wires") inside a
QTabWidget — more tabs (Nodes, Edges, BOM, ...) can be added later by
adding more widgets to the same QTabWidget in main_window.py.

WiresTab:
- One row per Wire: a highlight toggle, From, To, and a conductor
  description (color swatch + gauge/color text).
- Toggling "Highlight" lights up the edges (bundle segments) that wire's
  route passes through, in the HarnessGraphicsView.
- "+ Add Wire" opens AddWireDialog and, on accept, commits the new wire
  through the controller (undoable).

This file only talks to the Harness model through HarnessController — it
never mutates harness.wires directly.
"""

from __future__ import annotations

from typing import Optional

from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QFormLayout,
    QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView,
    QCheckBox, QPushButton, QLabel, QLineEdit, QComboBox, QDoubleSpinBox,
    QListWidget, QListWidgetItem, QDialog, QDialogButtonBox, QColorDialog, QMessageBox,
    QSpinBox,
)
from PyQt5.QtGui import QColor
from PyQt5.QtCore import Qt

from harness_model import Harness, Wire, Edge


def _swatch_color(color_str: str) -> QColor:
    """Wire colors are often two-tone ('red/black'). Take the first named
    color for the swatch; fall back to grey if it doesn't parse."""
    first = (color_str or "").split("/")[0].strip()
    color = QColor(first)
    return color if color.isValid() else QColor("#888888")
class EdgesTab(QWidget):
    """Tab displaying all edges (bundle segments) in the harness.
    
    Shows: Edge ID, Start Node, End Node, Length (mm).
    Edges are read-only in this stage (no add/delete UI yet).
    """
    
    def __init__(self, controller, view, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.controller = controller
        self.view = view
        
        layout = QVBoxLayout(self)
        
        self.table = QTableWidget(0, 4, self)
        self.table.setHorizontalHeaderLabels(["ID", "Start", "End", "Length (mm)"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.cellDoubleClicked.connect(self.on_cell_double_clicked)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        header.setSectionResizeMode(2, QHeaderView.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        
        layout.addWidget(self.table)
        
        # Keep the table in sync with the model
        self.controller.edgeListChanged.connect(self.refresh)
        self.controller.modelChanged.connect(self._on_model_changed)
        self.view.sceneRebuilt.connect(self.refresh)  # a fresh file was loaded
        
        self.refresh()
    def on_cell_double_clicked(self, row: int, column: int) -> None:
        """Handle double-click on an edge row - open edit dialog."""
        harness = self.controller.harness
        if harness is None:
            return
        
        # Get the edge ID from the row
        edge_ids = list(harness.edges.keys())
        if row >= len(edge_ids):
            return
        
        edge_id = edge_ids[row]
        edge = harness.edges.get(edge_id)
        if edge is None:
            return
        
        # Open edit dialog
        dialog = EditEdgeDialog(edge, harness, self)
        if dialog.exec() == QDialog.Accepted:
            updated_values = dialog.get_updated_values()
            # Apply changes using the controller
            for field_name, new_value in updated_values.items():
                # Skip if value hasn't changed
                old_value = getattr(edge, field_name)
                if old_value != new_value:
                    self.controller.set_edge_field(edge_id, field_name, new_value)

    def _on_model_changed(self, entity_kind: str, entity_id: str) -> None:
        if entity_kind == "edge":
            self.refresh()
    
    def refresh(self) -> None:
        self.table.setRowCount(0)
        harness = self.controller.harness
        if harness is None:
            return
        
        for edge in harness.edges.values():
            self._add_row(edge)
    
    def _add_row(self, edge: Edge) -> None:
        row = self.table.rowCount()
        self.table.insertRow(row)
        
        # Edge ID
        self.table.setItem(row, 0, QTableWidgetItem(edge.edge_id))
        
        # Start Node
        start_node = self.controller.harness.nodes.get(edge.start_node_id)
        start_label = f"{edge.start_node_id}"
        if start_node and start_node.label:
            start_label += f" ({start_node.label})"
        self.table.setItem(row, 1, QTableWidgetItem(start_label))
        
        # End Node
        end_node = self.controller.harness.nodes.get(edge.end_node_id)
        end_label = f"{edge.end_node_id}"
        if end_node and end_node.label:
            end_label += f" ({end_node.label})"
        self.table.setItem(row, 2, QTableWidgetItem(end_label))
        
        # Length
        length_item = QTableWidgetItem(f"{edge.length_mm:.1f}" if edge.length_mm else "")
        length_item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.table.setItem(row, 3, length_item)


# --------------------------------------------------------------------------
# Wires tab
# --------------------------------------------------------------------------

class WiresTab(QWidget):
    def __init__(self, controller, view, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.controller = controller
        self.view = view

        layout = QVBoxLayout(self)

        self.table = QTableWidget(0, 5, self)
        self.table.setHorizontalHeaderLabels(["Highlight", "From", "To", "Conductor", "Route"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.cellDoubleClicked.connect(self.on_cell_double_clicked)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        header.setSectionResizeMode(2, QHeaderView.Stretch)
        header.setSectionResizeMode(3, QHeaderView.Stretch)
        header.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        layout.addWidget(self.table)

        self.add_button = QPushButton("+ Add Wire", self)
        self.add_button.clicked.connect(self.on_add_wire)
        layout.addWidget(self.add_button)

        # Keep the table in sync with the model, however it changes.
        self.controller.wireListChanged.connect(self.refresh)
        self.controller.modelChanged.connect(self._on_model_changed)
        self.view.sceneRebuilt.connect(self.refresh)  # a fresh file was loaded

        self.refresh()

    # ---- keeping the table in sync ----
    def on_cell_double_clicked(self, row: int, column: int) -> None:
        """Handle double-click on a wire row - open edit dialog."""
        harness = self.controller.harness
        if harness is None:
            return
        
        # Get the wire ID from the row
        # The row corresponds to the wire's position in the table
        # We need to find the wire by matching the row index
        wire_ids = list(harness.wires.keys())
        if row >= len(wire_ids):
            return
        
        wire_id = wire_ids[row]
        wire = harness.wires.get(wire_id)
        if wire is None:
            return
        
        # Open edit dialog
        dialog = AddWireDialog(harness, self, existing_wire=wire)
        if dialog.exec() == QDialog.Accepted:
            new_wire = dialog.get_wire()
            # Apply changes field by field using the controller
            if new_wire.gauge_mm2 != wire.gauge_mm2:
                self.controller.set_wire_field(wire_id, "gauge_mm2", new_wire.gauge_mm2)
            if new_wire.color != wire.color:
                self.controller.set_wire_field(wire_id, "color", new_wire.color)
            if new_wire.from_node_id != wire.from_node_id:
                self.controller.set_wire_field(wire_id, "from_node_id", new_wire.from_node_id)
            if new_wire.from_pin != wire.from_pin:
                self.controller.set_wire_field(wire_id, "from_pin", new_wire.from_pin)
            if new_wire.to_node_id != wire.to_node_id:
                self.controller.set_wire_field(wire_id, "to_node_id", new_wire.to_node_id)
            if new_wire.to_pin != wire.to_pin:
                self.controller.set_wire_field(wire_id, "to_pin", new_wire.to_pin)
            if new_wire.route_edge_ids != wire.route_edge_ids:
                self.controller.set_wire_field(wire_id, "route_edge_ids", new_wire.route_edge_ids)

    def _on_model_changed(self, entity_kind: str, entity_id: str) -> None:
        if entity_kind == "wire":
            self.refresh()

    def refresh(self) -> None:
        self.table.setRowCount(0)
        harness = self.controller.harness
        if harness is None:
            return
        for wire in harness.wires.values():
            self._add_row(wire)

    def _add_row(self, wire: Wire) -> None:
        row = self.table.rowCount()
        self.table.insertRow(row)

        # --- Highlight toggle, centered in its cell ---
        toggle = QCheckBox()
        toggle.setChecked(wire.wire_id in self.view.highlighted_wire_ids)
        toggle.toggled.connect(lambda checked, wid=wire.wire_id: self.on_toggle_highlight(wid, checked))
        toggle_holder = QWidget()
        toggle_layout = QHBoxLayout(toggle_holder)
        toggle_layout.addWidget(toggle)
        toggle_layout.setAlignment(Qt.AlignCenter)
        toggle_layout.setContentsMargins(0, 0, 0, 0)
        self.table.setCellWidget(row, 0, toggle_holder)

        # --- From / To ---
        self.table.setItem(row, 1, QTableWidgetItem(self._describe_endpoint(wire.from_node_id, wire.from_pin)))
        self.table.setItem(row, 2, QTableWidgetItem(self._describe_endpoint(wire.to_node_id, wire.to_pin)))

        # --- Conductor: color square + gauge/color text ---
        conductor_widget = QWidget()
        c_layout = QHBoxLayout(conductor_widget)
        c_layout.setContentsMargins(4, 2, 4, 2)
        swatch = QLabel()
        swatch.setFixedSize(14, 14)
        swatch.setStyleSheet(
            f"background-color: {_swatch_color(wire.color).name()}; border: 1px solid #333;"
        )
        text = QLabel(f"{wire.gauge_mm2:g} mm²  {wire.color}")
        c_layout.addWidget(swatch)
        c_layout.addWidget(text)
        c_layout.addStretch()
        self.table.setCellWidget(row, 3, conductor_widget)

        # --- Route: (re)assign this wire's edges via Dijkstra ---
        route_button = QPushButton("Auto-Route")
        route_button.setToolTip(
            "Assign this wire's path using the shortest chain of existing edges.\n"
            "If no path exists between its nodes, a direct segment is created."
        )
        route_button.clicked.connect(lambda _checked, wid=wire.wire_id: self.controller.auto_route_wire(wid))
        self.table.setCellWidget(row, 4, route_button)

    def _describe_endpoint(self, node_id: str, pin: str) -> str:
        harness = self.controller.harness
        node = harness.nodes.get(node_id) if harness else None
        label = f" ({node.label})" if node and node.label else ""
        return f"{node_id}{label} / pin {pin}"

    # ---- highlight toggling ----

    def on_toggle_highlight(self, wire_id: str, checked: bool) -> None:
        self.view.set_wire_highlighted(wire_id, checked)

    # ---- add wire ----

    def on_add_wire(self) -> None:
        harness = self.controller.harness
        if harness is None:
            QMessageBox.information(self, "No harness loaded", "Open a harness JSON file first.")
            return
        if not harness.nodes:
            QMessageBox.information(self, "No nodes", "This harness has no nodes to connect a wire between.")
            return

        dialog = AddWireDialog(harness, self)
        if dialog.exec_() == QDialog.Accepted:
            wire = dialog.get_wire()
            if dialog.auto_route_requested():
                self.controller.add_wire_auto_route(wire)
            else:
                self.controller.add_wire(wire)


# --------------------------------------------------------------------------
# Add-wire dialog
# --------------------------------------------------------------------------

class AddWireDialog(QDialog):
    def __init__(self, harness: Harness, parent: Optional[QWidget] = None,existing_wire: Optional[Wire] = None):
        super().__init__(parent)
        self.harness = harness
        self.existing_wire = existing_wire
        if existing_wire:
            self.setWindowTitle(f"Edit Wire: {existing_wire.wire_id}")
        else:
            self.setWindowTitle("Add Wire")

        self.setMinimumWidth(380)

        form = QFormLayout()

        self.wire_id_edit = QLineEdit(self)
        form.addRow("Wire ID:", self.wire_id_edit)

        self.gauge_spin = QDoubleSpinBox(self)
        self.gauge_spin.setRange(0.05, 100.0)
        self.gauge_spin.setDecimals(2)
        self.gauge_spin.setSuffix(" mm\u00b2")
        self.gauge_spin.setValue(0.75)
        form.addRow("Gauge:", self.gauge_spin)

        color_row = QHBoxLayout()
        self.color_edit = QLineEdit(self)
        self.color_edit.setPlaceholderText("e.g. red, red/black, #FF0000")
        pick_button = QPushButton("Pick...", self)
        pick_button.clicked.connect(self._pick_color)
        color_row.addWidget(self.color_edit)
        color_row.addWidget(pick_button)
        form.addRow("Color:", color_row)

        self.from_node_combo = QComboBox(self)
        self.to_node_combo = QComboBox(self)
        for node_id, node in harness.nodes.items():
            display = f"{node_id} ({node.label})" if node.label else node_id
            self.from_node_combo.addItem(display, node_id)
            self.to_node_combo.addItem(display, node_id)
        form.addRow("From node:", self.from_node_combo)

        self.from_pin_edit = QLineEdit(self)
        form.addRow("From pin:", self.from_pin_edit)

        form.addRow("To node:", self.to_node_combo)

        self.to_pin_edit = QLineEdit(self)
        form.addRow("To pin:", self.to_pin_edit)

        self.route_list = QListWidget(self)
        self.route_list.setSelectionMode(QAbstractItemView.NoSelection)
        for edge_id in harness.edges.keys():
            item = QListWidgetItem(edge_id)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Unchecked)
            self.route_list.addItem(item)

        self.auto_route_checkbox = QCheckBox(
            "Auto-route with Dijkstra (creates a direct segment if no path exists)", self
        )
        self.auto_route_checkbox.setChecked(True)
        self.auto_route_checkbox.toggled.connect(lambda checked: self.route_list.setEnabled(not checked))
        self.route_list.setEnabled(False)  # matches the checkbox's default checked state

        form.addRow(self.auto_route_checkbox)
        form.addRow("Manual route (edges):", self.route_list)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel, self)
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)
        if existing_wire:
            self._load_existing_wire(existing_wire)
    def _load_existing_wire(self, wire: Wire) -> None:
        """Populate form fields from an existing wire."""
        self.wire_id_edit.setText(wire.wire_id)
        self.wire_id_edit.setReadOnly(True)  # Don't allow ID changes
        self.gauge_spin.setValue(wire.gauge_mm2)
        self.color_edit.setText(wire.color)
        
        # Set from node
        index = self.from_node_combo.findData(wire.from_node_id)
        if index >= 0:
            self.from_node_combo.setCurrentIndex(index)
        self.from_pin_edit.setText(wire.from_pin)
        
        # Set to node
        index = self.to_node_combo.findData(wire.to_node_id)
        if index >= 0:
            self.to_node_combo.setCurrentIndex(index)
        self.to_pin_edit.setText(wire.to_pin)
        
        # Set route checkboxes
        for i in range(self.route_list.count()):
            item = self.route_list.item(i)
            if item.text() in wire.route_edge_ids:
                item.setCheckState(Qt.Checked)
            else:
                item.setCheckState(Qt.Unchecked)
        
        # Disable auto-route if editing (we want to preserve the route)
        self.auto_route_checkbox.setChecked(False)
        self.auto_route_checkbox.setEnabled(False)
        self.route_list.setEnabled(True)

    def _pick_color(self) -> None:
        color = QColorDialog.getColor(parent=self)
        if color.isValid():
            self.color_edit.setText(color.name())

    def _on_accept(self) -> None:
        wire_id = self.wire_id_edit.text().strip()
        if not wire_id:
            QMessageBox.warning(self, "Missing Wire ID", "Please enter a wire ID.")
            return
        # Only check for duplicates if this is a new wire
        if not self.existing_wire and wire_id in self.harness.wires:
            QMessageBox.warning(self, "Duplicate Wire ID", f"Wire '{wire_id}' already exists.")
            return
        self.accept()


    def auto_route_requested(self) -> bool:
        return self.auto_route_checkbox.isChecked()

    def get_wire(self) -> Wire:
        if self.auto_route_checkbox.isChecked():
            route_edge_ids = []  # the controller computes this via Dijkstra after accept
        else:
            route_edge_ids = [
                self.route_list.item(i).text()
                for i in range(self.route_list.count())
                if self.route_list.item(i).checkState() == Qt.Checked
            ]
        return Wire(
            wire_id=self.wire_id_edit.text().strip(),
            gauge_mm2=self.gauge_spin.value(),
            color=self.color_edit.text().strip() or "unspecified",
            from_node_id=self.from_node_combo.currentData(),
            from_pin=self.from_pin_edit.text().strip(),
            to_node_id=self.to_node_combo.currentData(),
            to_pin=self.to_pin_edit.text().strip(),
            route_edge_ids=route_edge_ids,
        )
class EditEdgeDialog(QDialog):
    """Dialog for editing an existing Edge."""
    
    def __init__(self, edge: Edge, harness: Harness, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.edge = edge
        self.harness = harness
        self.setWindowTitle(f"Edit Edge: {edge.edge_id}")
        self.setMinimumWidth(400)
        
        form = QFormLayout()
        
        # Edge ID (read-only)
        self.edge_id_label = QLabel(edge.edge_id, self)
        form.addRow("Edge ID:", self.edge_id_label)
        
        # Start Node (read-only - changing would break routing)
        start_node = harness.nodes.get(edge.start_node_id)
        start_text = f"{edge.start_node_id}"
        if start_node and start_node.label:
            start_text += f" ({start_node.label})"
        self.start_label = QLabel(start_text, self)
        form.addRow("Start Node:", self.start_label)
        
        # End Node (read-only - changing would break routing)
        end_node = harness.nodes.get(edge.end_node_id)
        end_text = f"{edge.end_node_id}"
        if end_node and end_node.label:
            end_text += f" ({end_node.label})"
        self.end_label = QLabel(end_text, self)
        form.addRow("End Node:", self.end_label)
        
        # Length (editable)
        self.length_spin = QDoubleSpinBox(self)
        self.length_spin.setRange(0.0, 10000.0)
        self.length_spin.setDecimals(1)
        self.length_spin.setSuffix(" mm")
        self.length_spin.setValue(edge.length_mm)
        form.addRow("Length (mm):", self.length_spin)
        
        # Length (editable)
        self.locked_spin = QSpinBox(self)
        self.locked_spin.setRange(0, 1)
        self.locked_spin.setValue(edge.length_locked)
        form.addRow("Is length locked:", self.locked_spin)
        
        # Max Diameter (editable)
        self.diameter_spin = QDoubleSpinBox(self)
        self.diameter_spin.setRange(0.0, 1000.0)
        self.diameter_spin.setDecimals(1)
        self.diameter_spin.setSuffix(" mm")
        self.diameter_spin.setValue(edge.max_diameter_mm)
        form.addRow("Max Diameter:", self.diameter_spin)
        
        # Bend Radius (editable)
        self.bend_spin = QDoubleSpinBox(self)
        self.bend_spin.setRange(0.0, 1000.0)
        self.bend_spin.setDecimals(1)
        self.bend_spin.setSuffix(" mm")
        self.bend_spin.setValue(edge.bend_radius_mm)
        form.addRow("Bend Radius:", self.bend_spin)
        
        # Buttons
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel, self)
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)
    
    def _on_accept(self) -> None:
        # Validate length
        if self.length_spin.value() <= 0:
            reply = QMessageBox.question(
                self, "Zero Length",
                "Length is 0. Is this correct?",
                QMessageBox.Yes | QMessageBox.No
            )
            if reply == QMessageBox.No:
                return
        self.accept()
    
    def get_updated_values(self) -> dict:
        """Return the updated field values."""
        return {
            "length_mm": self.length_spin.value(),
            "max_diameter_mm": self.diameter_spin.value(),
            "bend_radius_mm": self.bend_spin.value(),
            "length_locked": self.locked_spin.value(),
        }
