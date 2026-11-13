"""生成应用图标 icon.ico（纯标准库，不依赖 Pillow）"""
import struct
import zlib
from pathlib import Path

SIZE = 128
BG = (16, 20, 28, 255)          # 深色底
ACCENT = (37, 244, 238, 255)    # 青绿
ACCENT2 = (254, 44, 85, 255)    # 抖音红
RADIUS = 26


def in_round_rect(x, y, size, r):
    if r <= x < size - r and 0 <= y < size:
        return True
    if 0 <= x < size and r <= y < size - r:
        return True
    for cx, cy in ((r, r), (size - r - 1, r), (r, size - r - 1), (size - r - 1, size - r - 1)):
        if (x - cx) ** 2 + (y - cy) ** 2 <= r * r:
            return True
    return False


def build_pixels():
    rows = []
    for y in range(SIZE):
        row = bytearray()
        row.append(0)  # filter type 0
        for x in range(SIZE):
            if not in_round_rect(x, y, SIZE, RADIUS):
                row.extend((0, 0, 0, 0))
                continue
            color = BG
            # 向下箭头竖干
            if 56 <= x <= 72 and 30 <= y <= 62:
                color = ACCENT
            # 箭头三角
            elif 62 <= y <= 88:
                half = int((y - 62) * 0.85) + 6
                if abs(x - 64) <= half and 62 <= y <= 88:
                    color = ACCENT
            # 底部横线
            elif 96 <= y <= 104 and 36 <= x <= 92:
                color = ACCENT2
            row.extend(color)
        rows.append(bytes(row))
    return b"".join(rows)


def png_bytes(width, height, raw):
    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw, 9))
            + chunk(b"IEND", b""))


def main():
    png = png_bytes(SIZE, SIZE, build_pixels())
    header = struct.pack("<HHH", 0, 1, 1)
    entry = struct.pack("<BBBBHHII", SIZE, SIZE, 0, 0, 1, 32, len(png), 6 + 16)
    out = Path(__file__).resolve().with_name("icon.ico")
    out.write_bytes(header + entry + png)
    print("已生成:", out, out.stat().st_size, "字节")


if __name__ == "__main__":
    main()
