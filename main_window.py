"""
main_window.py

Top-level application window. Composes:
- HarnessGraphicsView (the canvas)
- HarnessController (wires the canvas to the Harness model, owns undo/redo)
- Menu bar (File: Open/Save/Save As, Edit: Undo/Redo)
- Toolbar (Open/Save/Undo/Redo buttons)
- Status bar

This is the file to run:
    python3 main_window.py [optional_path_to_harness.json]
"""

from __future__ import annotations

import sys
from typing import Optional

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QToolBar, QAction, QFileDialog, QMessageBox, QStatusBar,
    QSplitter, QTabWidget,
)
from PyQt5.QtGui import QKeySequence
from PyQt5.QtCore import Qt

from harness_view import HarnessGraphicsView
from harness_controller import HarnessController
from wires_panel import WiresTab


class MainWindow(QMainWindow):
    def __init__(self, initial_path: Optional[str] = None):
        super().__init__()
        self.setWindowTitle("Harness Editor")
        self.resize(1000, 700)

        self.view = HarnessGraphicsView(self)

        # Controller derives its model reference from the view and keeps
        # itself in sync across reloads (see harness_controller.py).
        self.controller = HarnessController(self.view)

        # Left panel: a QTabWidget so more tabs (Nodes, Edges, BOM, ...)
        # can be added later alongside Wires.
        self.side_panel = QTabWidget(self)
        self.wires_tab = WiresTab(self.controller, self.view, self)
        self.side_panel.addTab(self.wires_tab, "Wires")
        # Add the Edges tab
        from wires_panel import EdgesTab
        self.edges_tab = EdgesTab(self.controller, self.view, self)
        self.side_panel.addTab(self.edges_tab, "Edges")

        splitter = QSplitter(Qt.Horizontal, self)
        splitter.addWidget(self.side_panel)
        splitter.addWidget(self.view)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([280, 720])
        self.setCentralWidget(splitter)

        self.status_bar = QStatusBar(self)
        self.setStatusBar(self.status_bar)

        self._build_menu_and_toolbar()

        if initial_path:
            self.open_path(initial_path)

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
