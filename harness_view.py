"""
harness_view.py

Read-only-layout PyQt5 viewer for a Harness (see harness_model.py).

Scope for this stage (deliberately minimal, per spec):
- Load a Harness from JSON and render it.
- Save the currently loaded Harness back to JSON.
- Nodes are drawn as circles (connector) or polygons (splice / inline joint).
- Edges are drawn as lines between their start/end node positions.
- NO creation, deletion, or moving of items yet — ItemIsMovable is off,
  and there is no "add node/edge/wire" UI.

Architecture (this revision):
- NodeGraphicsItem and EdgeGraphicsItem are real QGraphicsItem subclasses,
  not thin wrapper objects around native shape items. This is the standard
  Qt pattern for CAD-style tools: custom paint()/boundingRect()/shape(),
  the domain object (Node/Edge) attached directly on the item, and
  itemChange() hooks that will matter once dragging is added.
- NodeGraphicsItem draws itself in local coordinates centered on (0, 0)
  and is placed in the scene via setPos(x, y). This means a future "move"
  feature only has to change .pos() — no shape recomputation needed.
- NodeGraphicsItem keeps a list of the EdgeGraphicsItems attached to it.
  On ItemPositionHasChanged it tells each attached edge to redraw. Movement
  is disabled for now, but the wiring is already in place for later.
- EdgeGraphicsItem holds references to its two NodeGraphicsItem endpoints
  (not raw coordinates) and recomputes its line from their live .pos().
- The label is a child QGraphicsTextItem (setParentItem), so it moves and
  selects together with its shape automatically.

Layout:
- If a Node has an explicit `position` (x, y[, z]) it is used as-is
  (x, y become scene coordinates; z is ignored for this 2D view).
- If a Node has no position, it is auto-placed on a simple grid so the
  file can still be visualized. This auto layout is NOT written back to
  the JSON on save — save serializes the original Harness model, whose
  Node.position was never touched.

Run:
    python3 harness_view.py [optional_path_to_harness.json]
"""

from __future__ import annotations

import sys
import math
from typing import Optional

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QGraphicsView, QGraphicsScene,
    QGraphicsItem, QGraphicsObject, QGraphicsTextItem,
    QFileDialog, QAction, QMessageBox, QStatusBar, QStyleOptionGraphicsItem, QWidget,
)
from PyQt5.QtGui import QBrush, QPen, QColor, QPolygonF, QPainter, QPainterPath
from PyQt5.QtCore import Qt, QPointF, QRectF, pyqtSignal

from harness_model import Harness, Node, Edge, Wire, NodeType
from harness_controller import HarnessController

# --------------------------------------------------------------------------
# Visual constants
# --------------------------------------------------------------------------

NODE_RADIUS = 18          # px, for connector circles / bounding size for polygons
GRID_SPACING_X = 140
GRID_SPACING_Y = 120
GRID_COLUMNS = 6

NODE_COLORS = {
    NodeType.CONNECTOR: QColor("#3B82F6"),     # blue
    NodeType.SPLICE: QColor("#F59E0B"),        # amber
    NodeType.INLINE_JOINT: QColor("#10B981"),  # green
}
NODE_SELECTED_COLOR = QColor("#EF4444")  # red outline when selected
NODE_PEN = QPen(QColor("#222222"), 1.5)

EDGE_PEN = QPen(QColor("#555555"), 2)
LABEL_COLOR = QColor("#222222")


def _regular_polygon(radius: float, sides: int, rotation_deg: float = -90) -> QPolygonF:
    """Build a regular polygon centered at local origin (0, 0)."""
    poly = QPolygonF()
    start_angle = math.radians(rotation_deg)
    for i in range(sides):
        angle = start_angle + i * (2 * math.pi / sides)
        poly.append(QPointF(radius * math.cos(angle), radius * math.sin(angle)))
    return poly


# --------------------------------------------------------------------------
# NodeGraphicsItem — connector (circle) / splice (hexagon) / inline joint (diamond)
# --------------------------------------------------------------------------

class NodeGraphicsItem(QGraphicsObject):
    """A single Node (connector / splice / inline joint), drawn in local
    coordinates around (0, 0) and positioned in the scene via setPos().

    Subclasses QGraphicsObject (QObject + QGraphicsItem) rather than plain
    QGraphicsItem so it can emit Qt signals. This is how the controller
    intercepts scene changes: it doesn't poll or subclass the scene, it
    just connects to positionChanged (and future signals like
    labelEdited) on every item."""

    positionChanged = pyqtSignal(str, QPointF)  # (node_id, new_scene_pos)

    def __init__(self, node: Node, radius: float = NODE_RADIUS, parent: Optional[QGraphicsItem] = None):
        super().__init__(parent)
        self.node = node
        self.radius = radius
        self.edges: list["EdgeGraphicsItem"] = []  # edges attached to this node

        self._polygon: Optional[QPolygonF] = None  # None => draw as ellipse
        if node.node_type == NodeType.SPLICE:
            self._polygon = _regular_polygon(radius, sides=6)
        elif node.node_type == NodeType.INLINE_JOINT:
            self._polygon = _regular_polygon(radius, sides=4, rotation_deg=45)
        # CONNECTOR stays as ellipse (self._polygon is None)

        self.setToolTip(self._tooltip())
        self.setFlag(QGraphicsItem.ItemIsSelectable, True)
        self.setFlag(QGraphicsItem.ItemIsMovable, True)          # not yet — CAD move comes later
        self.setFlag(QGraphicsItem.ItemSendsGeometryChanges, True)  # needed so itemChange fires on setPos

        self.label_item = QGraphicsTextItem(node.label or node.node_id, self)
        self.label_item.setDefaultTextColor(LABEL_COLOR)
        label_rect = self.label_item.boundingRect()
        self.label_item.setPos(-label_rect.width() / 2, radius + 2)

    # ---- bookkeeping used by EdgeGraphicsItem ----

    def register_edge(self, edge_item: "EdgeGraphicsItem") -> None:
        self.edges.append(edge_item)

    # ---- QGraphicsItem overrides ----

    def boundingRect(self) -> QRectF:
        r = self.radius + NODE_PEN.widthF()
        return QRectF(-r, -r, 2 * r, 2 * r)

    def shape(self) -> QPainterPath:
        path = QPainterPath()
        if self._polygon is not None:
            path.addPolygon(self._polygon)
            path.closeSubpath()
        else:
            path.addEllipse(QPointF(0, 0), self.radius, self.radius)
        return path

    def paint(self, painter, option: QStyleOptionGraphicsItem, widget: Optional[QWidget] = None) -> None:
        color = NODE_COLORS.get(self.node.node_type, QColor("#888888"))
        pen = QPen(NODE_SELECTED_COLOR, 2.5) if self.isSelected() else NODE_PEN
        painter.setBrush(QBrush(color))
        painter.setPen(pen)
        if self._polygon is not None:
            painter.drawPolygon(self._polygon)
        else:
            painter.drawEllipse(QPointF(0, 0), self.radius, self.radius)

    def itemChange(self, change, value):
        if change == QGraphicsItem.ItemPositionHasChanged:
            for edge_item in self.edges:
                edge_item.update_line()
            # Notify anyone listening (the controller) that this node moved.
            # NOTE: while ItemIsMovable is False this only fires from
            # programmatic setPos() calls (e.g. the controller applying an
            # undo/redo). Once dragging is enabled, a real mouse drag fires
            # this on every intermediate step — at that point the commit
            # point should likely move to mouseReleaseEvent instead of
            # here, so a single drag produces a single undo entry.
            self.positionChanged.emit(self.node.node_id, self.pos())
        return super().itemChange(change, value)

    def refresh_from_model(self) -> None:
        """Re-sync this item's visuals from self.node after the controller
        has changed a field on the model (label, etc). Does NOT touch
        position — position updates go through setPos()/itemChange."""
        self.label_item.setPlainText(self.node.label or self.node.node_id)
        label_rect = self.label_item.boundingRect()
        self.label_item.setPos(-label_rect.width() / 2, self.radius + 2)
        self.setToolTip(self._tooltip())
        self.update()

    def _tooltip(self) -> str:
        return f"{self.node.node_id} ({self.node.node_type.value})\n{self.node.label}"


# --------------------------------------------------------------------------
# EdgeGraphicsItem — physical bundle segment between two nodes
# --------------------------------------------------------------------------

class EdgeGraphicsItem(QGraphicsItem):
    """A single Edge, drawn as a line between two NodeGraphicsItem endpoints.
    Tracks the endpoint items (not static coordinates) so it can redraw
    itself if either node ever moves."""

    def __init__(self, edge: Edge, start_item: NodeGraphicsItem, end_item: NodeGraphicsItem,
                 parent: Optional[QGraphicsItem] = None):
        super().__init__(parent)
        self.edge = edge
        self.start_item = start_item
        self.end_item = end_item
        self._line_start = QPointF(start_item.pos())
        self._line_end = QPointF(end_item.pos())

        self.setToolTip(self._tooltip())
        self.setZValue(-1)  # draw behind nodes
        self.setFlag(QGraphicsItem.ItemIsSelectable, True)

        start_item.register_edge(self)
        end_item.register_edge(self)

    def update_line(self) -> None:
        """Recompute the line from the live positions of the endpoint nodes."""
        self.prepareGeometryChange()
        self._line_start = QPointF(self.start_item.pos())
        self._line_end = QPointF(self.end_item.pos())
        self.update()

    def boundingRect(self) -> QRectF:
        pad = EDGE_PEN.widthF()
        return QRectF(self._line_start, self._line_end).normalized().adjusted(-pad, -pad, pad, pad)

    def shape(self) -> QPainterPath:
        stroker_path = QPainterPath()
        # Give the line some hit-test width so it's easy to click/select.
        stroker_path.addPolygon(self._widen_line(width=6.0))
        return stroker_path

    def _widen_line(self, width: float) -> QPolygonF:
        dx = self._line_end.x() - self._line_start.x()
        dy = self._line_end.y() - self._line_start.y()
        length = math.hypot(dx, dy) or 1.0
        nx, ny = -dy / length * width / 2, dx / length * width / 2
        return QPolygonF([
            QPointF(self._line_start.x() + nx, self._line_start.y() + ny),
            QPointF(self._line_end.x() + nx, self._line_end.y() + ny),
            QPointF(self._line_end.x() - nx, self._line_end.y() - ny),
            QPointF(self._line_start.x() - nx, self._line_start.y() - ny),
        ])

    def paint(self, painter, option: QStyleOptionGraphicsItem, widget: Optional[QWidget] = None) -> None:
        pen = QPen(QColor("#EF4444"), EDGE_PEN.widthF()) if self.isSelected() else EDGE_PEN
        painter.setPen(pen)
        painter.drawLine(self._line_start, self._line_end)

    def refresh_from_model(self) -> None:
        """Re-sync this item's visuals (tooltip, etc.) after the controller
        has changed a field on self.edge. Line geometry itself is driven
        by the endpoint nodes' positions via update_line(), not this."""
        self.setToolTip(self._tooltip())
        self.update()

    def _tooltip(self) -> str:
        e = self.edge
        return (f"{e.edge_id}\nlength={e.length_mm}mm  "
                f"max_diam={e.max_diameter_mm}mm  bend_r={e.bend_radius_mm}mm")


# --------------------------------------------------------------------------
# Main window
# --------------------------------------------------------------------------

class HarnessView(QMainWindow):
    # Emitted after _render() rebuilds the scene from scratch (e.g. a fresh
    # Open), since that discards the old graphics items. The controller
    # listens to this so it can reconnect to the newly created items.
    sceneRebuilt = pyqtSignal()

    def __init__(self, initial_path: Optional[str] = None):
        super().__init__()
        
        self.setWindowTitle("Harness Viewer")
        self.resize(1000, 700)

        
        self.harness: Optional[Harness] = None
        self.current_path: Optional[str] = None
        self.node_items: dict[str, NodeGraphicsItem] = {}
        self.edge_items: dict[str, EdgeGraphicsItem] = {}

        self.scene = QGraphicsScene(self)
        self.view = QGraphicsView(self.scene, self)
        self.view.setRenderHint(QPainter.Antialiasing)
        self.view.setDragMode(QGraphicsView.ScrollHandDrag)
        self.setCentralWidget(self.view)

        self.status_bar = QStatusBar(self)
        self.setStatusBar(self.status_bar)

        self._build_menu()
        
        if initial_path:
            self.load_json(initial_path)

    # ---- menu ----

    def _build_menu(self) -> None:
        menu = self.menuBar().addMenu("&File")

        open_action = QAction("&Open JSON...", self)
        open_action.setShortcut("Ctrl+O")
        open_action.triggered.connect(self.on_open)
        menu.addAction(open_action)

        save_action = QAction("&Save JSON", self)
        save_action.setShortcut("Ctrl+S")
        save_action.triggered.connect(self.on_save)
        menu.addAction(save_action)

        save_as_action = QAction("Save JSON &As...", self)
        save_as_action.setShortcut("Ctrl+Shift+S")
        save_as_action.triggered.connect(self.on_save_as)
        menu.addAction(save_as_action)

    # ---- file actions ----

    def on_open(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Open Harness JSON", "", "JSON Files (*.json)")
        if path:
            self.load_json(path)

    def on_save(self) -> None:
        if self.harness is None:
            return
        if self.current_path is None:
            self.on_save_as()
            return
        self.save_json(self.current_path)

    def on_save_as(self) -> None:
        if self.harness is None:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Save Harness JSON", "", "JSON Files (*.json)")
        if path:
            self.save_json(path)

    # ---- core load/save ----

    def load_json(self, path: str) -> None:
        try:
            harness = Harness.load_json(path)
        except Exception as exc:
            QMessageBox.critical(self, "Load failed", f"Could not load '{path}':\n{exc}")
            return

        self.harness = harness
        self.current_path = path
        self.controller = HarnessController(self.harness,self.view,self)
        self._render()
        self.status_bar.showMessage(
            f"Loaded '{path}': {len(harness.nodes)} nodes, "
            f"{len(harness.edges)} edges, {len(harness.wires)} wires"
        )
        self.setWindowTitle(f"Harness Viewer — {path}")

    def save_json(self, path: str) -> None:
        if self.harness is None:
            return
        try:
            self.harness.save_json(path)
        except Exception as exc:
            QMessageBox.critical(self, "Save failed", f"Could not save '{path}':\n{exc}")
            return
        self.current_path = path
        self.status_bar.showMessage(f"Saved '{path}'")
        self.setWindowTitle(f"Harness Viewer — {path}")

    # ---- rendering ----

    def _compute_layout(self) -> dict:
        """Return {node_id: QPointF}. Uses explicit Node.position where present,
        otherwise falls back to a simple grid layout for that node only."""
        positions: dict[str, QPointF] = {}
        grid_index = 0
        for node in self.harness.nodes.values():
            if node.position is not None:
                x, y = node.position[0], node.position[1]
                positions[node.node_id] = QPointF(x, y)
            else:
                col = grid_index % GRID_COLUMNS
                row = grid_index // GRID_COLUMNS
                positions[node.node_id] = QPointF(col * GRID_SPACING_X, row * GRID_SPACING_Y)
                grid_index += 1
        return positions

    def _render(self) -> None:
        self.scene.clear()
        self.node_items.clear()
        self.edge_items.clear()
        if self.harness is None:
            return

        positions = self._compute_layout()

        # Create node items first (edges need to reference them)
        for node in self.harness.nodes.values():
            item = NodeGraphicsItem(node)
            item.setPos(positions[node.node_id])
            self.scene.addItem(item)
            self.node_items[node.node_id] = item

        # Create edge items, wiring them to their endpoint node items
        for edge in self.harness.edges.values():
            start_item = self.node_items.get(edge.start_node_id)
            end_item = self.node_items.get(edge.end_node_id)
            if start_item is None or end_item is None:
                continue  # dangling reference; skip silently for now
            edge_item = EdgeGraphicsItem(edge, start_item, end_item)
            self.scene.addItem(edge_item)
            self.edge_items[edge.edge_id] = edge_item

        self.scene.setSceneRect(self.scene.itemsBoundingRect().adjusted(-40, -40, 40, 40))
        self.view.fitInView(self.scene.sceneRect(), Qt.KeepAspectRatio)

        self.sceneRebuilt.emit()

    # ---- targeted refresh (used by the controller after a model change) ----

    def refresh_entity(self, entity_kind: str, entity_id: str) -> None:
        """Refresh only the visuals for one entity, rather than re-rendering
        the whole scene. The controller calls this after every committed
        model change."""
        if entity_kind == "node":
            item = self.node_items.get(entity_id)
            if item is not None:
                item.refresh_from_model()
        elif entity_kind == "edge":
            item = self.edge_items.get(entity_id)
            if item is not None:
                item.refresh_from_model()
        elif entity_kind == "wire":
            pass  # wires aren't drawn yet — nothing to refresh visually


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def main():
    app = QApplication(sys.argv)
    initial_path = sys.argv[1] if len(sys.argv) > 1 else None
    window = HarnessView(initial_path)
    window.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
