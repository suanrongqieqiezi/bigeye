"""
大眼画图器 — 用 Pillow 从文字生成抽象艺术作品。
等 API key 配好了可以升级成真 AI 绘图。
"""

import io, os, json, random, math, colorsys
from datetime import datetime
from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImagePath

OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "public", "generated")
os.makedirs(OUTPUT_DIR, exist_ok=True)

# ── 颜色主题映射 ─────────────────────────────────────
THEMES = {
    "赛博": [(0,255,255), (255,0,128), (128,0,255), (0,200,200)],
    "cyber": [(0,255,255), (255,0,128), (128,0,255), (0,200,200)],
    "霓虹": [(255,0,128), (0,255,200), (200,0,255), (255,200,0)],
    "neon": [(255,0,128), (0,255,200), (200,0,255), (255,200,0)],
    "日出": [(255,200,50), (255,120,50), (255,50,50), (200,50,150)],
    "sunset": [(255,200,50), (255,120,50), (255,50,50), (200,50,150)],
    "森林": [(34,139,34), (0,100,0), (85,107,47), (154,205,50)],
    "forest": [(34,139,34), (0,100,0), (85,107,47), (154,205,50)],
    "海洋": [(0,119,190), (0,180,216), (144,224,239), (202,240,248)],
    "ocean": [(0,119,190), (0,180,216), (144,224,239), (202,240,248)],
    "火焰": [(255,69,0), (255,140,0), (255,215,0), (255,0,0)],
    "fire": [(255,69,0), (255,140,0), (255,215,0), (255,0,0)],
    "梦幻": [(200,150,255), (255,150,200), (150,200,255), (255,200,150)],
    "dream": [(200,150,255), (255,150,200), (150,200,255), (255,200,150)],
    "极简": [(200,200,200), (100,100,100), (50,50,50), (255,255,255)],
    "minimal": [(200,200,200), (100,100,100), (50,50,50), (255,255,255)],
    "星空": [(5,5,40), (20,20,80), (100,50,150), (200,100,200)],
    "starry": [(5,5,40), (20,20,80), (100,50,150), (200,100,200)],
    "金属": [(169,169,169), (192,192,192), (105,105,105), (218,165,32)],
    "metal": [(169,169,169), (192,192,192), (105,105,105), (218,165,32)],
}

DEFAULT_THEME = [(30,30,80), (80,30,120), (200,80,50), (255,200,50)]


def _match_theme(prompt: str) -> list:
    prompt_lower = prompt.lower()
    for keyword, palette in THEMES.items():
        if keyword in prompt_lower:
            return palette
    return DEFAULT_THEME


def generate(prompt: str, width: int = 1024, height: int = 768, output_dir: str = None) -> str:
    """Generate an image from text prompt, return URL path.
    When output_dir is given, save there instead of OUTPUT_DIR.
    """
    palette = _match_theme(prompt)
    img = Image.new("RGB", (width, height), palette[0])
    draw = ImageDraw.Draw(img)

    rnd = random.Random(hash(prompt) + os.getpid())

    # ── 渐变背景 ───────────────────────────────────
    for y in range(height):
        ratio = y / height
        c = tuple(int(a + (b - a) * ratio) for a, b in zip(palette[0], palette[1]))
        draw.line([(0, y), (width, y)], fill=c)

    # ── 几何元素 ────────────────────────────────────
    num_elements = 5 + rnd.randint(0, 8)
    for _ in range(num_elements):
        x = rnd.randint(0, width)
        y = rnd.randint(0, height)
        size = rnd.randint(20, width // 3)
        color = palette[rnd.randint(0, len(palette) - 1)]
        alpha = rnd.randint(30, 120)
        # Adjust for translucent effect
        color = tuple(min(c + 40, 255) for c in color)
        shape_type = rnd.choice(["ellipse", "rect", "circle", "poly"])

        if shape_type in ("ellipse", "circle"):
            bbox = [x - size // 2, y - size // 2, x + size // 2, y + size // 2]
            overlay = Image.new("RGBA", (width, height), (0, 0, 0, 0))
            overlay_draw = ImageDraw.Draw(overlay)
            if shape_type == "circle":
                overlay_draw.ellipse(bbox, fill=color + (alpha,))
            else:
                overlay_draw.ellipse(bbox, fill=color + (int(alpha * 0.6),))
            img = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")
            draw = ImageDraw.Draw(img)
        elif shape_type == "rect":
            overlay = Image.new("RGBA", (width, height), (0, 0, 0, 0))
            overlay_draw = ImageDraw.Draw(overlay)
            overlay_draw.rectangle(
                [x, y, x + size, y + int(size * 0.6)],
                fill=color + (int(alpha * 0.5),),
            )
            img = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")
            draw = ImageDraw.Draw(img)
        elif shape_type == "poly":
            points = []
            cx, cy = x, y
            radius = size // 2
            sides = rnd.randint(3, 8)
            for i in range(sides):
                angle = 2 * math.pi * i / sides - math.pi / 2
                px = cx + radius * math.cos(angle)
                py = cy + radius * math.sin(angle)
                points.append((px, py))
            overlay = Image.new("RGBA", (width, height), (0, 0, 0, 0))
            overlay_draw = ImageDraw.Draw(overlay)
            overlay_draw.polygon(points, fill=color + (alpha,))
            img = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")
            draw = ImageDraw.Draw(img)

    # ── 粒子/星星 ──────────────────────────────────
    is_starry = "星空" in prompt or "starry" in prompt or "星" in prompt
    num_particles = 200 if is_starry else rnd.randint(30, 80)
    for _ in range(num_particles):
        px = rnd.randint(0, width - 1)
        py = rnd.randint(0, height - 1)
        ps = rnd.randint(1, 3)
        brightness = rnd.randint(150, 255)
        draw.ellipse(
            [px, py, px + ps, py + ps],
            fill=(brightness, brightness, brightness),
        )

    # ── 光晕特效 ─────────────────────────────────────
    glow_x = rnd.randint(width // 4, 3 * width // 4)
    glow_y = rnd.randint(height // 4, 3 * height // 4)
    glow_color = palette[rnd.randint(0, len(palette) - 1)]
    for r in range(120, 0, -4):
        alpha = int(20 * (1 - r / 120))
        overlay = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        overlay_draw = ImageDraw.Draw(overlay)
        overlay_draw.ellipse(
            [glow_x - r, glow_y - r, glow_x + r, glow_y + r],
            fill=glow_color + (alpha,),
        )
        img = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")
        draw = ImageDraw.Draw(img)

    # ── 文字（把 prompt 作为标题画上去） ───────────────
    try:
        font = ImageFont.truetype("C:/Windows/Fonts/msyh.ttf", 32)
        font_small = ImageFont.truetype("C:/Windows/Fonts/msyh.ttf", 16)
    except:
        font = ImageFont.load_default()
        font_small = font

    # 底部水印
    draw.text(
        (20, height - 40),
        f"大眼 · {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        fill=(255, 255, 255, 100),
        font=font_small,
    )

    # 标题（取prompt前20字）
    title = prompt.strip()[:30]
    draw.text(
        (20, 20),
        title,
        fill=(255, 255, 255, 180),
        font=font,
    )

    # ── 输出 ───────────────────────────────────────
    filename = f"gen_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{rnd.randint(1000,9999)}.png"
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)
        filepath = os.path.join(output_dir, filename)
        img.save(filepath, "PNG")
        return filename  # just the filename; caller (rpc_server) constructs URL
    filepath = os.path.join(OUTPUT_DIR, filename)
    img.save(filepath, "PNG")
    return f"/generated/{filename}"


def generate_from_json(data: dict) -> dict:
    """API handler wrapper — takes request data, returns response."""
    try:
        prompt = data.get("prompt", "").strip()
        if not prompt:
            return {"error": "prompt is required"}
        width = data.get("width", 1024)
        height = data.get("height", 768)
        output_dir = data.get("output_dir") or None
        url = generate(prompt, width, height, output_dir)
        return {"url": url, "prompt": prompt}
    except Exception as e:
        return {"error": str(e)}
