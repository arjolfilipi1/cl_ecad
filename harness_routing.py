"""
harness_routing.py

Pure graph logic for assigning a Wire's electrical path (route_edge_ids)
to physical Edges (bundle segments). No PyQt dependency here — this is
plain Harness-model logic, easy to unit test on its own.

find_route() runs Dijkstra's algorithm over the harness graph, treating
each Edge as an undirected connection between its start and end Node,
weighted by Edge.length_mm (shortest physical bundle path wins). If the
two nodes aren't connected by any chain of edges, it returns None — the
caller (HarnessController) is expected to fall back to creating a direct
edge between the two nodes in that case (see controller._make_direct_edge).
"""

from __future__ import annotations

import heapq
import math
from typing import Optional

from harness_model import Harness


def find_route(harness: Harness, start_node_id: str, end_node_id: str) -> Optional[list]:
    """Return the ordered list of edge_ids forming the shortest path (by
    length_mm) between two nodes, or None if they aren't connected by any
    existing chain of edges.

    Returns [] if start_node_id == end_node_id (nothing to traverse).
    """
    print("find_route")
    if start_node_id == end_node_id:
        return []
    if start_node_id not in harness.nodes or end_node_id not in harness.nodes:
        return None

    # Build an undirected adjacency list: node_id -> [(neighbor_id, weight, edge_id), ...]
    adjacency: dict[str, list[tuple[str, float, str]]] = {nid: [] for nid in harness.nodes}
    for edge in harness.edges.values():
        weight = max(edge.length_mm, 0.0) if edge.length_mm is not None else 0.0
        adjacency.setdefault(edge.start_node_id, []).append((edge.end_node_id, weight, edge.edge_id))
        adjacency.setdefault(edge.end_node_id, []).append((edge.start_node_id, weight, edge.edge_id))

    dist: dict[str, float] = {nid: math.inf for nid in harness.nodes}
    dist[start_node_id] = 0.0
    prev: dict[str, tuple[str, str]] = {}  # node_id -> (previous_node_id, edge_id used to arrive)
    visited: set[str] = set()
    heap: list[tuple[float, str]] = [(0.0, start_node_id)]

    while heap:
        d, node_id = heapq.heappop(heap)
        if node_id in visited:
            continue
        visited.add(node_id)
        if node_id == end_node_id:
            break
        for neighbor_id, weight, edge_id in adjacency.get(node_id, []):
            if neighbor_id in visited:
                continue
            new_dist = d + weight
            if new_dist < dist.get(neighbor_id, math.inf):
                dist[neighbor_id] = new_dist
                prev[neighbor_id] = (node_id, edge_id)
                heapq.heappush(heap, (new_dist, neighbor_id))

    if dist.get(end_node_id, math.inf) == math.inf:
        return None  # unreachable — caller should create a direct edge

    # Walk back from end to start, collecting the edge_ids used.
    path_edge_ids: list[str] = []
    current = end_node_id
    while current != start_node_id:
        previous_node, edge_id = prev[current]
        path_edge_ids.append(edge_id)
        current = previous_node
    path_edge_ids.reverse()
    print("found:",path_edge_ids)
    return path_edge_ids


def estimate_direct_length(harness: Harness, start_node_id: str, end_node_id: str,
                            fallback_mm: float = 50.0) -> float:
    """Straight-line distance between two nodes' positions, in the same
    units as Node.position (mm, by convention in this project). Falls
    back to a flat default if either node has no saved position."""
    start_node = harness.nodes.get(start_node_id)
    end_node = harness.nodes.get(end_node_id)
    if start_node and end_node and start_node.position is not None and end_node.position is not None:
        ax, ay = start_node.position[0], start_node.position[1]
        bx, by = end_node.position[0], end_node.position[1]
        return math.hypot(bx - ax, by - ay)
    return fallback_mm
