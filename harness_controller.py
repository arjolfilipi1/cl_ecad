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
from PyQt5.QtWidgets import QUndoCommand, QUndoStack,QDialog

from harness_model import Harness, Node, Edge, Wire, NodeType, BRANCH_MERGE_DISTANCE_MM
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
    fallback when Dijkstra routing finds no existing path, and for the
    two new segments created when an edge is split)."""

    def __init__(self, controller: "HarnessController", edge: Edge):
        super().__init__(f"Add edge {edge.edge_id}")
        self.controller = controller
        self.edge = edge

    def redo(self) -> None:
        self.controller._apply_add_edge(self.edge)

    def undo(self) -> None:
        self.controller._apply_remove_edge(self.edge.edge_id)


class DeleteEdgeCommand(QUndoCommand):
    """Undoable deletion of an existing Edge — the inverse of
    AddEdgeCommand. Used when splitting an edge (the original edge is
    deleted and replaced by two new ones)."""

    def __init__(self, controller: "HarnessController", edge: Edge):
        super().__init__(f"Delete edge {edge.edge_id}")
        self.controller = controller
        self.edge = edge  # kept so undo can re-add it exactly as it was

    def redo(self) -> None:
        self.controller._apply_remove_edge(self.edge.edge_id)

    def undo(self) -> None:
        self.controller._apply_add_edge(self.edge)


class AddNodeCommand(QUndoCommand):
    """Undoable creation of a new Node (used for branch/layout points
    added by splitting an edge)."""

    def __init__(self, controller: "HarnessController", node: Node):
        super().__init__(f"Add {node.node_type.value} {node.node_id}")
        self.controller = controller
        self.node = node

    def redo(self) -> None:
        self.controller._apply_add_node(self.node)

    def undo(self) -> None:
        self.controller._apply_remove_node(self.node.node_id)


class MergeBranchPointsCommand(QUndoCommand):
    """Undoable structural merge of one BRANCH_POINT onto another.
    Reassigns external edges to the target, swallows connecting edges,
    and hides the moved node while retaining its data for unmerging."""

    def __init__(self, controller: "HarnessController", moved_node_id: str, target_node_id: str,
                 pre_merge_pos: tuple, target_pos: tuple, old_metadata: dict,reassigned_edges: list):
        super().__init__(f"Merge {moved_node_id} into {target_node_id}")
        self.controller = controller
        self.moved_node_id = moved_node_id
        self.target_node_id = target_node_id
        self.pre_merge_pos = pre_merge_pos
        self.target_pos = target_pos
        self.old_metadata = old_metadata

        self.edges_to_reassign = []  # [(edge_id, field_name_to_change)]
        self.edges_to_swallow = []   # [Edge objects]
        self.wires_to_update = []    # [(wire_id, old_route, new_route)]

        # Analyze topology to determine what happens to the edges
        for edge in list(controller.harness.edges.values()):
            if self.moved_node_id in (edge.start_node_id, edge.end_node_id):
                other_node = edge.end_node_id if edge.start_node_id == self.moved_node_id else edge.start_node_id
                
                if other_node == self.target_node_id:
                    # Edge connects the two merging nodes directly; swallow it
                    self.edges_to_swallow.append(edge)
                else:
                    # Edge goes somewhere else; re-anchor it to the target
                    field = "start_node_id" if edge.start_node_id == self.moved_node_id else "end_node_id"
                    self.edges_to_reassign.append((edge.edge_id, field))

        # Analyze wires passing through swallowed edges
        swallow_ids = {e.edge_id for e in self.edges_to_swallow}
        if swallow_ids:
            for wire in controller.harness.wires.values():
                if any(eid in swallow_ids for eid in wire.route_edge_ids):
                    new_route = [eid for eid in wire.route_edge_ids if eid not in swallow_ids]
                    self.wires_to_update.append((wire.wire_id, list(wire.route_edge_ids), new_route))

    def redo(self) -> None:
        # 1. Update wire routes to bypass the edges we are about to swallow
        for wire_id, _, new_route in self.wires_to_update:
            self.controller._apply_field("wire", wire_id, "route_edge_ids", new_route)

        # 2. Remove swallowed edges from the active graph
        for edge in self.edges_to_swallow:
            self.controller._apply_remove_edge(edge.edge_id)

        # 3. Re-anchor remaining edges to the target node
        for edge_id, field in self.edges_to_reassign:
            self.controller._apply_field("edge", edge_id, field, self.target_node_id)

        # 4. Update the merged node metadata (This makes it invisible via the view refresh)
        self.controller._apply_merge(self.moved_node_id, self.target_node_id, self.target_pos, self.pre_merge_pos)

    def undo(self) -> None:
        # 1. Un-ghost the node by restoring old metadata
        self.controller._apply_unmerge(self.moved_node_id, self.pre_merge_pos, self.old_metadata)

        # 2. Re-anchor edges back to the original node
        for edge_id, field in self.edges_to_reassign:
            self.controller._apply_field("edge", edge_id, field, self.moved_node_id)

        # 3. Restore swallowed edges back to the graph
        for edge in self.edges_to_swallow:
            self.controller._apply_add_edge(edge)

        # 4. Restore original wire routes
        for wire_id, old_route, _ in self.wires_to_update:
            self.controller._apply_field("wire", wire_id, "route_edge_ids", old_route)


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

    # Same idea, for nodes — fires when a branch/layout point is added or
    # removed (e.g. by splitting an edge). No Nodes tab consumes this yet,
    # but it's here for when one exists.
    nodeListChanged = pyqtSignal()

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
            edge_item.splitRequested.connect(self._on_split_requested)
            edge_item.editRequested.connect(self._on_edge_edit_requested)
    # ---- scene -> model (change interception) ----
    def _on_edge_edit_requested(self, edge_id: str) -> None:
        """Open the edge edit dialog for the given edge."""
        # Find the main window - could use a signal or go up the hierarchy
        # Option: Use the parent chain to find MainWindow
        parent = self.parent()
        while parent:
            if hasattr(parent, 'edges_tab') and hasattr(parent, 'view'):
                # We found the main window
                main_window = parent
                # The edges tab has refresh, but we need to open the edit dialog
                # We can open it directly
                print(edge_id)
                edge = self.harness.edges.get(edge_id)
                if edge is not None:
                    from wires_panel import EditEdgeDialog
                    dialog = EditEdgeDialog(edge, self.harness, main_window)
                    if dialog.exec() == QDialog.Accepted:
                        updated_values = dialog.get_updated_values()
                        for field_name, new_value in updated_values.items():
                            old_value = getattr(edge, field_name)
                            if old_value != new_value:
                                self.set_edge_field(edge_id, field_name, new_value)
                return
            parent = parent.parent()

    def _on_node_move_finished(self, node_id: str, old_pos: QPointF, new_pos: QPointF) -> None:
        if self._applying:
            return  # this move came from us (undo/redo/apply) — don't re-record it

        old_pos_tuple = (old_pos.x(), old_pos.y())
        new_pos_tuple = (new_pos.x(), new_pos.y())
        if old_pos_tuple == new_pos_tuple:
            return  # no real movement

        node = self.harness.nodes[node_id]
        if node.node_type == NodeType.BRANCH_POINT:
            target_id = self._find_merge_target(node_id, new_pos_tuple)
            if target_id is not None:
                target_pos = self.harness.nodes[target_id].position
                self.undo_stack.push(MergeBranchPointsCommand(
                    self, node_id, target_id, old_pos_tuple, tuple(target_pos), dict(node.metadata)
                ))
                return

        self.undo_stack.push(MoveNodeCommand(self, node_id, old_pos_tuple, new_pos_tuple))

    def _find_merge_target(self, moving_node_id: str, position: tuple) -> Optional[str]:
        """Closest OTHER branch point within BRANCH_MERGE_DISTANCE_MM, if
        any (mirrors NodeGraphicsItem._nearby_branch_point's live preview,
        so what turned green during the drag is exactly what merges)."""
        mx, my = position
        best_id, best_dist = None, BRANCH_MERGE_DISTANCE_MM
        for nid, n in self.harness.nodes.items():
            if nid == moving_node_id or n.node_type != NodeType.BRANCH_POINT or n.position is None:
                continue
            dist = math.hypot(n.position[0] - mx, n.position[1] - my)
            if dist <= best_dist:
                best_dist = dist
                best_id = nid
        return best_id

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

    # ---- edge splitting (Add Branch Point / Add Layout Point) ----

    def _on_split_requested(self, edge_id: str, scene_pos: QPointF, node_type_value: str) -> None:
        self.split_edge(edge_id, scene_pos, NodeType(node_type_value))

    def split_edge(self, edge_id: str, scene_pos: QPointF, node_type: NodeType) -> None:
        """Interrupt an edge with a new node at (approximately) scene_pos,
        splitting it into two edges that meet there. Used for both branch
        points (future connection/fastener anchors) and layout points
        (routing/visual only) — they differ only in node_type. This is
        also how an edge ends up visually "bent": a layout point is just
        an ordinary node, so two straight edges meeting at it look like
        one bent path, with no separate polyline concept needed.

        The click position is projected onto the edge's current straight
        line (clamped away from the exact endpoints) rather than used
        verbatim, so the new node always sits exactly on the line.
        length_mm is split proportionally to where along the line that
        projection falls, so the two halves sum back to the original
        length exactly."""
        edge = self.harness.edges[edge_id]
        start_pos = self.harness.nodes[edge.start_node_id].position
        end_pos = self.harness.nodes[edge.end_node_id].position
        if start_pos is None or end_pos is None:
            return

        sx, sy = start_pos[0], start_pos[1]
        ex, ey = end_pos[0], end_pos[1]
        vx, vy = ex - sx, ey - sy
        seg_len_sq = vx * vx + vy * vy
        if seg_len_sq < 1e-9:
            t = 0.5
        else:
            wx, wy = scene_pos.x() - sx, scene_pos.y() - sy
            t = (vx * wx + vy * wy) / seg_len_sq
            t = min(max(t, 0.05), 0.95)  # keep the new node off the exact endpoints
        split_x, split_y = sx + vx * t, sy + vy * t

        prefix = "BRANCH_" if node_type == NodeType.BRANCH_POINT else "LAYOUT_"
        new_node_id = self._generate_id(prefix, self.harness.nodes)
        new_node = Node(node_id=new_node_id, node_type=node_type, label=new_node_id,
                         position=(split_x, split_y))

        new_edge_id = self._generate_id("SEG_SPLIT_", self.harness.edges)
        old_end_id = edge.end_node_id
        old_length = edge.length_mm
        length_1 = old_length * t
        length_2 = old_length - length_1

        new_edge = Edge(
            edge_id=new_edge_id,
            start_node_id=new_node_id,
            end_node_id=old_end_id,
            length_mm=length_2,
            max_diameter_mm=edge.max_diameter_mm,
            bend_radius_mm=edge.bend_radius_mm,
            length_locked=edge.length_locked,
        )

        self.undo_stack.beginMacro(f"Add {node_type.value} on {edge_id}")
        self.undo_stack.push(AddNodeCommand(self, new_node))
        self.undo_stack.push(SetFieldCommand(self, "edge", edge_id, "end_node_id", old_end_id, new_node_id))
        self.undo_stack.push(SetFieldCommand(self, "edge", edge_id, "length_mm", old_length, length_1))
        self.undo_stack.push(AddEdgeCommand(self, new_edge))

        # Keep any wire that was routed through the original edge
        # physically continuous — it now needs to also pass through the
        # new segment to still reach its original destination.
        for wire in self.harness.wires.values():
            if edge_id in wire.route_edge_ids:
                old_route = list(wire.route_edge_ids)
                idx = old_route.index(edge_id)
                new_route = old_route[:idx + 1] + [new_edge_id] + old_route[idx + 1:]
                self.undo_stack.push(SetFieldCommand(
                    self, "wire", wire.wire_id, "route_edge_ids", old_route, new_route,
                    description=f"Extend {wire.wire_id} route through {new_edge_id}",
                ))
        self.undo_stack.endMacro()

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
        return self._generate_id("SEG_AUTO_", self.harness.edges)

    def _generate_id(self, prefix: str, existing: dict) -> str:
        n = 1
        while f"{prefix}{n}" in existing:
            n += 1
        return f"{prefix}{n}"

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

        if entity_kind == "edge" and field_name in ("start_node_id", "end_node_id"):
            # Not just a data change — the graphics item needs to be
            # structurally rewired to a different NodeGraphicsItem, not
            # just repainted (used when splitting an edge).
            end = "start" if field_name == "start_node_id" else "end"
            self.view.reassign_edge_endpoint(entity_id, end, value)

        if entity_kind == "wire" and field_name == "route_edge_ids":
            # A wire's path changed (e.g. extended through a new split
            # segment) — if it's currently highlighted, the highlight
            # needs to immediately reflect the updated route.
            self.view._apply_highlights()

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
        self.view.edge_items[edge.edge_id].splitRequested.connect(self._on_split_requested)
        self.edgeListChanged.emit()

    def _apply_remove_edge(self, edge_id: str) -> None:
        self.view.remove_edge_item(edge_id)
        del self.harness.edges[edge_id]
        self.edgeListChanged.emit()

    def _apply_merge(self, moved_node_id: str, target_node_id: str, target_pos: tuple, pre_merge_pos: tuple) -> None:
        node = self.harness.nodes[moved_node_id]
        node.metadata["merged_into"] = target_node_id
        node.metadata["pre_merge_position"] = list(pre_merge_pos)
        # Position update goes through the normal path so the graphics
        # item, undo-guard, and modelChanged signal all behave exactly
        # like any other move.
        self._apply_node_position(moved_node_id, target_pos)
        self.view.refresh_entity("node", moved_node_id)  # picks up the merged-into tooltip text

    def _apply_unmerge(self, moved_node_id: str, pre_merge_pos: tuple, old_metadata: dict) -> None:
        node = self.harness.nodes[moved_node_id]
        node.metadata = dict(old_metadata)
        self._apply_node_position(moved_node_id, pre_merge_pos)
        self.view.refresh_entity("node", moved_node_id)

    def _apply_add_node(self, node: Node) -> None:
        self.harness.add_node(node)
        item = self.view.add_node_item(node)
        # connect_scene() only re-wires signals on a full rebuild; a node
        # added incrementally (splitting an edge) needs its own signal
        # connected here so it can be dragged like any other node.
        item.moveFinished.connect(self._on_node_move_finished)
        self.nodeListChanged.emit()

    def _apply_remove_node(self, node_id: str) -> None:
        self.view.remove_node_item(node_id)
        del self.harness.nodes[node_id]
        self.nodeListChanged.emit()

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