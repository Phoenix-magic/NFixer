"""Рисует иконку NFixer: зелёный глаз смотрит ролик на квадратном экране. Нужен Pillow.

Запуск: python tools/make_icon.py  ->  nfixer.ico и nfixer.png рядом с программой.
"""
import math
import os

from PIL import Image, ImageDraw

S = 1024  # рисуем крупно, потом уменьшаем - так края получаются гладкими
GREEN = (118, 185, 0, 255)
DARK = (20, 23, 28, 255)
LIGHT = (242, 242, 242, 255)


def almond(cx, cy, w, h, n=60):
    """Контур глаза: две дуги окружностей, сходящиеся в уголках."""
    half = w / 2
    r = (half * half + (h / 2) ** 2) / h  # радиус дуги при заданных ширине и высоте
    a = math.asin(half / r)
    top = [(cx + r * math.sin(t), cy + (r - h / 2) - r * math.cos(t)) for t in (-a + 2 * a * i / n for i in range(n + 1))]
    bottom = [(cx + r * math.sin(t), cy - (r - h / 2) + r * math.cos(t)) for t in (a - 2 * a * i / n for i in range(n + 1))]
    return top + bottom


def draw():
    im = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    # экран с роликом
    d.rounded_rectangle((300, 50, 990, 740), radius=120, fill=DARK, outline=LIGHT, width=46)
    d.polygon([(560, 270), (560, 520), (790, 395)], fill=LIGHT)
    # глаз в левом нижнем углу, смотрит на экран
    cx, cy = 360, 740
    d.polygon(almond(cx, cy, 700, 430), fill=DARK)       # тёмная подложка отделяет глаз от экрана
    d.polygon(almond(cx, cy, 640, 370), fill=GREEN)
    d.polygon(almond(cx, cy, 500, 250), fill=LIGHT)
    ix, iy = cx + 50, cy - 18                            # радужка смещена к экрану - взгляд вверх-вправо
    d.ellipse((ix - 118, iy - 118, ix + 118, iy + 118), fill=GREEN)
    d.ellipse((ix - 58, iy - 58, ix + 58, iy + 58), fill=DARK)
    d.ellipse((ix + 14, iy - 62, ix + 58, iy - 18), fill=LIGHT)
    return im


def main():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    im = draw()
    im.resize((256, 256), Image.LANCZOS).save(os.path.join(root, "nfixer.png"))
    im.save(os.path.join(root, "nfixer.ico"), sizes=[(s, s) for s in (16, 24, 32, 48, 64, 128, 256)])
    print("ok")


if __name__ == "__main__":
    main()
