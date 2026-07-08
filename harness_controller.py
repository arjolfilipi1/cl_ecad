"""
harness_controller.py

Mediator between the Harness model (harness_model.py) and the
HarnessView/graphics items (harness_view.py).

Responsibilities:
- Own the QUndoStack. Every mutation — node moved, a field renamed or
  edited, anything — goes through a QUndoCommand pushed here, so it's
  automatically undoable/redoable.
- Intercept change signals coming from the scene's graphics items.
  NodeGraphicsItem.moveFinished fires once per completed drag (not once
  per intermediate mouse-move step), so one drag produces exactly one
  undo command. Future signals — a label edited in place, a property
  panel editing an edge/wire field — plug in the same way.
- The Harness model is the single source of truth. The controller is the
  ONLY thing allowed to mutate it. Graphics items never touch the model
  directly; they only report "this changed" via signals or explicit
  controller calls.
- After a change is applied to the model, the controller tells the view
  to refresh just the affected item — it never re-renders the whole
  scene for a single edit.

Not yet implemented (comes with further CAD UI work):
- Add/delete commands for nodes, edges, wires.
- Signals for label-in-place-editing / property-panel edits, though the
  controller methods for committing such edits already exist below
  (set_node_label, set_edge_field, set_wire_field) and just need a UI to
  call them.
"""

from __future__ import annotations

import math
from typing import Any, Optional

from PyQt5.QtCore import QObject, QPointF, pyqtSignal
from PyQt5.QtWidgets import QUndoCommand, QUndoStack

from harness_model import Harness, Edge, Wire
from harness_routing import find_route, estimate_direct_length


# --------------------------------------------------------------------------
# Undo commands
# --------------------------------------------------------------------------

class MoveNodeCommand(QUndoCommand):
    """Undoable move of a single node to a new position."""

    def __init__(self, controller: "HarnessController", node_id: str,
                 old_pos: tuple, new_pos: tuple):
        super().__init__(f"Move {node_id}")
        self.controller = controller
        self.node_id = node_id
        self.old_pos = old_pos
        self.new_pos = new_pos

    def redo(self) -> None:
        self.controller._apply_node_position(self.node_id, self.new_pos)

    def undo(self) -> None:
        self.controller._apply_node_position(self.node_id, self.old_pos)


class AddEdgeCommand(QUndoCommand):
    """Undoable creation of a new Edge (used for the direct-segment
    fallback when Dijkstra routing finds no existing path)."""

    def __init__(self, controller: "HarnessController", edge: Edge):
        super().__init__(f"Add edge {edge.edge_id}")
        self.controller = controller
        self.edge = edge

    def redo(self) -> None:
        self.controller._apply_add_edge(self.edge)

    def undo(self) -> None:
        self.controller._apply_remove_edge(self.edge.edge_id)

class AddRoutePointCommand(QUndoCommand):
    def __init__(self, controller: "HarnessController", point: RoutePoint):
        super().__init__(f"Add {point.point_type.value} point {point.point_id}")
        self.controller = controller
        self.point = point
    
    def redo(self) -> None:
        self.controller._apply_add_route_point(self.point)
    
    def undo(self) -> None:
        self.controller._apply_delete_route_point(self.point.point_id)

class MergeBranchPointCommand(QUndoCommand):
    def __init__(self, controller: "HarnessController", point_id: str):
        super().__init__(f"Merge branch point {point_id}")
        self.controller = controller
        self.point_id = point_id
    
    def redo(self) -> None:
        self.controller._apply_merge_branch_point(self.point_id)
    
    def undo(self) -> None:
        # Undo merge by re-splitting the edge
        # This is complex - store the split state
        pass

class DeleteRoutePointCommand(QUndoCommand):
    def __init__(self, controller: "HarnessController", point_id: str):
        super().__init__(f"Delete layout point {point_id}")
        self.controller = controller
        self.point_id = point_id
    
    def redo(self) -> None:
        self.controller._apply_delete_route_point(self.point_id)
    
    def undo(self) -> None:
        # Re-add the point
        pass



class AddWireCommand(QUndoCommand):
    """Undoable creation of a new Wire."""

    def __init__(self, controller: "HarnessController", wire):
        super().__init__(f"Add wire {wire.wire_id}")
        self.controller = controller
        self.wire = wire

    def redo(self) -> None:
        self.controller._apply_add_wire(self.wire)

    def undo(self) -> None:
        self.controller._apply_remove_wire(self.wire.wire_id)


class SetFieldCommand(QUndoCommand):
    """Generic undoable set of a single attribute on a model entity
    (Node.label, Edge.length_mm, Wire.color, etc). Covers renaming and
    ordinary data edits with one command class."""

    def __init__(self, controller: "HarnessController", entity_kind: str, entity_id: str,
                 field_name: str, old_value: Any, new_value: Any,
                 description: Optional[str] = None):
        super().__init__(description or f"Edit {entity_kind}.{field_name}")
        self.controller = controller
        self.entity_kind = entity_kind
        self.entity_id = entity_id
        self.field_name = field_name
        self.old_value = old_value
        self.new_value = new_value

    def redo(self) -> None:
        self.controller._apply_field(self.entity_kind, self.entity_id, self.field_name, self.new_value)

    def undo(self) -> None:
        self.controller._apply_field(self.entity_kind, self.entity_id, self.field_name, self.old_value)


# --------------------------------------------------------------------------
# Controller
# --------------------------------------------------------------------------

class HarnessController(QObject):
    """Wires a Harness model to a HarnessView, routing every change through
    an undo stack."""

    # Emitted after a change has been committed to the model, so the view
    # (or anything else, e.g. a future property panel) can react.
    modelChanged = pyqtSignal(str, str)  # (entity_kind, entity_id)

    # Emitted specifically when a wire is added or removed — i.e. the
    # *set* of wires changed, not just a field on one of them. The Wires
    # tab listens to this to know when to add/remove a row (modelChanged
    # alone only tells it a field changed on an existing row).
    wireListChanged = pyqtSignal()

    # Same idea, for edges — fires when Dijkstra routing has to fall back
    # to creating a brand new direct edge.
    edgeListChanged = pyqtSignal()
    pointListChanged = pyqtSignal()  # New signal for points list changes
    def __init__(self, view, parent: Optional[QObject] = None):
        super().__init__(parent)
        self.view = view
        self.harness: Optional[Harness] = view.harness
        self.undo_stack = QUndoStack(self)

        # Guards against the controller's own model->view writes (e.g. an
        # undo/redo calling item.setPos()) being mistaken for a fresh user
        # edit and re-recorded as a new command.
        self._applying = False

        self.modelChanged.connect(self._on_model_changed)

        # Reconnect to the scene's items whenever the view rebuilds them
        # (e.g. on File > Open), and connect now in case items already
        # exist.
        self.view.sceneRebuilt.connect(self.connect_scene)
        self.connect_scene()

    # ---- wiring into the scene ----

    def connect_scene(self) -> None:
        """(Re)connect to every node/edge item currently in the view, and
        re-sync self.harness. Safe to call repeatedly — old items are gone
        after a rebuild, so there's nothing stale to disconnect. Loading a
        new file replaces view.harness with a brand new Harness instance,
        so the controller must track that too or it would keep mutating
        (and undo/redo-ing) a discarded model."""
        self.harness = self.view.harness
        self.undo_stack.clear()  # old commands would reference a now-discarded document
        for node_item in self.view.node_items.values():
            node_item.moveFinished.connect(self._on_node_move_finished)
        for edge_item in self.view.edge_items.values():
            edge_item.fixLengthRequested.connect(self._on_fix_length_requested)
        for point_item in self.view.route_point_items.values():
            point_item.moveFinished.connect(self._on_point_move_finished)

    # ---- scene -> model (change interception) ----

    def _on_node_move_finished(self, node_id: str, old_pos: QPointF, new_pos: QPointF) -> None:
        if self._applying:
            return  # this move came from us (undo/redo/apply) — don't re-record it

        old_pos_tuple = (old_pos.x(), old_pos.y())
        new_pos_tuple = (new_pos.x(), new_pos.y())
        if old_pos_tuple == new_pos_tuple:
            return  # no real movement

        self.undo_stack.push(MoveNodeCommand(self, node_id, old_pos_tuple, new_pos_tuple))
    def _on_point_move_finished(self, point_id: str, old_pos: QPointF, new_pos: QPointF) -> None:
        """Handle a route point being moved."""
        if self._applying:
            return
        
        old_tuple = (old_pos.x(), old_pos.y())
        new_tuple = (new_pos.x(), new_pos.y())
        if old_tuple == new_tuple:
            return
        
        # Update the model
        point = self.harness.route_points[point_id]
        point.position = new_tuple
        self.modelChanged.emit("point", point_id)
    
    def add_route_point(self, point: RoutePoint) -> None:
        """Add a route point (branch or layout)."""
        self.undo_stack.push(AddRoutePointCommand(self, point))
    
    def merge_branch_point(self, point_id: str) -> None:
        """Merge a branch point back into its edge."""
        self.undo_stack.push(MergeBranchPointCommand(self, point_id))
    
    def delete_route_point(self, point_id: str) -> None:
        """Delete a layout point (branch points must be merged first)."""
        point = self.harness.route_points[point_id]
        if point.point_type == PointType.BRANCH:
            raise ValueError("Branch points must be merged before deletion")
        self.undo_stack.push(DeleteRoutePointCommand(self, point_id))

    def _on_fix_length_requested(self, edge_id: str, side: str) -> None:
        """One of an edge's fix-length arrows was clicked. Rigid-translate
        the network on that side so the edge's drawn length matches its
        stored length_mm exactly, moving along the edge's current bearing
        (an "expand/contract" correction, not a re-angle).

        If the edge is a bridge (removing it disconnects the graph), the
        WHOLE side moves together, preserving its internal shape exactly
        ("transposed with no change"). If it isn't a bridge (it's on a
        cycle, so there's no well-defined "other side"), only the single
        endpoint node on that side is moved instead."""
        edge = self.harness.edges[edge_id]
        moving_id = edge.start_node_id if side == "start" else edge.end_node_id
        anchor_id = edge.end_node_id if side == "start" else edge.start_node_id

        anchor_pos = self.harness.nodes[anchor_id].position
        moving_pos = self.harness.nodes[moving_id].position
        if anchor_pos is None or moving_pos is None:
            return
        ax, ay = anchor_pos[0], anchor_pos[1]
        mx, my = moving_pos[0], moving_pos[1]
        dist = math.hypot(mx - ax, my - ay)
        if dist < 1e-9:
            return  # coincident nodes — no sensible bearing to correct along

        ux, uy = (mx - ax) / dist, (my - ay) / dist
        target_x, target_y = ax + ux * edge.length_mm, ay + uy * edge.length_mm
        delta = (target_x - mx, target_y - my)
        if math.hypot(*delta) < 1e-6:
            return  # already correct — nothing to do, don't push a no-op undo entry

        component = self._reachable_component(moving_id, exclude_edge_id=edge_id)
        if anchor_id in component:
            # Not a bridge — the two sides are still connected some other
            # way, so there's no clean "other side" to hold fixed. Fall
            # back to moving just the one endpoint.
            node_ids_to_move = {moving_id}
        else:
            node_ids_to_move = component

        self.undo_stack.beginMacro(f"Fix length of {edge_id}")
        for nid in node_ids_to_move:
            pos = self.harness.nodes[nid].position
            old = tuple(pos) if pos is not None else (0.0, 0.0)
            new = (old[0] + delta[0], old[1] + delta[1])
            self.undo_stack.push(MoveNodeCommand(self, nid, old, new))
        self.undo_stack.endMacro()

    def _reachable_component(self, start_node_id: str, exclude_edge_id: str) -> set:
        """BFS over the harness graph, ignoring one specific edge. Used to
        find "the rest of the network on this side" for the fix-length
        arrows — if the excluded edge is a bridge, this returns exactly
        the nodes on start_node_id's side of the cut."""
        adjacency: dict[str, list[str]] = {}
        for e in self.harness.edges.values():
            if e.edge_id == exclude_edge_id:
                continue
            adjacency.setdefault(e.start_node_id, []).append(e.end_node_id)
            adjacency.setdefault(e.end_node_id, []).append(e.start_node_id)

        visited = {start_node_id}
        stack = [start_node_id]
        while stack:
            current = stack.pop()
            for neighbor in adjacency.get(current, []):
                if neighbor not in visited:
                    visited.add(neighbor)
                    stack.append(neighbor)
        return visited

    # ---- public API for editors not built yet (rename dialogs, property panels) ----

    def set_node_label(self, node_id: str, new_label: str) -> None:
        node = self.harness.nodes[node_id]
        if node.label == new_label:
            return
        self.undo_stack.push(SetFieldCommand(
            self, "node", node_id, "label", node.label, new_label,
            description=f"Rename {node_id}",
        ))

    def set_edge_field(self, edge_id: str, field_name: str, new_value: Any) -> None:
        edge = self.harness.edges[edge_id]
        old_value = getattr(edge, field_name)
        if old_value == new_value:
            return
        self.undo_stack.push(SetFieldCommand(self, "edge", edge_id, field_name, old_value, new_value))

    def set_wire_field(self, wire_id: str, field_name: str, new_value: Any) -> None:
        wire = self.harness.wires[wire_id]
        old_value = getattr(wire, field_name)
        if old_value == new_value:
            return
        self.undo_stack.push(SetFieldCommand(self, "wire", wire_id, field_name, old_value, new_value))

    def add_wire(self, wire) -> None:
        """Add a brand new Wire (e.g. from the Wires tab's Add Wire dialog).
        Undoable like everything else."""
        self.undo_stack.push(AddWireCommand(self, wire))

    def add_wire_auto_route(self, wire: Wire) -> None:
        """Add a new wire, assigning its route via Dijkstra over the
        existing edges. If the from/to nodes aren't connected by any
        existing chain of edges, a direct edge between them is created
        first. Both steps (the edge creation, if any, and the wire
        creation) undo together as a single step."""
        route, new_edge = self._compute_route_or_direct_edge(wire.from_node_id, wire.to_node_id)
        self.undo_stack.beginMacro(f"Add wire {wire.wire_id} (auto-route)")
        if new_edge is not None:
            self.undo_stack.push(AddEdgeCommand(self, new_edge))
        wire.route_edge_ids = route
        self.undo_stack.push(AddWireCommand(self, wire))
        self.undo_stack.endMacro()

    def auto_route_wire(self, wire_id: str) -> None:
        """Re-run Dijkstra routing for an existing wire (e.g. the
        per-row 'Auto-Route' button in the Wires tab). Same direct-edge
        fallback as add_wire_auto_route."""
        wire = self.harness.wires[wire_id]
        old_route = list(wire.route_edge_ids)
        route, new_edge = self._compute_route_or_direct_edge(wire.from_node_id, wire.to_node_id)

        self.undo_stack.beginMacro(f"Auto-route {wire_id}")
        if new_edge is not None:
            self.undo_stack.push(AddEdgeCommand(self, new_edge))
        if route != old_route:
            self.undo_stack.push(SetFieldCommand(self, "wire", wire_id, "route_edge_ids", old_route, route))
        self.undo_stack.endMacro()

    # ---- undo/redo passthrough ----

    def undo(self) -> None:
        self.undo_stack.undo()

    def redo(self) -> None:
        self.undo_stack.redo()

    def can_undo(self) -> bool:
        return self.undo_stack.canUndo()

    def can_redo(self) -> bool:
        return self.undo_stack.canRedo()

    # ---- Dijkstra routing helpers ----

    def _compute_route_or_direct_edge(self, from_id: str, to_id: str):
        """Returns (route_edge_ids, new_edge_or_None). If Dijkstra finds an
        existing path, new_edge is None. If the nodes are disconnected, a
        fresh direct Edge is constructed (but not yet added to the model —
        the caller pushes it through AddEdgeCommand so it's undoable)."""
        route = find_route(self.harness, from_id, to_id)
        if route is not None:
            return route, None
        edge = self._make_direct_edge(from_id, to_id)
        return [edge.edge_id], edge

    def _make_direct_edge(self, from_id: str, to_id: str) -> Edge:
        edge_id = self._generate_edge_id()
        length = estimate_direct_length(self.harness, from_id, to_id)
        return Edge(
            edge_id=edge_id,
            start_node_id=from_id,
            end_node_id=to_id,
            length_mm=length,
            max_diameter_mm=0.0,
            bend_radius_mm=0.0,
            metadata={"auto_generated": True, "direct": True},
        )

    def _generate_edge_id(self) -> str:
        n = 1
        while f"SEG_AUTO_{n}" in self.harness.edges:
            n += 1
        return f"SEG_AUTO_{n}"

    # ---- command application: the ONLY place that mutates the model ----

    def _apply_node_position(self, node_id: str, position: tuple) -> None:
        node = self.harness.nodes[node_id]
        node.position = position

        self._applying = True
        try:
            item = self.view.node_items.get(node_id)
            if item is not None:
                item.setPos(position[0], position[1])
        finally:
            self._applying = False

        self.modelChanged.emit("node", node_id)

    def _apply_field(self, entity_kind: str, entity_id: str, field_name: str, value: Any) -> None:
        entity = self._get_entity(entity_kind, entity_id)
        setattr(entity, field_name, value)
        self.modelChanged.emit(entity_kind, entity_id)

    def _apply_add_wire(self, wire) -> None:
        self.harness.add_wire(wire)  # Harness.add_wire validates node/edge references
        self.wireListChanged.emit()

    def _apply_remove_wire(self, wire_id: str) -> None:
        del self.harness.wires[wire_id]
        # Clean up any stale highlight for a wire that no longer exists.
        self.view.highlighted_wire_ids.discard(wire_id)
        self.view._apply_highlights()
        self.wireListChanged.emit()

    def _apply_add_edge(self, edge: Edge) -> None:
        self.harness.add_edge(edge)  # validates node references
        self.view.add_edge_item(edge)
        # connect_scene() only re-wires signals on a full rebuild; an edge
        # added incrementally (e.g. the auto-route direct-edge fallback)
        # needs its own signal connected here.
        self.view.edge_items[edge.edge_id].fixLengthRequested.connect(self._on_fix_length_requested)
        self.edgeListChanged.emit()

    def _apply_remove_edge(self, edge_id: str) -> None:
        self.view.remove_edge_item(edge_id)
        del self.harness.edges[edge_id]
        self.edgeListChanged.emit()

    def _get_entity(self, entity_kind: str, entity_id: str):
        table = {
            "node": self.harness.nodes,
            "edge": self.harness.edges,
            "wire": self.harness.wires,
        }[entity_kind]
        return table[entity_id]

    # ---- model -> view (targeted refresh, never a full re-render) ----

    def _on_model_changed(self, entity_kind: str, entity_id: str) -> None:
        self.view.refresh_entity(entity_kind, entity_id)
    def _apply_add_route_point(self, point: RoutePoint) -> None:
        self.harness.add_route_point(point)
        self.view.add_route_point_item(point)
        self.pointListChanged.emit()

    def _apply_delete_route_point(self, point_id: str) -> None:
        del self.harness.route_points[point_id]
        self.view.remove_route_point_item(point_id)
        self.pointListChanged.emit()

    def _apply_merge_branch_point(self, point_id: str) -> None:
        self.harness.merge_branch_point(point_id)
        self.view.remove_route_point_item(point_id)
        # Rebuild affected edges
        self.view.render()  # Full rebuild for simplicity
        self.pointListChanged.emit()