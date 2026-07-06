"""
main_window.py

Top-level application window. Composes:
- HarnessGraphicsView (the canvas)
- HarnessController (wires the canvas to the Harness model, owns undo/redo)
- HarnessPanel (tabbed panel on the left with wires, nodes, etc.)
- Menu bar (File: Open/Save/Save As, Edit: Undo/Redo)
- Toolbar (Open/Save/Undo/Redo buttons)
- Status bar

This is the file to run:
    python3 main_window.py [optional_path_to_harness.json]
"""

from __future__ import annotations

import sys
from typing import Optional
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QToolBar, QAction, QFileDialog, QMessageBox, QStatusBar,QSplitter, QWidget,
)
from PyQt5.QtGui import QKeySequence,QPen,QColor

from harness_view import HarnessGraphicsView
from harness_controller import HarnessController
from wire_panel import HarnessPanel

class MainWindow(QMainWindow):
    def __init__(self, initial_path: Optional[str] = None):
        super().__init__()
        self.setWindowTitle("Harness Editor")
        self.resize(1200, 700)

        # Create splitter for panel and view
        self.splitter = QSplitter(Qt.Horizontal)
        
        # View (canvas)
        self.view = HarnessGraphicsView(self)
        
        # Controller derives its model reference from the view and keeps
        # itself in sync across reloads (see harness_controller.py).
        self.controller = HarnessController(self.view,self)
        
        # Panel (left side with tabs)
        self.panel = HarnessPanel(self.view, self.controller)
        self.panel.setMaximumWidth(500)
        self.panel.setMinimumWidth(280)
        
        # Connect panel signals
        self.panel.wire_highlighted.connect(self._on_wire_highlighted)
        self.panel.wire_added.connect(self._on_wire_added)
        
        # Add widgets to splitter
        self.splitter.addWidget(self.panel)
        self.splitter.addWidget(self.view)
        self.splitter.setSizes([350, 850])
        
        self.setCentralWidget(self.splitter)

        self.status_bar = QStatusBar(self)
        self.setStatusBar(self.status_bar)

        self._build_menu_and_toolbar()

        if initial_path:
            self.open_path(initial_path)
    def _on_wire_highlighted(self, wire_id: str, highlighted: bool):
        """Handle wire highlighting - light up the route bundles."""
        # Get the wire's route edges
        wire = self.view.harness.wires.get(wire_id)
        print("path:",wire.route_edge_ids)
        if not wire:
            return
        
        # Highlight edges on the view
        for edge_item in self.view.edge_items.values():
            edge_path = edge_item.edge.edge_id in wire.route_edge_ids
            if highlighted and edge_path:
                # Highlight the edge
                edge_item.setPen(QPen(QColor("#FF6B35"), 4))  # Orange highlight
            elif highlighted and not edge_path:
                # Dim non-route edges
                edge_item.setPen(QPen(QColor("#CCCCCC"), 1.5))
            else:
                # Restore normal appearance
                edge_item.update_line()  # This will restore the normal pen

    def _on_wire_added(self):
        """Handle new wire added - refresh view."""
        self.view.render()


    # ---- menu / toolbar ----

    def _build_menu_and_toolbar(self) -> None:
        file_menu = self.menuBar().addMenu("&File")

        open_action = QAction("&Open JSON...", self)
        open_action.setShortcut(QKeySequence.Open)
        open_action.triggered.connect(self.on_open)
        file_menu.addAction(open_action)

        save_action = QAction("&Save JSON", self)
        save_action.setShortcut(QKeySequence.Save)
        save_action.triggered.connect(self.on_save)
        file_menu.addAction(save_action)

        save_as_action = QAction("Save JSON &As...", self)
        save_as_action.setShortcut(QKeySequence.SaveAs)
        save_as_action.triggered.connect(self.on_save_as)
        file_menu.addAction(save_as_action)

        # Undo/redo actions come straight from the QUndoStack. Qt keeps
        # their label ("Undo Move CONN_A") and enabled state in sync
        # automatically — no manual bookkeeping needed here.
        self.undo_action = self.controller.undo_stack.createUndoAction(self, "&Undo")
        self.undo_action.setShortcut(QKeySequence.Undo)
        self.redo_action = self.controller.undo_stack.createRedoAction(self, "&Redo")
        self.redo_action.setShortcut(QKeySequence.Redo)

        edit_menu = self.menuBar().addMenu("&Edit")
        edit_menu.addAction(self.undo_action)
        edit_menu.addAction(self.redo_action)

        toolbar = QToolBar("Main", self)
        self.addToolBar(toolbar)
        toolbar.addAction(open_action)
        toolbar.addAction(save_action)
        toolbar.addSeparator()
        toolbar.addAction(self.undo_action)
        toolbar.addAction(self.redo_action)

    # ---- file actions ----

    def on_open(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Open Harness JSON", "", "JSON Files (*.json)")
        if path:
            self.open_path(path)

    def on_save(self) -> None:
        if self.view.harness is None:
            return
        if self.view.current_path is None:
            self.on_save_as()
            return
        self.save_path(self.view.current_path)

    def on_save_as(self) -> None:
        if self.view.harness is None:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Save Harness JSON", "", "JSON Files (*.json)")
        if path:
            self.save_path(path)

    def open_path(self, path: str) -> None:
        try:
            self.view.load_json(path)
            # Update panel with new harness data
            self.panel.view = self.view
            print("loaded ",self.panel.view.harness)
            self.panel.refresh()

        except Exception as exc:
            QMessageBox.critical(self, "Load failed", f"Could not load '{path}':\n{exc}")
            return
        h = self.view.harness
        self.status_bar.showMessage(
            f"Loaded '{path}': {len(h.nodes)} nodes, {len(h.edges)} edges, {len(h.wires)} wires"
        )
        self.setWindowTitle(f"Harness Editor — {path}")

    def save_path(self, path: str) -> None:
        try:
            self.view.save_json(path)
        except Exception as exc:
            QMessageBox.critical(self, "Save failed", f"Could not save '{path}':\n{exc}")
            return
        self.status_bar.showMessage(f"Saved '{path}'")
        self.setWindowTitle(f"Harness Editor — {path}")


def main():
    app = QApplication(sys.argv)
    initial_path = sys.argv[1] if len(sys.argv) > 1 else None
    window = MainWindow(initial_path)
    window.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
