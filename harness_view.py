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
- Edges: lines between their start/end node positions, each with a length
  label at its midpoint.
- Nodes can be dragged (ItemIsMovable is on), snapped to a 10-unit grid
  while dragging. Still NO add/delete UI for nodes.
- Single-constraint length lock: if an Edge has length_locked=True and the
  node being dragged has exactly one such locked edge attached, dragging
  that node keeps it at a fixed distance from the *other* end of that
  edge — it's constrained to a circle, not a fixed point, so any angle is
  still allowed (a distance constraint, not a rigid rod). This overrides
  the grid snap for that drag. Two or more locked edges on the same node
  is a multi-constraint solve we don't attempt yet — falls back to
  ordinary free movement in that case.
- Every edge shows a length label (its stored length_mm). The label turns
  red whenever the stored length doesn't match the actual on-screen
  distance between its endpoints, for any reason — a locked edge that
  somehow drifted, an unlocked edge whose nodes were moved without
  updating length_mm, etc. This is a passive visual flag, not an
  enforced constraint (unless length_locked is also set).

Architecture:
- NodeGraphicsItem / EdgeGraphicsItem are QGraphicsObject subclasses (see
  harness_controller.py for how their signals get intercepted).
- NodeGraphicsItem.itemChange(ItemPositionChange) is where both the grid
  snap AND the length-lock circle projection happen — but only during an
  actual interactive drag (tracked via self._press_pos being non-None).
  Programmatic moves (undo/redo, controller-driven changes) pass through
  untouched, so exact constrained positions survive undo/redo without
  being re-snapped to the grid.
- NodeGraphicsItem emits moveFinished(node_id, old_pos, new_pos) only
  once, when the drag ends (mouseReleaseEvent) — this is the point the
  controller turns into a single undo command, instead of one command
  per intermediate mouse-move step. Whatever position itemChange settled
  on (grid-snapped or length-constrained) is simply reported as-is; the
  controller doesn't need to know which case applied.
- EdgeGraphicsItem holds references to its two NodeGraphicsItem endpoints
  and recomputes its line (and length label) whenever either one moves
  (via a direct call from NodeGraphicsItem.itemChange, not a signal —
  this is a purely visual sync, not a model change, so it doesn't go
  through the controller).

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
    QGraphicsItem, QGraphicsObject, QGraphicsTextItem, QWidget, QStyleOptionGraphicsItem,QMenu
)
from PyQt5.QtGui import QBrush, QPen, QColor, QPolygonF, QPainter, QPainterPath
from PyQt5.QtCore import Qt, QPointF, QRectF, pyqtSignal

from harness_model import Harness, Node, Edge, NodeType,PointType,RoutePoint


# --------------------------------------------------------------------------
# Visual constants
# --------------------------------------------------------------------------

NODE_RADIUS = 18          # px, for connector circles / bounding size for polygons
GRID_SPACING_X = 140      # fallback auto-layout spacing (nodes with no saved position)
GRID_SPACING_Y = 120
GRID_COLUMNS = 6

DRAG_SNAP = 5             # nodes snap to this grid size (scene units) while dragging

NODE_COLORS = {
    NodeType.CONNECTOR: QColor("#3B82F6"),     # blue
    NodeType.SPLICE: QColor("#F59E0B"),        # amber
    NodeType.INLINE_JOINT: QColor("#10B981"),  # green
}
NODE_SELECTED_COLOR = QColor("#EF4444")  # red outline when selected
NODE_PEN = QPen(QColor("#222222"), 1.5)

EDGE_PEN = QPen(QColor("#555555"), 2)
LABEL_COLOR = QColor("#222222")

LENGTH_LABEL_TOLERANCE_MM = 0.5   # how far off length_mm can be from the drawn distance before it's flagged
LENGTH_LABEL_COLOR_OK = QColor("#444444")
LENGTH_LABEL_COLOR_BAD = QColor("#DC2626")  # red
BRANCH_POINT_COLOR = QColor("#8B5CF6")  # Purple
LAYOUT_POINT_COLOR = QColor("#94A3B8")  # Gray
BRANCH_POINT_RADIUS = 10
LAYOUT_POINT_RADIUS = 6


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

    def unregister_edge(self, edge_item: "EdgeGraphicsItem") -> None:
        if edge_item in self.edges:
            self.edges.remove(edge_item)

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

    def _locked_edge_item(self) -> Optional["EdgeGraphicsItem"]:
        """Return this node's one length_locked edge, if exactly one is
        attached. Two or more locked edges on the same node is a
        multi-constraint solve we don't attempt yet (see module docstring
        note) — in that case we fall back to ordinary free/grid-snapped
        movement, same as having none at all."""
        locked = [e for e in self.edges if e.edge.length_locked]
        return locked[0] if len(locked) == 1 else None

    def itemChange(self, change, value):
        if change == QGraphicsItem.ItemPositionChange and self._press_pos is not None:
            # Only intercept the proposed position during an actual
            # interactive drag (self._press_pos is set for the whole
            # duration of a drag, see mousePressEvent/_finish_drag).
            # Programmatic moves (undo/redo, controller-driven changes)
            # fall through untouched below.
            locked_edge_item = self._locked_edge_item()
            if locked_edge_item is not None:
                anchor_item = (locked_edge_item.end_item if locked_edge_item.start_item is self
                               else locked_edge_item.start_item)
                anchor_pos = anchor_item.pos()
                dx = value.x() - anchor_pos.x()
                dy = value.y() - anchor_pos.y()
                dist = math.hypot(dx, dy)
                if dist < 1e-6:
                    # Dragged exactly onto the anchor — direction is
                    # undefined; hold the previous position instead of
                    # dividing by zero.
                    return QPointF(self.pos())
                length_mm = locked_edge_item.edge.length_mm
                scale = length_mm / dist
                # Project the proposed point onto the circle of radius
                # length_mm around the anchor — this is a distance
                # constraint, not a rigid rod, so any angle is allowed.
                # Deliberately NOT grid-snapped: satisfying an exact
                # length takes priority over the 10-unit grid.
                return QPointF(anchor_pos.x() + dx * scale, anchor_pos.y() + dy * scale)

            # No active single-edge length constraint — ordinary grid snap.
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

ARROW_SIZE = 7             # px, half-height of the fix-length arrow triangles
ARROW_GAP = 4              # px gap between the label's edge and each arrow
LABEL_OFFSET = 12          # px the label sits above the line (perpendicular offset)


class EdgeLengthLabel(QGraphicsTextItem):
    """The length text on an edge. Selectable on its own (independent of
    the edge line itself) — selecting it is what reveals the two
    fix-length arrows, per spec ("when selecting the length label")."""

    def __init__(self, edge_item: "EdgeGraphicsItem"):
        super().__init__(edge_item)
        self.edge_item = edge_item
        self.setFlag(QGraphicsItem.ItemIsSelectable, True)

    def itemChange(self, change, value):
        if change == QGraphicsItem.ItemSelectedHasChanged:
            self.edge_item.set_arrows_visible(bool(value))
        return super().itemChange(change, value)


class LengthFixArrowItem(QGraphicsItem):
    """One of the two small triangles flanking a selected length label.
    Clicking it asks the controller to rigid-translate that side of the
    network so this edge's drawn length matches its stored length_mm
    exactly. `side` is "start" or "end" (matching Edge.start_node_id /
    Edge.end_node_id) — NOT tied to screen-space left/right, since an
    edge can be at any angle; the arrow's own rotation is what makes it
    point the right way visually."""

    def __init__(self, edge_item: "EdgeGraphicsItem", side: str):
        super().__init__(edge_item)
        self.edge_item = edge_item
        self.side = side
        self.setToolTip(f"Fix length by moving the {side} side")
        self.setVisible(False)
        self.setAcceptedMouseButtons(Qt.LeftButton)

        # A small triangle pointing along local +x; the item's own
        # rotation (set in EdgeGraphicsItem._update_length_label) points
        # it toward the correct node.
        self._triangle = QPolygonF([
            QPointF(-ARROW_SIZE, -ARROW_SIZE),
            QPointF(ARROW_SIZE, 0),
            QPointF(-ARROW_SIZE, ARROW_SIZE),
        ])

    def boundingRect(self) -> QRectF:
        return QRectF(-ARROW_SIZE - 1, -ARROW_SIZE - 1, 2 * ARROW_SIZE + 2, 2 * ARROW_SIZE + 2)

    def shape(self) -> QPainterPath:
        path = QPainterPath()
        path.addPolygon(self._triangle)
        path.closeSubpath()
        return path

    def paint(self, painter, option: QStyleOptionGraphicsItem, widget: Optional[QWidget] = None) -> None:
        painter.setBrush(QBrush(QColor("#2563EB")))  # blue
        painter.setPen(QPen(QColor("#1E3A8A"), 1))
        painter.drawPolygon(self._triangle)

    def mousePressEvent(self, event) -> None:
        event.accept()  # don't let this fall through to a rubber-band select

    def mouseReleaseEvent(self, event) -> None:
        event.accept()
        if self.contains(event.pos()):
            self.edge_item.fixLengthRequested.emit(self.edge_item.edge.edge_id, self.side)


class EdgeGraphicsItem(QGraphicsObject):
    """A single Edge, drawn as a line between two NodeGraphicsItem endpoints.
    Tracks the endpoint items (not static coordinates) so it can redraw
    itself whenever either node moves."""

    # Emitted when a fix-length arrow is clicked: (edge_id, side), where
    # side is "start" or "end" — the side of the network that should be
    # rigid-translated to make this edge's length correct.
    fixLengthRequested = pyqtSignal(str, str)

    def __init__(self, edge: Edge, start_item: NodeGraphicsItem, end_item: NodeGraphicsItem,
                 parent: Optional[QGraphicsItem] = None):
        super().__init__(parent)
        self.edge = edge
        self.start_item = start_item
        self.end_item = end_item
        self._line_start = QPointF(start_item.pos())
        self._line_end = QPointF(end_item.pos())
        self._highlighted = False  # true when a highlighted wire's route passes through this edge
        self._hovered = False  # true when mouse is hovering over this edge

        self.setToolTip(self._tooltip())
        self.setZValue(-1)  # draw behind nodes
        self.setFlag(QGraphicsItem.ItemIsSelectable, True)
        self.setAcceptHoverEvents(True)
        
        start_item.register_edge(self)
        end_item.register_edge(self)

        # This item is never repositioned via setPos() (it stays at local
        # origin (0,0) forever), so children placed with setPos(x, y) sit
        # at that exact scene coordinate — no extra transform math needed
        # for THIS item, though the label/arrows do their own rotation.
        self.length_label = EdgeLengthLabel(self)
        self.start_arrow = LengthFixArrowItem(self, side="start")
        self.end_arrow = LengthFixArrowItem(self, side="end")
        self._update_length_label()
        self.route_point_items: dict[str, RoutePointGraphicsItem] = {}
        self._create_route_points()
    def hoverEnterEvent(self, event) -> None:
        """Mouse entered the edge area."""
        self._hovered = True
        self.update()
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event) -> None:
        """Mouse left the edge area."""
        self._hovered = False
        self.update()
        super().hoverLeaveEvent(event)

    def set_highlighted(self, on: bool) -> None:
        if on != self._highlighted:
            self._highlighted = on
            self.update()
            
    def _create_route_points(self) -> None:
        """Create graphics items for route points on this edge."""
        if self.edge is None:
            return
        harness = self.get_harness()
        if harness is None:
            return
        
        for point in harness.route_points.values():
            if point.edge_id == self.edge.edge_id:
                point_item = RoutePointGraphicsItem(point, self)
                point_item.setPos(point.position[0], point.position[1])
                self.route_point_items[point.point_id] = point_item

    def set_arrows_visible(self, visible: bool) -> None:
        self.start_arrow.setVisible(visible)
        self.end_arrow.setVisible(visible)
        
    def get_harness(self) -> Optional[Harness]:
        """Helper to get the harness from the scene."""
        # This is a bit of a hack - we'll store a reference
        if hasattr(self, '_harness_ref'):
            return self._harness_ref
        return None

    def update_line(self) -> None:
        """Recompute the line from the live positions of the endpoint nodes."""
        self.prepareGeometryChange()
        self._line_start = QPointF(self.start_item.pos())
        self._line_end = QPointF(self.end_item.pos())
        self._update_length_label()
        self.update()

    def boundingRect(self) -> QRectF:
        pad = EDGE_PEN.widthF()*2
        return QRectF(self._line_start, self._line_end).normalized().adjusted(-pad, -pad, pad, pad)

    def shape(self) -> QPainterPath:
        stroker_path = QPainterPath()
        stroker_path.addPolygon(self._widen_line(width=8.0))  # easier to click than the bare line
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
        # Determine pen based on state (priority: hover > highlighted > selected > normal)
        if self._hovered:
            # Hover state: bright blue with glow effect
            pen = QPen(QColor("#3B82F6"), EDGE_PEN.widthF() + 3)
            pen.setCapStyle(Qt.RoundCap)
            painter.setPen(pen)
            painter.drawLine(self._line_start, self._line_end)
            
            # Draw a glow effect (semi-transparent wider line)
            glow_pen = QPen(QColor("#3B82F6"), EDGE_PEN.widthF() + 8)
            glow_pen.setCapStyle(Qt.RoundCap)
            glow_pen.setColor(QColor(59, 130, 246, 80))  # Semi-transparent blue
            painter.setPen(glow_pen)
            painter.drawLine(self._line_start, self._line_end)
        elif self._highlighted:
            # Wire highlight: amber, thicker
            pen = QPen(QColor("#FBBF24"), EDGE_PEN.widthF() + 2)
            pen.setCapStyle(Qt.RoundCap)
            painter.setPen(pen)
            painter.drawLine(self._line_start, self._line_end)
        elif self.isSelected():
            # Selected: red
            pen = QPen(QColor("#EF4444"), EDGE_PEN.widthF())
            pen.setCapStyle(Qt.RoundCap)
            painter.setPen(pen)
            painter.drawLine(self._line_start, self._line_end)
        else:
            # Normal: default gray
            pen = EDGE_PEN
            painter.setPen(pen)
            painter.drawLine(self._line_start, self._line_end)


    def refresh_from_model(self) -> None:
        """Re-sync this item's visuals (tooltip, length label, etc.) after
        the controller has changed a field on self.edge (length_mm,
        length_locked, ...). Line geometry itself is driven by the
        endpoint nodes' positions via update_line(), not this."""
        self.setToolTip(self._tooltip())
        self._update_length_label()
        self.update()

    def _update_length_label(self) -> None:
        """Position the length label aligned with the edge and a little
        above it (like a CAD dimension), flanked by the two fix-length
        arrows. Turns red whenever the stored length doesn't match the
        actual on-screen distance between the endpoints — regardless of
        why: a length_locked edge that somehow drifted out of sync, an
        ordinary unlocked edge whose nodes moved without its length_mm
        being updated, etc. A lock icon prefix marks locked edges."""
        dx = self._line_end.x() - self._line_start.x()
        dy = self._line_end.y() - self._line_start.y()
        actual_distance = math.hypot(dx, dy)
        mismatch = abs(actual_distance - self.edge.length_mm) > LENGTH_LABEL_TOLERANCE_MM

        prefix = "\U0001F512 " if self.edge.length_locked else ""  # lock icon
        self.length_label.setPlainText(f"{prefix}{self.edge.length_mm:g} mm")
        self.length_label.setDefaultTextColor(LENGTH_LABEL_COLOR_BAD if mismatch else LENGTH_LABEL_COLOR_OK)

        if actual_distance > 1e-6:
            ux, uy = dx / actual_distance, dy / actual_distance
        else:
            ux, uy = 1.0, 0.0  # degenerate (coincident nodes) — arbitrary direction

        # True bearing along the edge (start -> end), used for arrow
        # rotation. Arrows always point along the real edge, unrelated to
        # whether the label text itself gets flipped for readability below.
        bearing_deg = math.degrees(math.atan2(dy, dx))

        # Readable label angle: same bearing, but flipped 180° whenever
        # that would otherwise render the text upside-down.
        label_angle_deg = bearing_deg
        if label_angle_deg > 90 or label_angle_deg < -90:
            label_angle_deg += 180 if label_angle_deg < 0 else -180

        # Perpendicular to the edge, pick whichever of the two normals
        # points "up" on screen (Qt's y axis increases downward, so
        # "up" is the more-negative-y candidate).
        nx, ny = -uy, ux
        if ny > 0:
            nx, ny = -nx, -ny

        mid_x = (self._line_start.x() + self._line_end.x()) / 2
        mid_y = (self._line_start.y() + self._line_end.y()) / 2
        label_center = QPointF(mid_x + nx * LABEL_OFFSET, mid_y + ny * LABEL_OFFSET)

        label_rect = self.length_label.boundingRect()
        half_w, half_h = label_rect.width() / 2, label_rect.height() / 2
        self.length_label.setTransformOriginPoint(half_w, half_h)
        self.length_label.setPos(label_center.x() - half_w, label_center.y() - half_h)
        self.length_label.setRotation(label_angle_deg)

        # Arrows flank the label along the TRUE bearing (not the possibly
        # flipped label angle), just outside its rendered width.
        arrow_offset = half_w + ARROW_GAP
        self.end_arrow.setPos(label_center.x() + ux * arrow_offset, label_center.y() + uy * arrow_offset)
        self.end_arrow.setRotation(bearing_deg)
        self.start_arrow.setPos(label_center.x() - ux * arrow_offset, label_center.y() - uy * arrow_offset)
        self.start_arrow.setRotation(bearing_deg + 180)

    def _tooltip(self) -> str:
        e = self.edge
        return (f"{e.edge_id}\nlength={e.length_mm}mm  "
                f"max_diam={e.max_diameter_mm}mm  bend_r={e.bend_radius_mm}mm"
                f"{'  [length locked]' if e.length_locked else ''}")


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
        self.parent = parent
        self.harness: Optional[Harness] = None
        self.current_path: Optional[str] = None
        self.node_items: dict[str, NodeGraphicsItem] = {}
        self.edge_items: dict[str, EdgeGraphicsItem] = {}
        self.route_point_items: dict[str, RoutePointGraphicsItem] = {}
        self.highlighted_wire_ids: set[str] = set()  # wires currently toggled "on" in the Wires tab

        self.scene = QGraphicsScene(self)
        self.setScene(self.scene)
        self.setRenderHint(QPainter.Antialiasing)
        self.setDragMode(QGraphicsView.RubberBandDrag)  # click-drag on empty space = select; on a node = move it
        
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_context_menu)
        self.setFocusPolicy(Qt.StrongFocus)
    # ---- load / save (no dialogs, no message boxes — that's main_window's job) ----
    def add_route_point_item(self, point: RoutePoint) -> None:
        """Add a route point item to the scene."""
        edge_item = self.edge_items.get(point.edge_id)
        if edge_item is None:
            raise ValueError(f"Cannot add route point '{point.point_id}': edge '{point.edge_id}' not in scene")
        
        point_item = RoutePointGraphicsItem(point, edge_item)
        point_item.setPos(QPointF(point.position[0], point.position[1]))
        self.scene.addItem(point_item)
        self.route_point_items[point.point_id] = point_item
        
        # Connect signals for the controller
        if hasattr(self, 'controller'):
            point_item.moveFinished.connect(self.controller._on_point_move_finished)
    
    def remove_route_point_item(self, point_id: str) -> None:
        """Remove a route point item from the scene."""
        point_item = self.route_point_items.pop(point_id, None)
        if point_item is None:
            return
        self.scene.removeItem(point_item)

    def keyPressEvent(self,event):
        if event.key() == Qt.Key_Menu:
            self._show_context_menu_for_selected()
        else:
            super().keyPressEvent(event)
    def _show_context_menu_for_selected(self):
        selected_items = self.scene.selectedItems()
        if not selected_items:
            return
        item = selected_items[0]
        if isinstance(item, EdgeGraphicsItem):
            center = (item._line_start + item._line_end) /2 
            view_pos = self.mapFromScene(center)
            self._show_edge_context_menu(view_pos,item)
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
    def _show_context_menu(self, pos) -> None:
        """Show context menu on right-click."""
        # Convert viewport position to scene position
        scene_pos = self.mapToScene(pos)
        
        # Get items at the scene position - use a small tolerance
        # itemAt uses the exact position, so we need to check with a small area
        items = self.scene.items(scene_pos)
        # Filter to find the first interactive item (edge or route point)
        target_item = None
        for item in items:
            if isinstance(item, EdgeGraphicsItem):
                target_item = item
                break
            elif isinstance(item, RoutePointGraphicsItem):
                target_item = item
                break
            elif isinstance(item, EdgeLengthLabel):
                # If we clicked on the label, get its parent edge
                target_item = item.parentItem()
                break
            elif isinstance(item, LengthFixArrowItem):
                # If we clicked on an arrow, get its parent edge
                target_item = item.parentItem()
                break
        
        if target_item is None:
            # If no item found at exact position, try a small area search
            # Use items() with a small rectangle to find nearby items
            search_rect = QRectF(scene_pos - QPointF(5, 5), QPointF(10, 10))
            items = self.scene.items(search_rect)
            for item in items:
                if isinstance(item, EdgeGraphicsItem):
                    target_item = item
                    break
                elif isinstance(item, RoutePointGraphicsItem):
                    target_item = item
                    break
        
        if isinstance(target_item, EdgeGraphicsItem):
            self._show_edge_context_menu(pos, target_item)
        elif isinstance(target_item, RoutePointGraphicsItem):
            self._show_point_context_menu(pos, target_item)
        elif isinstance(target_item, NodeGraphicsItem):
            self._show_node_context_menu(pos, target_item)

    
    def _show_edge_context_menu(self, pos, edge_item: EdgeGraphicsItem) -> None:
        menu = QMenu(self)
        
        add_branch_action = menu.addAction("Add Branch Point")
        add_branch_action.triggered.connect(
            lambda: self._add_point_at_cursor(pos, edge_item, PointType.BRANCH)
        )
        
        add_layout_action = menu.addAction("Add Layout Point")
        add_layout_action.triggered.connect(
            lambda: self._add_point_at_cursor(pos, edge_item, PointType.LAYOUT)
        )
        
        menu.exec_(self.mapToGlobal(pos))
    
    def _show_point_context_menu(self, pos, point_item: RoutePointGraphicsItem) -> None:
        menu = QMenu(self)
        
        if point_item.is_branch:
            merge_action = menu.addAction("Merge Branch Point")
            merge_action.triggered.connect(
                lambda: self._merge_branch_point(point_item)
            )
            menu.addSeparator()
        
        delete_action = menu.addAction("Delete Point")
        delete_action.triggered.connect(
            lambda: self._delete_route_point(point_item)
        )
        
        menu.exec_(self.mapToGlobal(pos))
    
    def _add_point_at_cursor(self, pos, edge_item: EdgeGraphicsItem, point_type: PointType) -> None:
        """Add a route point at the cursor position on an edge."""
        if self.harness is None or self.parent.controller is None:
            return
        
        # Convert screen position to scene position
        scene_pos = self.mapToScene(pos)
        
        # Find nearest point on the edge
        edge = edge_item.edge
        start_pos = self.node_items[edge.start_node_id].pos()
        end_pos = self.node_items[edge.end_node_id].pos()
        
        # Project cursor onto edge line segment
        dx = end_pos.x() - start_pos.x()
        dy = end_pos.y() - start_pos.y()
        length = math.hypot(dx, dy)
        if length < 1e-6:
            return
        
        t = ((scene_pos.x() - start_pos.x()) * dx + (scene_pos.y() - start_pos.y()) * dy) / (length * length)
        t = max(0, min(1, t))  # Clamp to segment
        
        point_pos = QPointF(
            start_pos.x() + t * dx,
            start_pos.y() + t * dy
        )
        
        # Create the point
        point_id = f"{edge.edge_id}_point_{len(self.harness.route_points) + 1}"
        point = RoutePoint(
            point_id=point_id,
            point_type=point_type,
            edge_id=edge.edge_id,
            position=(point_pos.x(), point_pos.y(), 0),
            label=f"Point {len(self.harness.route_points) + 1}",
        )
        
        # Add to model through controller
        self.parent.controller.add_route_point(point)
    
    def _merge_branch_point(self, point_item: RoutePointGraphicsItem) -> None:
        """Merge a branch point back into its edge."""
        if self.harness is None or self.parent.controller is None:
            return
        
        # Confirm with user
        reply = QMessageBox.question(
            self, "Merge Branch Point",
            f"Merge branch point {point_item.point.point_id}?",
            QMessageBox.Yes | QMessageBox.No
        )
        if reply == QMessageBox.Yes:
            self.parent.controller.merge_branch_point(point_item.point.point_id)
    
    def _delete_route_point(self, point_item: RoutePointGraphicsItem) -> None:
        """Delete a route point (layout points only - branch points must be merged)."""
        if point_item.is_branch:
            QMessageBox.warning(
                self, "Cannot Delete",
                "Branch points must be merged before deletion."
            )
            return
        
        if self.parent.controller is not None:
            self.parent.controller.delete_route_point(point_item.point.point_id)

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
        self.route_point_items.clear()
        self.highlighted_wire_ids.clear()  # a freshly loaded document starts with nothing highlighted
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
        for point in self.harness.route_points.values():
            edge_item = self.edge_items.get(point.edge_id)
            if edge_item is None:
                continue
            point_item = RoutePointGraphicsItem(point, edge_item)
            point_item.setPos(QPointF(point.position[0], point.position[1]))
            self.scene.addItem(point_item)
            self.route_point_items[point.point_id] = point_item

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

    # ---- wire highlighting (driven by the Wires tab) ----

    def set_wire_highlighted(self, wire_id: str, on: bool) -> None:
        """Toggle whether a wire's route is lit up on the canvas. Multiple
        wires can be highlighted at once."""
        if on:
            self.highlighted_wire_ids.add(wire_id)
        else:
            self.highlighted_wire_ids.discard(wire_id)
        self._apply_highlights()

    def _apply_highlights(self) -> None:
        if self.harness is None:
            return
        highlighted_edge_ids: set[str] = set()
        for wire_id in self.highlighted_wire_ids:
            wire = self.harness.wires.get(wire_id)
            if wire is not None:
                highlighted_edge_ids.update(wire.route_edge_ids)

        for edge_id, edge_item in self.edge_items.items():
            edge_item.set_highlighted(edge_id in highlighted_edge_ids)

    # ---- incremental edge add/remove (used by the controller's Dijkstra
    # auto-routing fallback — creating a direct edge shouldn't require a
    # full scene rebuild) ----

    def add_edge_item(self, edge: Edge) -> None:
        start_item = self.node_items.get(edge.start_node_id)
        end_item = self.node_items.get(edge.end_node_id)
        if start_item is None or end_item is None:
            raise ValueError(f"Cannot add edge '{edge.edge_id}': endpoint node(s) not in the scene")
        edge_item = EdgeGraphicsItem(edge, start_item, end_item)
        self.scene.addItem(edge_item)
        self.edge_items[edge.edge_id] = edge_item

    def remove_edge_item(self, edge_id: str) -> None:
        edge_item = self.edge_items.pop(edge_id, None)
        if edge_item is None:
            return
        edge_item.start_item.unregister_edge(edge_item)
        edge_item.end_item.unregister_edge(edge_item)
        self.scene.removeItem(edge_item)
        
class RoutePointGraphicsItem(QGraphicsObject):
    """A point along an edge - either a branch point (can have connections)
    or a layout point (routing aid)."""
    
    # Emitted when this point is moved
    moveFinished = pyqtSignal(str, QPointF, QPointF)
    
    def __init__(self, point: RoutePoint, edge_item: "EdgeGraphicsItem",
                 parent: Optional[QGraphicsItem] = None):
        super().__init__(parent)
        self.point = point
        self.edge_item = edge_item
        self.is_branch = point.point_type == PointType.BRANCH
        
        self.setFlag(QGraphicsItem.ItemIsSelectable, True)
        self.setFlag(QGraphicsItem.ItemIsMovable, True)
        self.setFlag(QGraphicsItem.ItemSendsGeometryChanges, True)
        
        if self.is_branch:
            self.setToolTip(f"Branch: {point.point_id}\n{point.label}")
        else:
            self.setToolTip(f"Layout: {point.point_id}")
        
        # Label for the point
        self.label_item = QGraphicsTextItem(point.label or point.point_id, self)
        self.label_item.setDefaultTextColor(LABEL_COLOR)
        label_rect = self.label_item.boundingRect()
        r = BRANCH_POINT_RADIUS if self.is_branch else LAYOUT_POINT_RADIUS
        self.label_item.setPos(-label_rect.width() / 2, r + 4)
        
        self._press_pos: Optional[QPointF] = None
    
    def boundingRect(self) -> QRectF:
        r = BRANCH_POINT_RADIUS if self.is_branch else LAYOUT_POINT_RADIUS
        pad = NODE_PEN.widthF()
        return QRectF(-r - pad, -r - pad, 2*(r + pad), 2*(r + pad))
    
    def shape(self) -> QPainterPath:
        path = QPainterPath()
        r = BRANCH_POINT_RADIUS if self.is_branch else LAYOUT_POINT_RADIUS
        if self.is_branch:
            # Branch point: diamond
            path.addPolygon(QPolygonF([
                QPointF(0, -r),
                QPointF(r, 0),
                QPointF(0, r),
                QPointF(-r, 0),
            ]))
            path.closeSubpath()
        else:
            # Layout point: small circle with cross
            path.addEllipse(QPointF(0, 0), r, r)
        return path
    
    def paint(self, painter, option: QStyleOptionGraphicsItem, 
              widget: Optional[QWidget] = None) -> None:
        color = BRANCH_POINT_COLOR if self.is_branch else LAYOUT_POINT_COLOR
        r = BRANCH_POINT_RADIUS if self.is_branch else LAYOUT_POINT_RADIUS
        
        if self.is_branch:
            # Diamond
            pen = QPen(QColor("#222222"), 1.5)
            painter.setBrush(QBrush(color))
            painter.setPen(pen)
            painter.drawPolygon(QPolygonF([
                QPointF(0, -r),
                QPointF(r, 0),
                QPointF(0, r),
                QPointF(-r, 0),
            ]))
        else:
            # Circle with cross
            pen = QPen(QColor("#222222"), 1.5)
            painter.setBrush(QBrush(color))
            painter.setPen(pen)
            painter.drawEllipse(QPointF(0, 0), r, r)
            # Cross inside
            painter.drawLine(QPointF(-r/2, 0), QPointF(r/2, 0))
            painter.drawLine(QPointF(0, -r/2), QPointF(0, r/2))
    
    def itemChange(self, change, value):
        if change == QGraphicsItem.ItemPositionChange and self._press_pos is not None:
            # Snap to grid during drag
            return QPointF(_snap(value.x()), _snap(value.y()))
        
        if change == QGraphicsItem.ItemPositionHasChanged:
            # Update the edge's line
            self.edge_item.update_line()
        
        return super().itemChange(change, value)
    
    def mousePressEvent(self, event) -> None:
        self._press_pos = QPointF(self.pos())
        super().mousePressEvent(event)
    
    def mouseReleaseEvent(self, event) -> None:
        super().mouseReleaseEvent(event)
        if self._press_pos is not None:
            old_pos, new_pos = self._press_pos, QPointF(self.pos())
            self._press_pos = None
            if old_pos != new_pos:
                self.moveFinished.emit(self.point.point_id, old_pos, new_pos)
    
    def refresh_from_model(self) -> None:
        self.label_item.setPlainText(self.point.label or self.point.point_id)
        label_rect = self.label_item.boundingRect()
        r = BRANCH_POINT_RADIUS if self.is_branch else LAYOUT_POINT_RADIUS
        self.label_item.setPos(-label_rect.width() / 2, r + 4)
        self.update()

    def setPos(self, pos: QPointF) -> None:
        # Override to keep the point on the edge
        super().setPos(pos)
        # Update the point's position in the model
        self.point.position = (pos.x(), pos.y(), 0)
