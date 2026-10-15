"""Arranging the map by hand: nothing overlaps, and no group frame covers other tables."""

from PySide6.QtCore import QPointF, QRectF

from etl_tool.ui.map import GAP_Y, NODE_WIDTH, Unit, frame_rect, free_spot, place, push_down, tidy


def box(point: QPointF, height: float) -> QRectF:
    return QRectF(point.x(), point.y(), NODE_WIDTH, height)


def test_push_down_moves_what_is_in_the_way_and_what_it_lands_on():
    area = QRectF(0, 0, 300, 200)
    rects = [
        QRectF(0, 150, 100, 100),  # in the area
        QRectF(0, 300, 100, 100),  # where the first one lands
        QRectF(500, 150, 100, 100),  # beside the area
        QRectF(0, 1000, 100, 50),  # far below
    ]
    assert push_down(area, rects, gap=20) == [70, 40, 0, 0]


def test_push_down_leaves_overlaps_elsewhere_alone():
    rects = [QRectF(1000, 0, 100, 100), QRectF(1050, 50, 100, 100)]
    assert push_down(QRectF(0, 0, 100, 100), rects) == [0, 0]


def test_tidy_keeps_frames_free_and_moves_groups_as_a_whole():
    heights = dict.fromkeys("abcde", 60.0)
    groups = {"a": "g1", "b": "g1", "c": "g2", "d": "g2", "e": ""}
    positions = {
        "a": QPointF(0, 0),
        "b": QPointF(0, 96),
        "c": QPointF(0, 180),  # g2's frame overlaps g1's
        "d": QPointF(400, 180),
        "e": QPointF(0, 120),  # on top of b, without a group
    }
    result = tidy(positions, heights, groups)
    assert result["a"] == positions["a"] and result["b"] == positions["b"]
    assert result["c"].y() - 180 == result["d"].y() - 180 > 0  # g2 moved in one piece
    for name in ("g1", "g2"):
        frame = frame_rect(box(result[u], 60) for u in groups if groups[u] == name)
        assert not any(frame.intersects(box(result[u], 60)) for u in groups if groups[u] != name)


def test_tidy_lets_moved_boxes_give_way():
    heights = {"a": 60.0, "b": 60.0}
    positions = {"a": QPointF(0, 0), "b": QPointF(0, 20)}
    assert tidy(positions, heights, {}, moved=["a"])["a"] == QPointF(0, 20 + 60 + GAP_Y)
    assert tidy(positions, heights, {})["b"] == QPointF(0, 60 + GAP_Y)


def test_a_table_without_a_place_goes_below_its_group():
    units = [
        Unit("a", 60, "g", (0, 0)),
        Unit("b", 60, "g", (0, 100)),
        Unit("x", 60, "", (0, 260)),  # right below the group
        Unit("new", 60, "g"),
    ]
    result = place(units, [])
    assert result["new"] == QPointF(0, 160 + GAP_Y)
    frame = frame_rect(box(result[u], 60) for u in ("a", "b", "new"))
    assert not frame.intersects(box(result["x"], 60))  # x made room
    assert result["a"] == QPointF(0, 0) and result["b"] == QPointF(0, 100)


def test_free_spot_avoids_boxes_and_the_frames_of_other_groups():
    positions, heights, groups = {"a": QPointF(0, 0)}, {"a": 100.0}, {"a": "g"}
    assert free_spot(QPointF(0, 50), 60, "", positions, heights, groups) == QPointF(0, 100 + 14 + GAP_Y)
    assert free_spot(QPointF(0, 150), 60, "g", positions, heights, groups) == QPointF(0, 150)  # inside its own group
    assert free_spot(QPointF(900, 0), 60, "", positions, heights, groups) == QPointF(900, 0)
