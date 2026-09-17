#!/usr/bin/env python3
"""Map between the source figures.json schema and CVAT shapes, in both directions.

Holds no transport: the caller passes plain dictionaries in and gets plain dictionaries
back, so the same label naming and keypoint order serve the import and the export.
"""

import json
import math
import sys
from pathlib import Path

# CSS colour names used by meta.json; an unlisted name is an error rather than a default.
CSS_COLORS = {
    "aqua": "#00ffff",
    "black": "#000000",
    "blue": "#0000ff",
    "brown": "#a52a2a",
    "cyan": "#00ffff",
    "fuchsia": "#ff00ff",
    "gold": "#ffd700",
    "gray": "#808080",
    "green": "#008000",
    "lime": "#00ff00",
    "magenta": "#ff00ff",
    "maroon": "#800000",
    "navy": "#000080",
    "olive": "#808000",
    "orange": "#ffa500",
    "pink": "#ffc0cb",
    "purple": "#800080",
    "red": "#ff0000",
    "silver": "#c0c0c0",
    "teal": "#008080",
    "violet": "#ee82ee",
    "white": "#ffffff",
    "yellow": "#ffff00",
}

SKELETON_SUFFIX = "_skeleton"
IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".bmp")

# An image-level tag carried by every task, so an annotator can mark a frame unusable. The
# export keeps such a frame and flips its "trash" flag rather than dropping the entry.
TRASH_LABEL = "trash"

# The skeleton svg lives in a 100x100 viewport; keep the nodes off the edges.
VIEWPORT_MIN = 10.0
VIEWPORT_MAX = 90.0


def color(name: str) -> str:
    if name.startswith("#"):
        return name
    if name.lower() not in CSS_COLORS:
        sys.exit(f"unknown colour name {name!r}; add it to CSS_COLORS in {Path(__file__).name}")
    return CSS_COLORS[name.lower()]


def viewport(value: float) -> float:
    """Map a unit keypoint coordinate onto the svg viewport."""
    return VIEWPORT_MIN + value * (VIEWPORT_MAX - VIEWPORT_MIN)


def build_svg(keypoint_info: dict, connections: list, node_ids: dict) -> str:
    if len(keypoint_info) == 1:
        positions = {name: (50.0, 50.0) for name in keypoint_info}
    else:
        positions = {
            name: (viewport(point["x"]), viewport(point["y"]))
            for name, point in keypoint_info.items()
        }

    elements = []
    for edge in connections:
        x1, y1 = positions[edge["from"]]
        x2, y2 = positions[edge["to"]]
        elements.append(
            f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{color(edge["color"])}" '
            f'data-type="edge" data-node-from="{node_ids[edge["from"]]}" stroke-width="0.5" '
            f'data-node-to="{node_ids[edge["to"]]}"></line>'
        )

    for name, point in keypoint_info.items():
        x, y = positions[name]
        node_id = node_ids[name]
        elements.append(
            f'<circle r="1.5" stroke="black" fill="{color(point["color"])}" cx="{x}" cy="{y}" '
            f'stroke-width="0.1" data-type="element node" data-element-id="{node_id}" '
            f'data-node-id="{node_id}" data-label-name="{name}"></circle>'
        )

    return "".join(elements)


def build_labels(meta: dict) -> list:
    labels = []
    for entry in meta["labels"]:
        if entry["type"] == "BBOX":
            labels.append(
                {
                    "name": entry["name"],
                    "type": "rectangle",
                    "color": color(entry["color"]),
                    "attributes": [],
                }
            )
        elif entry["type"] == "KGROUP":
            attributes = entry.get("attributes") or {}
            keypoint_info = attributes.get("keypoint_info") or {}
            if not keypoint_info:
                sys.exit(f"KGROUP label {entry['name']!r} has no keypoint_info")
            node_ids = {name: index for index, name in enumerate(keypoint_info, start=1)}
            labels.append(
                {
                    "name": entry["name"] + SKELETON_SUFFIX,
                    "type": "skeleton",
                    "color": color(entry["color"]),
                    "attributes": [],
                    "sublabels": [
                        {
                            "name": name,
                            "type": "points",
                            "color": color(point["color"]),
                            "attributes": [],
                        }
                        for name, point in keypoint_info.items()
                    ],
                    "svg": build_svg(
                        keypoint_info, attributes.get("keypoint_connections") or [], node_ids
                    ),
                }
            )
        else:
            sys.exit(f"unsupported label type {entry['type']!r} in meta.json")

    # meta.json describes the classes to draw; the trash tag is ours and belongs to every task.
    if not any(label["name"] == TRASH_LABEL for label in labels):
        labels.append(
            {
                "name": TRASH_LABEL,
                "type": "tag",
                "color": color("gray"),
                "attributes": [],
            }
        )
    return labels


def keypoint_names(meta: dict) -> dict:
    return {
        entry["name"]: list((entry.get("attributes") or {}).get("keypoint_info") or {})
        for entry in meta["labels"]
        if entry["type"] == "KGROUP"
    }


def build_annotations(figures: dict, meta: dict, image_names: list) -> tuple[dict, dict]:
    sublabels = keypoint_names(meta)
    counts = {"boxes": 0, "skeletons": 0, "incomplete": 0, "dropped": 0}
    annotations = {}

    for name in image_names:
        figure = figures[name]
        shapes = []

        # A detection-only task writes no "kgroups" key at all.
        for box in figure.get("bboxes", []):
            shapes.append(
                {
                    "type": "rectangle",
                    "label": box["label"],
                    "points": [box["x1"], box["y1"], box["x2"], box["y2"]],
                }
            )
            counts["boxes"] += 1

        for group in figure.get("kgroups", []):
            wanted = sublabels.get(group["label"])
            if wanted is None:
                sys.exit(f"{name}: kgroup label {group['label']!r} is not in meta.json")

            present = {point["label"]: (point["x"], point["y"]) for point in group["points"]}
            if not present:
                counts["dropped"] += 1
                continue
            if len(present) != len(wanted):
                counts["incomplete"] += 1

            # A skeleton needs an element per sublabel, so a missing keypoint is placed at the
            # centroid of the ones that are there and marked outside for the annotator to move.
            centroid = [
                sum(value[0] for value in present.values()) / len(present),
                sum(value[1] for value in present.values()) / len(present),
            ]
            shapes.append(
                {
                    "type": "skeleton",
                    "label": group["label"] + SKELETON_SUFFIX,
                    "elements": [
                        {
                            "label": sublabel,
                            "points": list(present.get(sublabel, centroid)),
                            "outside": sublabel not in present,
                        }
                        for sublabel in wanted
                    ],
                }
            )
            counts["skeletons"] += 1

        annotations[name] = {
            "width": figure["width"],
            "height": figure["height"],
            "shapes": shapes,
        }

    return annotations, counts


def rectangle_corners(points: list, rotation: float) -> list:
    x1, y1, x2, y2 = points
    if not rotation:
        return [x1, y1, x2, y2]

    # A rotated rectangle has no place in the figures.json schema; keep its outer box.
    angle = math.radians(rotation)
    center_x, center_y = (x1 + x2) / 2, (y1 + y2) / 2
    corners = [(x1, y1), (x2, y1), (x2, y2), (x1, y2)]
    rotated = [
        (
            center_x + (x - center_x) * math.cos(angle) - (y - center_y) * math.sin(angle),
            center_y + (x - center_x) * math.sin(angle) + (y - center_y) * math.cos(angle),
        )
        for x, y in corners
    ]
    xs = [point[0] for point in rotated]
    ys = [point[1] for point in rotated]
    return [min(xs), min(ys), max(xs), max(ys)]


def read_task(source: Path) -> tuple[list, dict, list, dict]:
    """Read one source task directory into CVAT labels, per-image shapes and image paths."""
    images_dir = source / "img"
    if not images_dir.is_dir():
        sys.exit(f"{images_dir} does not exist")

    meta = json.loads((source / "meta.json").read_text())
    figures = json.loads((source / "figures.json").read_text())

    images = sorted(path for path in images_dir.iterdir() if path.suffix.lower() in IMAGE_SUFFIXES)
    missing = [path.name for path in images if path.name not in figures]
    if missing:
        sys.exit(f"{len(missing)} images have no entry in figures.json, first is {missing[0]}")

    labels = build_labels(meta)
    annotations, counts = build_annotations(figures, meta, [path.name for path in images])
    return labels, annotations, images, counts


def figures_from_shapes(
    frames: dict,
    shapes: list,
    names: dict,
    sublabels: dict,
    counts,
    trash: set = frozenset(),
) -> dict:
    """Turn the shapes of one job into the figures.json entry of every frame it covers.

    A frame tagged as trash stays in the output with its shapes; only its flag is set, so the
    caller decides what to do with it.
    """
    figures = {
        frame["name"]: {
            "trash": number in trash,
            "bboxes": [],
            "kgroups": [],
            "height": frame["height"],
            "width": frame["width"],
        }
        for number, frame in frames.items()
    }

    for shape in shapes:
        frame = frames.get(shape.frame)
        if frame is None:
            counts["shapes outside the job frames"] += 1
            continue
        figure = figures[frame["name"]]

        if shape.type.value == "rectangle":
            x1, y1, x2, y2 = rectangle_corners(list(shape.points), shape.rotation or 0.0)
            figure["bboxes"].append(
                {
                    "x1": round(x1),
                    "y1": round(y1),
                    "x2": round(x2),
                    "y2": round(y2),
                    "label": names.get(shape.label_id, str(shape.label_id)),
                    "score": round(float(getattr(shape, "score", 1.0) or 1.0), 4),
                }
            )
            counts["bboxes"] += 1
        elif shape.type.value == "skeleton":
            label = names.get(shape.label_id, str(shape.label_id))
            points = []
            for element in shape.elements:
                if element.outside:
                    counts["keypoints left outside"] += 1
                    continue
                sublabel, order = sublabels.get(element.label_id, (str(element.label_id), 0))
                points.append(
                    (
                        order,
                        {
                            "x": round(element.points[0]),
                            "y": round(element.points[1]),
                            "label": sublabel,
                        },
                    )
                )
            # keep the keypoint order of the skeleton definition, as figures.json has it
            points = [point for _, point in sorted(points, key=lambda item: item[0])]
            figure["kgroups"].append(
                {
                    "points": points,
                    "label": label[: -len(SKELETON_SUFFIX)]
                    if label.endswith(SKELETON_SUFFIX)
                    else label,
                }
            )
            counts["kgroups"] += 1
        else:
            counts[f"{shape.type.value} shapes skipped"] += 1

    return figures
