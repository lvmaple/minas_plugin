from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

size = 300
im = Image.new("RGBA", (size, size), (0, 0, 0, 0))
draw = ImageDraw.Draw(im)
draw.rounded_rectangle((12, 12, 288, 288), radius=68, fill="#4776E6")
try:
    font = ImageFont.truetype("C:/Windows/Fonts/arialbd.ttf", 196)
except OSError:
    font = ImageFont.load_default()
draw.text((150, 146), "M", font=font, anchor="mm", fill="white")
im.save(Path(__file__).resolve().parent.parent / "src/ui/icon.png")
