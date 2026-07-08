"""
harness_model.py

Data model for a wiring harness: Nodes (connectors/splices/inline joints),
Edges (physical bundle segments), and Wires (logical electrical circuits
routed through edges).

Design notes:
- Plain dataclasses -> trivial to_dict()/from_dict() -> json.dumps/loads.
- Every entity has a stable string ID so it maps cleanly to SQLite primary
  keys / foreign keys.
- Harness is the aggregate root: holds nodes/edges/wires, validates
  references, and knows how to (de)serialize itself to JSON and SQLite.
- `metadata` dict on each entity is a free-form JSON blob column, so you
  can bolt on extra attributes later (e.g. connector part number, wire
  insulation type) without a schema migration.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Optional


# --------------------------------------------------------------------------
# Enums
# --------------------------------------------------------------------------

class NodeType(str, Enum):
    CONNECTOR = "connector"
    SPLICE = "splice"
    INLINE_JOINT = "inline_joint"
    
class PointType(str, Enum):
    BRANCH = "branch"          # Can have connections/fasteners
    LAYOUT = "layout"          # Routing/visual aid only


# --------------------------------------------------------------------------
# Node (Connectors / Splices / Inline Joints)
# --------------------------------------------------------------------------

@dataclass
class Node:
    node_id: str
    node_type: NodeType
    label: str = ""
    position: Optional[tuple] = None  # (x, y, z) in mm, optional
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["node_type"] = self.node_type.value
        d["position"] = list(self.position) if self.position else None
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Node":
        return cls(
            node_id=d["node_id"],
            node_type=NodeType(d["node_type"]),
            label=d.get("label", ""),
            position=tuple(d["position"]) if d.get("position") else None,
            metadata=d.get("metadata", {}) or {},
        )

@dataclass
class RoutePoint:
    """A point along an edge that can be a branch point (can have wires
    connected/fasteners attached) or a layout point (visual/routing aid)."""
    point_id: str
    point_type: PointType
    edge_id: str               # The edge this point lies on
    position: tuple            # (x, y, z) in mm
    label: str = ""
    metadata: dict = field(default_factory=dict)
    
    def to_dict(self) -> dict:
        d = asdict(self)
        d["point_type"] = self.point_type.value
        d["position"] = list(self.position) if self.position else None
        return d
    
    @classmethod
    def from_dict(cls, d: dict) -> "RoutePoint":
        return cls(
            point_id=d["point_id"],
            point_type=PointType(d["point_type"]),
            edge_id=d["edge_id"],
            position=tuple(d["position"]) if d.get("position") else None,
            label=d.get("label", ""),
            metadata=d.get("metadata", {}) or {},
        )

# --------------------------------------------------------------------------
# Edge (Segments / Branches) — physical bundle casing between two nodes
# --------------------------------------------------------------------------

@dataclass
class Edge:
    edge_id: str
    start_node_id: str
    end_node_id: str
    length_mm: float
    max_diameter_mm: float
    bend_radius_mm: float
    length_locked: bool = False  
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Edge":
        return cls(
            edge_id=d["edge_id"],
            start_node_id=d["start_node_id"],
            end_node_id=d["end_node_id"],
            length_mm=d["length_mm"],
            max_diameter_mm=d["max_diameter_mm"],
            bend_radius_mm=d["bend_radius_mm"],
            length_locked=d.get("length_locked", False),
            metadata=d.get("metadata", {}) or {},
        )


# --------------------------------------------------------------------------
# Wire — logical circuit routed through one or more edges
# --------------------------------------------------------------------------

@dataclass
class Wire:
    wire_id: str
    gauge_mm2: float
    color: str
    from_node_id: str
    from_pin: str
    to_node_id: str
    to_pin: str
    route_edge_ids: list = field(default_factory=list)  # ordered edge path
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Wire":
        return cls(
            wire_id=d["wire_id"],
            gauge_mm2=d["gauge_mm2"],
            color=d["color"],
            from_node_id=d["from_node_id"],
            from_pin=d["from_pin"],
            to_node_id=d["to_node_id"],
            to_pin=d["to_pin"],
            route_edge_ids=d.get("route_edge_ids", []) or [],
            metadata=d.get("metadata", {}) or {},
        )


# --------------------------------------------------------------------------
# Harness — aggregate root
# --------------------------------------------------------------------------

class Harness:
    def __init__(self, harness_id: str, name: str = ""):
        self.harness_id = harness_id
        self.name = name
        self.nodes: dict[str, Node] = {}
        self.edges: dict[str, Edge] = {}
        self.wires: dict[str, Wire] = {}
        self.route_points: dict[str, RoutePoint] = {}
    # ---- mutators ----
    def add_route_point(self, point: RoutePoint) -> None:
        if point.edge_id not in self.edges:
            raise ValueError(f"Route point '{point.point_id}' references unknown edge '{point.edge_id}'")
        self.route_points[point.point_id] = point
    
    def split_edge_at_branch_point(self, edge_id: str, point_id: str, 
                                   point_pos: tuple) -> tuple[Edge, Edge]:
        """Split an edge at a branch point, creating two new edges.
        Returns (edge_a, edge_b) where edge_a goes from start->point and
        edge_b goes from point->end."""
        old_edge = self.edges[edge_id]
        
        # Calculate lengths based on positions
        start_pos = self.nodes[old_edge.start_node_id].position
        end_pos = self.nodes[old_edge.end_node_id].position
        if start_pos is None or end_pos is None:
            raise ValueError(f"Node positions missing for edge {edge_id}")
        
        # Calculate distances
        dist_start_to_point = math.hypot(
            point_pos[0] - start_pos[0],
            point_pos[1] - start_pos[1]
        )
        dist_point_to_end = math.hypot(
            end_pos[0] - point_pos[0],
            end_pos[1] - point_pos[1]
        )
        
        # Create two new edges
        edge_a = Edge(
            edge_id=f"{old_edge.edge_id}_A",
            start_node_id=old_edge.start_node_id,
            end_node_id=point_id,
            length_mm=dist_start_to_point,
            max_diameter_mm=old_edge.max_diameter_mm,
            bend_radius_mm=old_edge.bend_radius_mm,
            length_locked=old_edge.length_locked,
            metadata=old_edge.metadata.copy(),
        )
        
        edge_b = Edge(
            edge_id=f"{old_edge.edge_id}_B",
            start_node_id=point_id,
            end_node_id=old_edge.end_node_id,
            length_mm=dist_point_to_end,
            max_diameter_mm=old_edge.max_diameter_mm,
            bend_radius_mm=old_edge.bend_radius_mm,
            length_locked=old_edge.length_locked,
            metadata=old_edge.metadata.copy(),
        )
        
        # Remove old edge, add new ones
        del self.edges[edge_id]
        self.edges[edge_a.edge_id] = edge_a
        self.edges[edge_b.edge_id] = edge_b
        
        # Update any wires that used this edge
        for wire in self.wires.values():
            if edge_id in wire.route_edge_ids:
                idx = wire.route_edge_ids.index(edge_id)
                wire.route_edge_ids[idx:idx+1] = [edge_a.edge_id, edge_b.edge_id]
        
        return edge_a, edge_b
    
    def merge_branch_point(self, point_id: str) -> None:
        """Merge a branch point back into its parent edge, like in RapidHarness."""
        point = self.route_points[point_id]
        if point.point_type != PointType.BRANCH:
            return  # Only merge branch points
        
        # Find the two edges that connect to this point
        connected_edges = [
            e for e in self.edges.values()
            if e.start_node_id == point_id or e.end_node_id == point_id
        ]
        
        if len(connected_edges) != 2:
            return  # Can't merge if not exactly 2 edges
        
        # Determine which edge is the "main" one and which is the branch
        # For simplicity, merge them back into one edge
        edge_a, edge_b = connected_edges[0], connected_edges[1]
        
        # Determine start and end nodes
        start_id = edge_a.start_node_id if edge_a.start_node_id != point_id else edge_a.end_node_id
        end_id = edge_b.end_node_id if edge_b.start_node_id == point_id else edge_b.start_node_id
        
        # Create merged edge
        merged_edge = Edge(
            edge_id=point_id,  # Reuse the point ID as the edge ID
            start_node_id=start_id,
            end_node_id=end_id,
            length_mm=edge_a.length_mm + edge_b.length_mm,
            max_diameter_mm=max(edge_a.max_diameter_mm, edge_b.max_diameter_mm),
            bend_radius_mm=min(edge_a.bend_radius_mm, edge_b.bend_radius_mm),
            length_locked=edge_a.length_locked or edge_b.length_locked,
            metadata=edge_a.metadata.copy(),
        )
        
        # Remove old edges and point
        del self.edges[edge_a.edge_id]
        del self.edges[edge_b.edge_id]
        del self.route_points[point_id]
        self.edges[merged_edge.edge_id] = merged_edge
        
        # Update wires
        for wire in self.wires.values():
            route = wire.route_edge_ids
            if edge_a.edge_id in route and edge_b.edge_id in route:
                idx_a = route.index(edge_a.edge_id)
                idx_b = route.index(edge_b.edge_id)
                if idx_a < idx_b:
                    route[idx_a:idx_b+1] = [merged_edge.edge_id]
                else:
                    route[idx_b:idx_a+1] = [merged_edge.edge_id]

    def add_node(self, node: Node) -> None:
        self.nodes[node.node_id] = node

    def add_edge(self, edge: Edge) -> None:
        for nid in (edge.start_node_id, edge.end_node_id):
            if nid not in self.nodes:
                raise ValueError(f"Edge '{edge.edge_id}' references unknown node '{nid}'")
        self.edges[edge.edge_id] = edge

    def add_wire(self, wire: Wire) -> None:
        for nid in (wire.from_node_id, wire.to_node_id):
            if nid not in self.nodes:
                raise ValueError(f"Wire '{wire.wire_id}' references unknown node '{nid}'")
        for eid in wire.route_edge_ids:
            if eid not in self.edges:
                raise ValueError(f"Wire '{wire.wire_id}' references unknown edge '{eid}'")
        self.wires[wire.wire_id] = wire

    # ---- JSON ----

    def to_dict(self) -> dict:
        return {
            "harness_id": self.harness_id,
            "name": self.name,
            "nodes": [n.to_dict() for n in self.nodes.values()],
            "edges": [e.to_dict() for e in self.edges.values()],
            "wires": [w.to_dict() for w in self.wires.values()],
            "route_points": [p.to_dict() for p in self.route_points.values()],
        }


    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)

    @classmethod
    def from_dict(cls, d: dict) -> "Harness":
        h = cls(harness_id=d["harness_id"], name=d.get("name", ""))
        for nd in d.get("nodes", []):
            h.add_node(Node.from_dict(nd))
        for ed in d.get("edges", []):
            h.add_edge(Edge.from_dict(ed))
        for wd in d.get("wires", []):
            h.add_wire(Wire.from_dict(wd))
        for pd in d.get("route_points", []):
            h.add_route_point(RoutePoint.from_dict(pd))
        return h


    @classmethod
    def from_json(cls, s: str) -> "Harness":
        return cls.from_dict(json.loads(s))

    def save_json(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as f:
            f.write(self.to_json())

    @classmethod
    def load_json(cls, path: str) -> "Harness":
        with open(path, "r", encoding="utf-8") as f:
            return cls.from_json(f.read())

    # ---- SQLite ----

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS harness (
        harness_id TEXT PRIMARY KEY,
        name TEXT
    );

    CREATE TABLE IF NOT EXISTS node (
        node_id TEXT PRIMARY KEY,
        harness_id TEXT NOT NULL REFERENCES harness(harness_id) ON DELETE CASCADE,
        node_type TEXT NOT NULL,
        label TEXT,
        pos_x REAL,
        pos_y REAL,
        pos_z REAL,
        metadata TEXT
    );

    CREATE TABLE IF NOT EXISTS edge (
        edge_id TEXT PRIMARY KEY,
        harness_id TEXT NOT NULL REFERENCES harness(harness_id) ON DELETE CASCADE,
        start_node_id TEXT NOT NULL REFERENCES node(node_id),
        end_node_id TEXT NOT NULL REFERENCES node(node_id),
        length_mm REAL,
        max_diameter_mm REAL,
        bend_radius_mm REAL,
        length_locked INTEGER,
        metadata TEXT
    );

    CREATE TABLE IF NOT EXISTS wire (
        wire_id TEXT PRIMARY KEY,
        harness_id TEXT NOT NULL REFERENCES harness(harness_id) ON DELETE CASCADE,
        gauge_mm2 REAL,
        color TEXT,
        from_node_id TEXT NOT NULL REFERENCES node(node_id),
        from_pin TEXT,
        to_node_id TEXT NOT NULL REFERENCES node(node_id),
        to_pin TEXT,
        metadata TEXT
    );

    CREATE TABLE IF NOT EXISTS wire_route (
        wire_id TEXT NOT NULL REFERENCES wire(wire_id) ON DELETE CASCADE,
        edge_id TEXT NOT NULL REFERENCES edge(edge_id),
        seq INTEGER NOT NULL,
        PRIMARY KEY (wire_id, seq)
    );
    """

    @staticmethod
    def init_db(conn: sqlite3.Connection) -> None:
        conn.executescript(Harness.SCHEMA)
        conn.commit()

    def save_sqlite(self, conn: sqlite3.Connection) -> None:
        """Insert/replace this harness and all its children into an open sqlite3 connection."""
        Harness.init_db(conn)
        cur = conn.cursor()

        cur.execute(
            "INSERT OR REPLACE INTO harness (harness_id, name) VALUES (?, ?)",
            (self.harness_id, self.name),
        )

        # Clear old children for this harness (simple full-replace strategy)
        cur.execute("DELETE FROM wire_route WHERE wire_id IN "
                    "(SELECT wire_id FROM wire WHERE harness_id = ?)", (self.harness_id,))
        cur.execute("DELETE FROM wire WHERE harness_id = ?", (self.harness_id,))
        cur.execute("DELETE FROM edge WHERE harness_id = ?", (self.harness_id,))
        cur.execute("DELETE FROM node WHERE harness_id = ?", (self.harness_id,))

        for n in self.nodes.values():
            pos = n.position or (None, None, None)
            if len(pos) == 2:
                pos = (pos[0], pos[1], None)
            cur.execute(
                """INSERT INTO node
                   (node_id, harness_id, node_type, label, pos_x, pos_y, pos_z, metadata)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (n.node_id, self.harness_id, n.node_type.value, n.label,
                 pos[0], pos[1], pos[2], json.dumps(n.metadata)),
            )

        for e in self.edges.values():
            cur.execute(
                """INSERT INTO edge
                   (edge_id, harness_id, start_node_id, end_node_id,
                    length_mm, max_diameter_mm, bend_radius_mm, length_locked, metadata)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (e.edge_id, self.harness_id, e.start_node_id, e.end_node_id,
                 e.length_mm, e.max_diameter_mm, e.bend_radius_mm,
                 int(e.length_locked), json.dumps(e.metadata)),
            )

        for w in self.wires.values():
            cur.execute(
                """INSERT INTO wire
                   (wire_id, harness_id, gauge_mm2, color,
                    from_node_id, from_pin, to_node_id, to_pin, metadata)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (w.wire_id, self.harness_id, w.gauge_mm2, w.color,
                 w.from_node_id, w.from_pin, w.to_node_id, w.to_pin, json.dumps(w.metadata)),
            )
            for seq, edge_id in enumerate(w.route_edge_ids):
                cur.execute(
                    "INSERT INTO wire_route (wire_id, edge_id, seq) VALUES (?, ?, ?)",
                    (w.wire_id, edge_id, seq),
                )

        conn.commit()

    @classmethod
    def load_sqlite(cls, conn: sqlite3.Connection, harness_id: str) -> "Harness":
        cur = conn.cursor()

        row = cur.execute(
            "SELECT harness_id, name FROM harness WHERE harness_id = ?", (harness_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"No harness found with id '{harness_id}'")
        h = cls(harness_id=row[0], name=row[1] or "")

        for (node_id, node_type, label, px, py, pz, meta) in cur.execute(
            "SELECT node_id, node_type, label, pos_x, pos_y, pos_z, metadata "
            "FROM node WHERE harness_id = ?", (harness_id,)
        ):
            pos = (px, py, pz) if px is not None else None
            h.add_node(Node(
                node_id=node_id,
                node_type=NodeType(node_type),
                label=label or "",
                position=pos,
                metadata=json.loads(meta) if meta else {},
            ))

        for (edge_id, start_id, end_id, length_mm, max_d, bend_r, locked, meta) in cur.execute(
            "SELECT edge_id, start_node_id, end_node_id, length_mm, "
            "max_diameter_mm, bend_radius_mm, length_locked, metadata FROM edge WHERE harness_id = ?",
            (harness_id,)
        ):
            h.add_edge(Edge(
                edge_id=edge_id,
                start_node_id=start_id,
                end_node_id=end_id,
                length_mm=length_mm,
                max_diameter_mm=max_d,
                bend_radius_mm=bend_r,
                length_locked=bool(locked),
                metadata=json.loads(meta) if meta else {},
            ))

        wire_rows = cur.execute(
            "SELECT wire_id, gauge_mm2, color, from_node_id, from_pin, "
            "to_node_id, to_pin, metadata FROM wire WHERE harness_id = ?",
            (harness_id,)
        ).fetchall()

        for (wire_id, gauge, color, from_id, from_pin, to_id, to_pin, meta) in wire_rows:
            route = [
                r[0] for r in cur.execute(
                    "SELECT edge_id FROM wire_route WHERE wire_id = ? ORDER BY seq",
                    (wire_id,)
                )
            ]
            h.add_wire(Wire(
                wire_id=wire_id,
                gauge_mm2=gauge,
                color=color,
                from_node_id=from_id,
                from_pin=from_pin,
                to_node_id=to_id,
                to_pin=to_pin,
                route_edge_ids=route,
                metadata=json.loads(meta) if meta else {},
            ))

        return h


# --------------------------------------------------------------------------
# Demo / smoke test
# --------------------------------------------------------------------------

if __name__ == "__main__":
    h = Harness(harness_id="H001", name="Engine Bay Harness")

    h.add_node(Node("CONN_A", NodeType.CONNECTOR, label="ECU Connector"))
    h.add_node(Node("SPLICE_1", NodeType.SPLICE, label="Ground Splice"))
    h.add_node(Node("CONN_B", NodeType.CONNECTOR, label="Sensor Connector"))

    h.add_edge(Edge("SEG_1", "CONN_A", "SPLICE_1",
                     length_mm=350.0, max_diameter_mm=8.5, bend_radius_mm=25.0))
    h.add_edge(Edge("SEG_2", "SPLICE_1", "CONN_B",
                     length_mm=220.0, max_diameter_mm=6.0, bend_radius_mm=18.0))

    h.add_wire(Wire(
        wire_id="W001", gauge_mm2=0.75, color="red/black",
        from_node_id="CONN_A", from_pin="1",
        to_node_id="CONN_B", to_pin="3",
        route_edge_ids=["SEG_1", "SEG_2"],
    ))

    # JSON round trip
    json_str = h.to_json()
    print(json_str)
    h2 = Harness.from_json(json_str)
    assert h2.wires["W001"].color == "red/black"

    # SQLite round trip (in-memory)
    conn = sqlite3.connect(":memory:")
    h.save_sqlite(conn)
    h3 = Harness.load_sqlite(conn, "H001")
    assert h3.edges["SEG_2"].length_mm == 220.0
    assert h3.wires["W001"].route_edge_ids == ["SEG_1", "SEG_2"]
    print("\nSQLite round-trip OK.")