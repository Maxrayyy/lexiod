"""Conservative deskew of ruled forms; never infer field meaning from labels."""
from __future__ import annotations

import math
from PIL import Image


def table_skew(image: Image.Image) -> float:
    """Return a PIL correction angle only for a coherent, two-axis ruled grid.

    Work on a bounded thumbnail. Require multiple independent long rules and
    perpendicular column boundaries, so handwriting/text baselines alone do
    not rotate a page. Perspective with inconsistent rule angles is left alone.
    """
    import cv2
    import numpy as np

    sample = image.convert('L')
    sample.thumbnail((1600, 1600))
    w, h = sample.size
    if min(w, h) < 200:
        return 0.0
    edges = cv2.Canny(np.asarray(sample), 50, 150)
    lines = cv2.HoughLinesP(edges, 1, np.pi / 1800, threshold=80,
                            minLineLength=min(w, h) * .20, maxLineGap=12)
    if lines is None:
        return 0.0
    horizontal, vertical = [], []
    for x0, y0, x1, y1 in lines.reshape(-1, 4):
        dx, dy = float(x1-x0), float(y1-y0)
        angle = (math.degrees(math.atan2(dy, dx)) + 90) % 180 - 90
        length = math.hypot(dx, dy)
        if abs(angle) <= 20 and abs(dx) >= w * .30:
            # Intercept groups duplicate detections of the two edges of a rule.
            horizontal.append((angle, length, float(y0) - dy / dx * float(x0)))
        elif abs(angle) >= 70 and abs(dy) >= h * .20:
            vertical.append((angle - math.copysign(90, angle), float(x0) - dx / dy * float(y0)))
    if len(horizontal) < 4 or len(vertical) < 2:
        return 0.0
    ordered = sorted(horizontal)
    total = sum(line[1] for line in ordered)
    running = 0.0
    angle = 0.0
    for a, length, _ in ordered:
        running += length
        if running >= total / 2:
            angle = a
            break
    aligned = [line for line in horizontal if abs(line[0] - angle) <= .8]
    if sum(line[1] for line in aligned) < .85 * total:
        return 0.0

    def distinct(values, gap):
        groups = []
        for value in sorted(values):
            if not groups or value - groups[-1] > gap:
                groups.append(value)
        return groups

    rows = distinct([line[2] for line in aligned], max(8, h * .012))
    columns = distinct([x for a, x in vertical if abs(a-angle) < 2], max(8, w * .02))
    if len(rows) < 4 or rows[-1]-rows[0] < h * .20 or len(columns) < 2:
        return 0.0
    if columns[-1]-columns[0] < w * .25 or not .4 <= abs(angle) <= 20:
        return 0.0
    return round(angle, 4)


def deskew_table(image: Image.Image) -> tuple[Image.Image, float]:
    angle = table_skew(image)
    if not angle:
        return image, 0.0
    return image.rotate(angle, resample=Image.Resampling.BICUBIC,
                        expand=True, fillcolor='white'), angle
