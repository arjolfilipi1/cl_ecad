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
)
from PyQt5.QtGui import QColor
from PyQt5.QtCore import Qt

from harness_model import Harness, Wire


def _swatch_color(color_str: str) -> QColor:
    """Wire colors are often two-tone ('red/black'). Take the first named
    color for the swatch; fall back to grey if it doesn't parse."""
    first = (color_str or "").split("/")[0].strip()
    color = QColor(first)
    return color if color.isValid() else QColor("#888888")


# --------------------------------------------------------------------------
# Wires tab
# --------------------------------------------------------------------------

class WiresTab(QWidget):
    def __init__(self, controller, view, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.controller = controller
        self.view = view

        layout = QVBoxLayout(self)

        self.table = QTableWidget(0, 4, self)
        self.table.setHorizontalHeaderLabels(["Highlight", "From", "To", "Conductor"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        header.setSectionResizeMode(2, QHeaderView.Stretch)
        header.setSectionResizeMode(3, QHeaderView.Stretch)
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
            self.controller.add_wire(wire)


# --------------------------------------------------------------------------
# Add-wire dialog
# --------------------------------------------------------------------------

class AddWireDialog(QDialog):
    def __init__(self, harness: Harness, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.harness = harness
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
        form.addRow("Route (edges):", self.route_list)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel, self)
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)

    def _pick_color(self) -> None:
        color = QColorDialog.getColor(parent=self)
        if color.isValid():
            self.color_edit.setText(color.name())

    def _on_accept(self) -> None:
        wire_id = self.wire_id_edit.text().strip()
        if not wire_id:
            QMessageBox.warning(self, "Missing Wire ID", "Please enter a wire ID.")
            return
        if wire_id in self.harness.wires:
            QMessageBox.warning(self, "Duplicate Wire ID", f"Wire '{wire_id}' already exists.")
            return
        self.accept()

    def get_wire(self) -> Wire:
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
