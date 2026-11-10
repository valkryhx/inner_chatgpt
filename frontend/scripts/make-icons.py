"""从品牌原图生成站点图标。

用法（在 frontend/ 下）：python scripts/make-icons.py

输入：src/assets/brand/hlauz-icon.png
输出：public/assets/{favicon.ico, favicon-32.png, favicon-192.png,
                    apple-touch-icon.png, logo.png}

原图四周有透明边和少量杂点，因此按 alpha 阈值重新取边界再居中补白，
否则图标在 16/32px 下会显得很小。
"""
from __future__ import annotations

import os

from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src", "assets", "brand", "hlauz-icon.png")
OUT = os.path.join(ROOT, "public", "assets")
ALPHA_THRESHOLD = 24
PAD_RATIO = 0.05


def main() -> None:
    os.makedirs(OUT, exist_ok=True)
    img = Image.open(SRC).convert("RGBA")
    alpha = img.split()[3].point(lambda a: 255 if a > ALPHA_THRESHOLD else 0)
    bbox = alpha.getbbox()
    if bbox is None:
        raise SystemExit("source image is fully transparent")
    crop = img.crop(bbox)
    side = max(crop.size)
    pad = int(side * PAD_RATIO)
    canvas = Image.new("RGBA", (side + pad * 2, side + pad * 2), (0, 0, 0, 0))
    canvas.paste(crop, ((canvas.width - crop.width) // 2, (canvas.height - crop.height) // 2))

    canvas.resize((32, 32), Image.LANCZOS).save(os.path.join(OUT, "favicon-32.png"))
    canvas.resize((192, 192), Image.LANCZOS).save(os.path.join(OUT, "favicon-192.png"))
    canvas.resize((180, 180), Image.LANCZOS).save(os.path.join(OUT, "apple-touch-icon.png"))
    canvas.resize((320, 320), Image.LANCZOS).save(os.path.join(OUT, "logo.png"))
    canvas.resize((256, 256), Image.LANCZOS).save(
        os.path.join(OUT, "favicon.ico"),
        sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
    )
    print(f"icons written to {OUT} (source bbox={bbox})")


if __name__ == "__main__":
    main()
