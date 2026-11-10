"""对大麦自绘数字做本地字形比较。卡片位置由控件树提供。"""

import json
from functools import lru_cache
from pathlib import Path

import numpy as np
from PIL import Image


def _shape(mask):
    height, width = mask.shape
    glyph = Image.fromarray(mask.astype(np.uint8) * 255).resize((16, 24), Image.Resampling.BILINEAR)
    return np.asarray(glyph) >= 128, width / height


@lru_cache(maxsize=1)
def _templates():
    masks, aspects, labels = [], [], []
    # 只保存 12 个数字和标点的归一化二值字形，包含普通和选中样式。
    bank = json.loads(Path(__file__).with_name("sku_glyphs.json").read_text())
    for char, variants in bank.items():
        for aspect, bits in variants:
            mask = np.unpackbits(np.frombuffer(bytes.fromhex(bits), dtype=np.uint8))
            masks.append(mask.reshape(24, 16).astype(bool))
            aspects.append(aspect)
            labels.append(char)
    return np.stack(masks), np.asarray(aspects), np.asarray(labels)


def orange(pixels):
    r, g, b = np.moveaxis(pixels[..., :3], -1, 0)
    return (r > 155) & (g < .72 * r) & (b < .55 * r)


def read_digits(pixels) -> str:
    """返回数字和时间分隔符；其他字形标记为问号。"""
    if not pixels.size:
        return ""
    mask = (np.max(pixels[..., :3], axis=-1) < 150) | orange(pixels)
    columns = np.flatnonzero(mask.any(axis=0))
    if not columns.size:
        return ""
    templates, aspects, labels = _templates()
    groups = np.split(columns, np.where(np.diff(columns) > 1)[0] + 1)
    output = []
    previous_end = None
    for group in groups:
        start, end = group[0], group[-1] + 1
        if previous_end is not None and start - previous_end > pixels.shape[0] * .17:
            output.append(" ")
        previous_end = end
        glyph = mask[:, start:end]
        rows = np.flatnonzero(glyph.any(axis=1))
        glyph = glyph[rows[0]:rows[-1] + 1]
        sample, aspect = _shape(glyph)
        overlap = np.count_nonzero(templates & sample, axis=(1, 2))
        union = np.count_nonzero(templates | sample, axis=(1, 2))
        scores = overlap / union - .24 * np.abs(np.log(aspects / aspect))
        best = int(np.argmax(scores))
        other = max(scores[labels != labels[best]], default=0)
        output.append(labels[best] if scores[best] >= .65 and scores[best] - other >= .04 else "?")
    return "".join(output)
