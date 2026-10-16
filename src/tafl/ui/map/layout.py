"""Arranging the boxes of the project map.

The layout works on *units* (a table, or a collapsed group drawn as one box) and *arcs* (arrows
between units). Columns run from left to right in data-flow order. The units of a group share a
block, and so do connected ungrouped units; blocks sit side by side where they don't share
columns, so a group's frame never covers other boxes. A map arranged by hand keeps its
positions; ``tidy`` keeps it free of overlaps.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass

from PySide6.QtCore import QPointF, QRectF

NODE_WIDTH = 270
HEADER = 34  # title bar of a box
ROW = 21  # one row in a box
PADDING = 6
GAP_X = 110
GAP_Y = 36
GROUP_PAD = 14  # space between a group's frame and its boxes
GROUP_TITLE = 26  # height of a group's title bar
GROUP_GAP = 34  # space between two groups


@dataclass(frozen=True)
class Unit:
    """Something drawn as one box: a table, or a collapsed group."""

    id: str
    height: float
    group: str = ""  # the frame it is drawn in ("" = none)
    position: tuple[float, float] | None = None  # placed by hand


@dataclass(frozen=True)
class Arc:
    """An arrow between two units; the offsets are the heights where it leaves / arrives."""

    source: str
    target: str
    source_offset: float = HEADER / 2
    target_offset: float = HEADER / 2


def auto_layout(units: list[Unit], arcs: list[Arc]) -> dict[str, QPointF]:
    """Positions for all units (see the module docstring)."""
    arcs = [a for a in arcs if a.source != a.target]
    heights = {u.id: u.height for u in units}
    level = _levels(units, arcs)
    # The order without groups: inside each block it keeps arrows that leave the block from crossing.
    ideal = _layout_block([u.id for u in units], arcs, level, heights)
    positions: dict[str, QPointF] = {}
    placed: list[tuple[float, float, float]] = []  # left, right and bottom (incl. gap) of each block
    for grouped, members in _blocks(units, arcs, level):
        inside = [a for a in arcs if a.source in members and a.target in members]
        block = _layout_block(members, inside, level, heights, {u: ideal[u].y() for u in members})
        pad, pad_top = (GROUP_PAD, GROUP_PAD + GROUP_TITLE) if grouped else (0.0, 0.0)
        left = min(p.x() for p in block.values()) - pad - 20
        right = max(p.x() for p in block.values()) + NODE_WIDTH + pad + 20
        # Below the blocks that share columns with this one; level with its inputs if possible.
        floor = max((bottom for start, end, bottom in placed if start < right and left < end), default=0.0)
        wanted = [
            positions[a.source].y() + a.source_offset - a.target_offset - block[a.target].y() - pad_top
            for a in arcs
            if a.target in block and a.source in positions
        ]
        top = max(floor, _average(wanted) or 0.0)
        for unit_id, point in block.items():
            positions[unit_id] = QPointF(point.x(), point.y() + top + pad_top)
        bottom = max(positions[u].y() + heights[u] for u in members) + pad
        placed.append((left, right, bottom + (GROUP_GAP if grouped else GAP_Y)))
    return positions


def place(units: list[Unit], arcs: list[Arc]) -> dict[str, QPointF]:
    """Positions placed by hand where there are some. A unit without one goes below the other
    tables of its group, or to a free spot near where the automatic layout would put it; then
    overlaps are cleared up (see ``tidy``)."""
    auto = auto_layout(units, arcs)
    if all(u.position is None for u in units):
        return auto
    heights = {u.id: u.height for u in units}
    groups = {u.id: u.group for u in units}
    result = {u.id: QPointF(*u.position) for u in units if u.position is not None}
    new = sorted((u.id for u in units if u.position is None), key=lambda u: (auto[u].x(), auto[u].y()))
    for unit_id in new:
        group = groups[unit_id]
        mates = [m for m in result if group and groups[m] == group]
        if mates:
            x = min(result[m].x() for m in mates)
            result[unit_id] = QPointF(x, max(result[m].y() + heights[m] for m in mates) + GAP_Y)
        else:
            result[unit_id] = free_spot(auto[unit_id], heights[unit_id], group, result, heights, groups)
    return tidy(result, heights, groups, moved=new)


def tidy(
    positions: dict[str, QPointF],
    heights: dict[str, float],
    groups: dict[str, str],
    moved: Iterable[str] = (),
) -> dict[str, QPointF]:
    """Positions without overlaps, for a map arranged by hand:

    * boxes of the same group (or both without one) don't overlap: the ones in ``moved`` give
      way, otherwise the lower one;
    * no group's frame covers a box that isn't in the group: what is in the way moves down
      (the boxes of a group together, so its frame stays in one piece), and what that lands
      on moves further down.
    """
    result = {u: QPointF(p) for u, p in positions.items()}
    boxes = {u: _box(p, heights[u]) for u, p in result.items()}
    moved = set(moved)
    done: list[str] = []
    for unit_id in sorted(result, key=lambda u: (u in moved, result[u].y(), result[u].x())):
        group = groups.get(unit_id, "")
        for _ in range(len(done) + 1):
            box = boxes[unit_id]
            hit = next((boxes[d] for d in done if groups.get(d, "") == group and _near(boxes[d], box)), None)
            if hit is None:
                break
            result[unit_id] = QPointF(box.x(), hit.bottom() + GAP_Y)
            boxes[unit_id] = _box(result[unit_id], heights[unit_id])
        done.append(unit_id)

    names = {g for u in result if (g := groups.get(u, ""))}
    for _ in range(len(names) + 1):
        changed = False
        for name in sorted(names, key=lambda g: min(result[u].y() for u in result if groups.get(u, "") == g)):
            area = frame_rect(_box(result[u], heights[u]) for u in result if groups.get(u, "") == name)
            others = {u: p for u, p in result.items() if groups.get(u, "") != name}
            moves = make_space(area, others, heights, groups)
            result.update(moves)
            changed |= bool(moves)
        if not changed:
            break
    return result


def make_space(
    area: QRectF, positions: dict[str, QPointF], heights: dict[str, float], groups: dict[str, str]
) -> dict[str, QPointF]:
    """New positions for the units that are in the way of ``area``: they move down, the units of
    a group together; what they land on moves further down. Only moved units are returned."""
    blocks: dict[str, list[str]] = {}
    for unit_id in positions:
        group = groups.get(unit_id, "")
        blocks.setdefault(f"g:{group}" if group else f"u:{unit_id}", []).append(unit_id)
    keys = list(blocks)
    rects = []
    for key in keys:
        members = [_box(positions[u], heights[u]) for u in blocks[key]]
        rects.append(frame_rect(members) if key.startswith("g:") else members[0])
    result: dict[str, QPointF] = {}
    for key, dy in zip(keys, push_down(area, rects), strict=True):
        if dy:
            for unit_id in blocks[key]:
                result[unit_id] = positions[unit_id] + QPointF(0, dy)
    return result


def push_down(area: QRectF, rects: list[QRectF], gap: float = GROUP_GAP, margin: float = 10) -> list[float]:
    """How far each rectangle moves down so that none is in ``area``: what is in it goes below
    it, what that lands on goes further down, and so on. Rectangles that overlapped before and
    don't move are left as they are."""
    moves = [0.0] * len(rects)
    if not any(_near(area, r, margin) for r in rects):
        return moves
    done: list[int] = []
    for i in sorted(range(len(rects)), key=lambda k: (rects[k].top(), rects[k].left())):
        rect = QRectF(rects[i])
        for _ in range(len(done) + 2):
            obstacles = [area, *(rects[j].translated(0, moves[j]) for j in done if moves[i] or moves[j])]
            hit = next((o for o in obstacles if _near(o, rect, margin)), None)
            if hit is None:
                break
            rect.moveTop(hit.bottom() + gap)
            moves[i] = rect.top() - rects[i].top()
        done.append(i)
    return moves


def free_spot(
    start: QPointF,
    height: float,
    group: str,
    positions: dict[str, QPointF],
    heights: dict[str, float],
    groups: dict[str, str],
) -> QPointF:
    """``start``, or the first place below it where a new box of ``group`` touches no other box
    and no frame of another group (a group's first box keeps room for its frame)."""
    obstacles: list[QRectF] = []
    frames: dict[str, list[QRectF]] = {}
    for unit_id, point in positions.items():
        box = _box(point, heights[unit_id])
        other = groups.get(unit_id, "")
        if other and other != group:
            frames.setdefault(other, []).append(box)
        else:
            obstacles.append(box)
    obstacles += [frame_rect(boxes) for boxes in frames.values()]
    first = bool(group) and not any(groups.get(u, "") == group for u in positions)
    point = QPointF(start)
    for _ in range(len(obstacles) + 1):
        box = _box(point, height)
        rect = frame_rect([box]) if first else box
        hit = next((o for o in obstacles if _near(o, rect)), None)
        if hit is None:
            break
        point.setY(point.y() + hit.bottom() + GAP_Y - rect.top())
    return point


def frame_rect(boxes: Iterable[QRectF]) -> QRectF:
    """The frame of a group around its boxes."""
    bounds = QRectF()
    for box in boxes:
        bounds = bounds.united(box)
    return bounds.adjusted(-GROUP_PAD, -GROUP_PAD - GROUP_TITLE, GROUP_PAD, GROUP_PAD)


def _box(point: QPointF, height: float) -> QRectF:
    return QRectF(point.x(), point.y(), NODE_WIDTH, height)


def _near(a: QRectF, b: QRectF, margin: float = 10) -> bool:
    return a.adjusted(-margin, -margin, margin, margin).intersects(b)


def _levels(units: list[Unit], arcs: list[Arc]) -> dict[str, int]:
    """Column of each unit: one more than the highest column of the units it uses."""
    sources: dict[str, list[str]] = {u.id: [] for u in units}
    for arc in arcs:
        if arc.source in sources and arc.target in sources:
            sources[arc.target].append(arc.source)
    level: dict[str, int] = {}

    def visit(unit_id: str, visiting: set[str]) -> int:
        if unit_id in level:
            return level[unit_id]
        if unit_id in visiting:  # circular reference: stop here
            return 0
        visiting.add(unit_id)
        value = 1 + max((visit(s, visiting) for s in sources[unit_id]), default=-1)
        visiting.discard(unit_id)
        level[unit_id] = value
        return value

    for unit in units:
        visit(unit.id, set())
    return level


def _blocks(units: list[Unit], arcs: list[Arc], level: dict[str, int]) -> list[tuple[bool, list[str]]]:
    """One block per group and per set of connected ungrouped units, leftmost first (so a
    block's inputs are usually placed before it)."""
    group_of = {u.id: u.group for u in units}
    parent = {u.id: u.id for u in units if not u.group}

    def root(unit_id: str) -> str:
        while parent[unit_id] != unit_id:
            parent[unit_id] = parent[parent[unit_id]]
            unit_id = parent[unit_id]
        return unit_id

    for arc in arcs:
        if arc.source in parent and arc.target in parent:
            parent[root(arc.source)] = root(arc.target)
    blocks: dict[str, list[str]] = {}
    for unit in units:
        key = f"g:{unit.group}" if unit.group else f"u:{root(unit.id)}"
        blocks.setdefault(key, []).append(unit.id)
    order = sorted(blocks.values(), key=lambda members: min(level[u] for u in members))
    return [(bool(group_of[members[0]]), members) for members in order]


def _layout_block(
    members: list[str],
    arcs: list[Arc],
    level: dict[str, int],
    heights: dict[str, float],
    initial: dict[str, float] | None = None,
) -> dict[str, QPointF]:
    """Columns of one block; ``initial`` gives the starting order within each column."""
    start = initial or {}
    incoming: dict[str, list[Arc]] = {u: [] for u in members}
    outgoing: dict[str, list[Arc]] = {u: [] for u in members}
    for arc in arcs:
        if arc.target in incoming and arc.source in outgoing:
            incoming[arc.target].append(arc)
            outgoing[arc.source].append(arc)
    columns: dict[int, list[str]] = {}
    for unit_id in members:
        columns.setdefault(level[unit_id], []).append(unit_id)
    positions: dict[str, QPointF] = {}

    def near_sources(unit_id: str) -> float | None:  # arriving rows level with the source ports
        return _average(
            [positions[a.source].y() + a.source_offset - a.target_offset for a in incoming[unit_id] if a.source in positions]
        )

    def near_targets(unit_id: str) -> float | None:  # ports level with the rows they feed
        return _average(
            [positions[a.target].y() + a.target_offset - a.source_offset for a in outgoing[unit_id] if a.target in positions]
        )

    def stack(number: int, wanted: Callable[[str], float | None]) -> None:
        column = columns[number]
        targets = {u: wanted(u) for u in column}
        column.sort(
            key=lambda u: targets[u] if targets[u] is not None else positions[u].y() if u in positions else start.get(u, 0.0)
        )
        y = 0.0
        for unit_id in column:
            if targets[unit_id] is not None:
                y = max(y, targets[unit_id])
            positions[unit_id] = QPointF(number * (NODE_WIDTH + GAP_X), y)
            y += heights[unit_id] + GAP_Y

    order = sorted(columns)
    for number in order:
        stack(number, lambda u: None)  # the starting order
    for _ in range(2):
        for number in order[1:]:
            stack(number, near_sources)
        for number in reversed(order[:-1]):
            stack(number, near_targets)
    for number in order[1:]:
        stack(number, near_sources)
    return positions


def _average(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None
