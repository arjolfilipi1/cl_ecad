"""
harness_controller.py

Mediator between the Harness model (harness_model.py) and the
HarnessView/graphics items (harness_view.py).

Responsibilities:
- Own the QUndoStack. Every mutation — node moved, a field renamed or
  edited, anything — goes through a QUndoCommand pushed here, so it's
  automatically undoable/redoable.
- Intercept change signals coming from the scene's graphics items
  (currently: NodeGraphicsItem.positionChanged). Future signals — a
  label being edited in place, a property panel editing an edge/wire
  field — plug into this the same way.
- The Harness model is the single source of truth. The controller is the
  ONLY thing allowed to mutate it. Graphics items never touch the model
  directly; they only report "this changed" via signals or explicit
  controller calls.
- After a change is applied to the model, the controller tells the view
  to refresh just the affected item — it never re-renders the whole
  scene for a single edit.

Not yet implemented (comes with the actual CAD move/add/rename UI):
- Coalescing a mouse-drag's many intermediate itemChange events into a
  single undo command (drag should commit once, on release).
- Add/delete commands for nodes, edges, wires.
- Signals for label-in-place-editing / property-panel edits, though the
  controller methods for committing such edits already exist below
  (set_node_label, set_edge_field, set_wire_field) and just need a UI to
  call them.
"""

from __future__ import annotations

from typing import Any, Optional

from PyQt5.QtCore import QObject, QPointF, pyqtSignal
from PyQt5.QtWidgets import QUndoCommand, QUndoStack

from harness_model import Harness


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

    def __init__(self, harness: Harness, view, main_window: Optional[QObject] = None):
        super().__init__(main_window)
        self.main_window = main_window
        self.harness = harness
        self.view = view
        self.undo_stack = QUndoStack(self)

        # Guards against the controller's own model->view writes (e.g. an
        # undo/redo calling item.setPos()) being mistaken for a fresh user
        # edit and re-recorded as a new command.
        self._applying = False

        self.modelChanged.connect(self._on_model_changed)

        # Reconnect to the scene's items whenever the view rebuilds them
        # (e.g. on File > Open), and connect now in case items already
        # exist.
        main_window.sceneRebuilt.connect(self.connect_scene)
        self.connect_scene()

    # ---- wiring into the scene ----

    def connect_scene(self) -> None:
        """(Re)connect to every node item currently in the view, and
        re-sync self.harness. Safe to call repeatedly — old items are gone
        after a rebuild, so there's nothing stale to disconnect. Loading a
        new file replaces view.harness with a brand new Harness instance,
        so the controller must track that too or it would keep mutating
        (and undo/redo-ing) a discarded model."""
        self.harness = self.main_window.harness
        self.undo_stack.clear()  # old commands would reference a now-discarded document
        for node_item in self.main_window.node_items.values():
            node_item.positionChanged.connect(self._on_node_position_changed)

    # ---- scene -> model (change interception) ----

    def _on_node_position_changed(self, node_id: str, new_pos: QPointF) -> None:
        if self._applying:
            return  # this move came from us (undo/redo/apply) — don't re-record it
        
        node = self.harness.nodes[node_id]
        old_pos = tuple(node.position) if node.position is not None else (0.0, 0.0)
        new_pos_tuple = (round(new_pos.x(),-1), round(new_pos.y(),-1))
        print(new_pos_tuple)
        if old_pos[:2] == new_pos_tuple:
            return  # no real movement

        self.undo_stack.push(MoveNodeCommand(self, node_id, old_pos, new_pos_tuple))

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

    # ---- undo/redo passthrough ----

    def undo(self) -> None:
        self.undo_stack.undo()

    def redo(self) -> None:
        self.undo_stack.redo()

    def can_undo(self) -> bool:
        return self.undo_stack.canUndo()

    def can_redo(self) -> bool:
        return self.undo_stack.canRedo()

    # ---- command application: the ONLY place that mutates the model ----

    def _apply_node_position(self, node_id: str, position: tuple) -> None:
        node = self.harness.nodes[node_id]
        node.position = position

        self._applying = True
        try:
            item = self.main_window.node_items.get(node_id)
            if item is not None:
                item.setPos(position[0], position[1])
        finally:
            self._applying = False

        self.modelChanged.emit("node", node_id)

    def _apply_field(self, entity_kind: str, entity_id: str, field_name: str, value: Any) -> None:
        entity = self._get_entity(entity_kind, entity_id)
        setattr(entity, field_name, value)
        self.modelChanged.emit(entity_kind, entity_id)

    def _get_entity(self, entity_kind: str, entity_id: str):
        table = {
            "node": self.harness.nodes,
            "edge": self.harness.edges,
            "wire": self.harness.wires,
        }[entity_kind]
        return table[entity_id]

    # ---- model -> view (targeted refresh, never a full re-render) ----

    def _on_model_changed(self, entity_kind: str, entity_id: str) -> None:
        self.main_window.refresh_entity(entity_kind, entity_id)
