"""生成应用图标 icon.ico（纯标准库，不依赖 Pillow）

2026-11-14 起图标改用定稿的「归藏」字标。像素由
docs/brand/make_icon_from_cutout.py 渲染成：
  app/icon_frames/<边长>.png    给人看、给预览用
  app/icon_frames/<边长>.bgra   裸像素（BGRA，行序自上而下），本脚本用它拼 ICO
本脚本只负责把 .bgra 打包成 Windows ICO。

为什么不用 PNG 压缩帧：ICO 允许用 PNG 当帧，但部分系统路径（GDI+ 的 ToBitmap、
部分老的 shell 提取器）解码 PNG 帧会得到错色或直接失败；DIB 帧兼容性最好。
实测 128/256 的 PNG 帧在 System.Drawing 下会抛「请求的范围扩展超过了数组的结尾」。

换了新设计要重出图标时：
  1. 改 docs/brand/refs/ 里的源图；
  2. 跑 docs/brand/.venv/Scripts/python.exe docs/brand/make_icon_from_cutout.py
     （会重写 app/icon_frames/* 与 app/assets/logo-master.png）；
  3. 再跑本脚本，得到新 icon.ico。

旧的「深底 + 青绿箭头」设计保留在 git 历史里，不再使用。
"""
import struct
import sys
from pathlib import Path

APP = Path(__file__).resolve().parent
FRAMES = APP / "icon_frames"
ICON = APP / "icon.ico"
SIZES = (16, 20, 24, 32, 40, 48, 64, 96, 128, 256)


def read_bgra(size):
    p = FRAMES / f"{size}.bgra"
    if not p.is_file():
        raise SystemExit(f"缺少图标帧: {p}（先跑 docs/brand/make_icon_from_cutout.py）")
    data = p.read_bytes()
    if len(data) != size * size * 4:
        raise SystemExit(f"帧尺寸不对: {p} 期望 {size * size * 4} 实得 {len(data)}")
    return data


def dib(size, bgra):
    """ICO 用的 DIB：BITMAPINFOHEADER（高度写两倍）+ XOR 位图 + 全零 AND 掩码。"""
    hdr = struct.pack("<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0,
                      size * size * 4, 0, 0, 0, 0)
    rows = []
    for y in range(size - 1, -1, -1):           # DIB 是自下而上
        rows.append(bgra[y * size * 4:(y + 1) * size * 4])
    xor = b"".join(rows)
    mask_row = ((size + 31) // 32) * 4          # 1bpp，每行按 4 字节对齐
    and_mask = b"\x00" * (mask_row * size)      # 全零 = 全部不透明，透明度交给 alpha
    return hdr + xor + and_mask


def build():
    frames = [(s, dib(s, read_bgra(s))) for s in SIZES]
    header = struct.pack("<HHH", 0, 1, len(frames))
    offset = 6 + 16 * len(frames)
    entries, blobs = [], []
    for size, payload in frames:
        dim = 0 if size >= 256 else size
        entries.append(struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32,
                                   len(payload), offset))
        blobs.append(payload)
        offset += len(payload)
    return header + b"".join(entries) + b"".join(blobs), len(frames)


def main():
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else ICON
    data, n = build()
    out.write_bytes(data)
    print("已生成:", out, len(data), "字节,", n, "档")


if __name__ == "__main__":
    main()
