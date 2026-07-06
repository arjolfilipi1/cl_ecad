"""
harness_view.py

The canvas: a QGraphicsView widget that renders a Harness (see
harness_model.py), plus the QGraphicsItem subclasses it draws.

This file is deliberately "dumb" about application chrome — no menu bar,
no toolbar, no dialogs. That all now lives in main_window.py. This class
only knows how to load/hold/save a Harness and draw it.

Scope for this stage:
- Load a Harness from JSON and render it; save it back to JSON.
- Nodes: circle (connector) / hexagon (splice) / diamond (inline joint).
- Edges: lines between their start/end node positions.
- Nodes can now be dragged (ItemIsMovable is on), snapped to a 10-unit
  grid while dragging. Still NO add/delete UI.

Architecture:
- NodeGraphicsItem / EdgeGraphicsItem are QGraphicsObject subclasses (see
  harness_controller.py for how their signals get intercepted).
- NodeGraphicsItem snaps its own position to a 10-unit grid via
  itemChange(ItemPositionChange) — this runs on every intermediate step
  of a drag, not just the end, so the item visually snaps as you drag it.
- NodeGraphicsItem emits moveFinished(node_id, old_pos, new_pos) only
  once, when the drag ends (mouseReleaseEvent) — this is the point the
  controller turns into a single undo command, instead of one command
  per intermediate mouse-move step.
- EdgeGraphicsItem holds references to its two NodeGraphicsItem endpoints
  and recomputes its line whenever either one moves (via a direct call
  from NodeGraphicsItem.itemChange, not a signal — this is a purely
  visual sync, not a model change, so it doesn't go through the
  controller).

Layout:
- If a Node has an explicit `position` (x, y[, z]) it is used as-is.
- If a Node has no position, it's auto-placed on a simple grid so the
  file can still be visualized. This fallback is never written back to
  the model — save always serializes Node.position as last set.
"""

from __future__ import annotations

import math
from typing import Optional

from PyQt5.QtWidgets import (
    QGraphicsView, QGraphicsScene,
    QGraphicsItem, QGraphicsObject, QGraphicsTextItem, QWidget, QStyleOptionGraphicsItem,
)
from PyQt5.QtGui import QBrush, QPen, QColor, QPolygonF, QPainter, QPainterPath
from PyQt5.QtCore import Qt, QPointF, QRectF, pyqtSignal

from harness_model import Harness, Node, Edge, NodeType


# --------------------------------------------------------------------------
# Visual constants
# --------------------------------------------------------------------------

NODE_RADIUS = 18          # px, for connector circles / bounding size for polygons
GRID_SPACING_X = 140      # fallback auto-layout spacing (nodes with no saved position)
GRID_SPACING_Y = 120
GRID_COLUMNS = 6

DRAG_SNAP = 10            # nodes snap to this grid size (scene units) while dragging

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


def _snap(value: float, step: float = DRAG_SNAP) -> float:
    return round(value / step) * step


# --------------------------------------------------------------------------
# NodeGraphicsItem — connector (circle) / splice (hexagon) / inline joint (diamond)
# --------------------------------------------------------------------------

class NodeGraphicsItem(QGraphicsObject):
    """A single Node (connector / splice / inline joint), drawn in local
    coordinates around (0, 0) and positioned in the scene via setPos().

    Subclasses QGraphicsObject (QObject + QGraphicsItem) so it can emit
    Qt signals — that's how the controller intercepts scene changes
    without polling or subclassing the scene itself."""

    # Emitted once per completed drag: (node_id, old_scene_pos, new_scene_pos).
    # NOT emitted on every intermediate mouse-move step, so one drag
    # produces exactly one undo command.
    moveFinished = pyqtSignal(str, QPointF, QPointF)

    def __init__(self, node: Node, radius: float = NODE_RADIUS, parent: Optional[QGraphicsItem] = None):
        super().__init__(parent)
        self.node = node
        self.radius = radius
        self.edges: list["EdgeGraphicsItem"] = []  # edges attached to this node
        self._press_pos: Optional[QPointF] = None  # position at the start of the current drag

        self._polygon: Optional[QPolygonF] = None  # None => draw as ellipse
        if node.node_type == NodeType.SPLICE:
            self._polygon = _regular_polygon(radius, sides=6)
        elif node.node_type == NodeType.INLINE_JOINT:
            self._polygon = _regular_polygon(radius, sides=4, rotation_deg=45)
        # CONNECTOR stays as ellipse (self._polygon is None)

        self.setToolTip(self._tooltip())
        self.setFlag(QGraphicsItem.ItemIsSelectable, True)
        self.setFlag(QGraphicsItem.ItemIsMovable, True)
        self.setFlag(QGraphicsItem.ItemSendsGeometryChanges, True)  # needed for itemChange to fire

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
        if change == QGraphicsItem.ItemPositionChange:
            # value is the proposed new position — snap it to the grid.
            # This runs on every intermediate step of a drag, so the item
            # visibly snaps as it moves, not just when released.
            return QPointF(_snap(value.x()), _snap(value.y()))

        if change == QGraphicsItem.ItemPositionHasChanged:
            for edge_item in self.edges:
                edge_item.update_line()

        return super().itemChange(change, value)

    def mousePressEvent(self, event) -> None:
        self._press_pos = QPointF(self.pos())
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        super().mouseReleaseEvent(event)
        self._finish_drag()

    def _finish_drag(self) -> None:
        """Factored out of mouseReleaseEvent so it can also be called
        directly (e.g. in tests) without a real QGraphicsSceneMouseEvent."""
        if self._press_pos is None:
            return
        old_pos, new_pos = self._press_pos, QPointF(self.pos())
        self._press_pos = None
        if old_pos != new_pos:
            self.moveFinished.emit(self.node.node_id, old_pos, new_pos)

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
    itself whenever either node moves."""

    def __init__(self, edge: Edge, start_item: NodeGraphicsItem, end_item: NodeGraphicsItem,
                 parent: Optional[QGraphicsItem] = None):
        super().__init__(parent)
        self.edge = edge
        self.start_item = start_item
        self.end_item = end_item
        self._line_start = QPointF()
        self._line_end = QPointF()

        self.setToolTip(self._tooltip())
        self.setZValue(-1)  # draw behind nodes
        self.setFlag(QGraphicsItem.ItemIsSelectable, True)
        self._current_pen = EDGE_PEN
        
        start_item.register_edge(self)
        end_item.register_edge(self)
        self.update_line()

    def setPen(self, pen: QPen) -> None:
        """Set the pen used to draw the edge (used for highlighting)."""
        print("pen set to ",pen.color().rgb()  )
        self._current_pen = pen
        self.update()
    
    # Also update the paint method to use the stored pen:
    def paint(self, painter, option: QStyleOptionGraphicsItem, widget: Optional[QWidget] = None) -> None:
        if self.isSelected():
            pen = QPen(QColor("#EF4444"), self._current_pen.widthF())
        else:
            pen = getattr(self, '_current_pen', EDGE_PEN)
            
        painter.setPen(pen)
        painter.drawLine(self._line_start, self._line_end)

    def update_line(self) -> None:
        """Recompute the line from the live positions of the endpoint nodes."""
        self.prepareGeometryChange()
        self._line_start = self.mapFromScene(self.start_item.scenePos())
        self._line_end = self.mapFromScene(self.end_item.scenePos())
        self.update()

    def boundingRect(self) -> QRectF:
        # Use the dynamic pen width to prevent clipping artifacts
        pen_width = getattr(self, '_current_pen', EDGE_PEN).widthF()
        pad = pen_width / 2.0 + 2.0  # Add extra padding for safety
        
        rect = QRectF(self._line_start, self._line_end).normalized()
        return rect.adjusted(-pad, -pad, pad, pad)

    def shape(self) -> QPainterPath:
        stroker_path = QPainterPath()
        stroker_path.addPolygon(self._widen_line(width=6.0))  # easier to click than the bare line
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
# HarnessGraphicsView — the canvas widget
# --------------------------------------------------------------------------

class HarnessGraphicsView(QGraphicsView):
    """A QGraphicsView that renders a Harness. Owns the scene and all
    graphics items; knows nothing about menus, toolbars, or dialogs —
    that's main_window.py's job."""

    # Emitted after render() rebuilds the scene from scratch (e.g. a fresh
    # load_json), since that discards the old graphics items. The
    # controller listens to this so it can reconnect to the new items.
    sceneRebuilt = pyqtSignal()

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)

        self.harness: Optional[Harness] = None
        self.current_path: Optional[str] = None
        self.node_items: dict[str, NodeGraphicsItem] = {}
        self.edge_items: dict[str, EdgeGraphicsItem] = {}

        self.scene = QGraphicsScene(self)
        self.setScene(self.scene)
        self.setRenderHint(QPainter.Antialiasing)
        self.setDragMode(QGraphicsView.RubberBandDrag)  # click-drag on empty space = select; on a node = move it

    # ---- load / save (no dialogs, no message boxes — that's main_window's job) ----

    def load_json(self, path: str) -> None:
        harness = Harness.load_json(path)  # let exceptions propagate to the caller
        self.harness = harness
        self.current_path = path
        self.render()

    def save_json(self, path: str) -> None:
        if self.harness is None:
            raise ValueError("No harness loaded to save")
        self.harness.save_json(path)
        self.current_path = path

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

    def render(self) -> None:
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
        self.fitInView(self.scene.sceneRect(), Qt.KeepAspectRatio)

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
