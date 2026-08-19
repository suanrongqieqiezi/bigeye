#!/usr/bin/env python3
"""AI image generation tool — wraps image_gen.py for function calling."""
import os
import sys
import threading
from .registry import register_tool

_ws = threading.local()


def set_workspace(path):
    """Set workspace for current thread. Generated images save here."""
    _ws.path = path


@register_tool(
    name="generate_image",
    description="根据文字描述生成图片，返回图片文件名。支持各种主题风格。",
    parameters={
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": "图片描述，例如：'星空下的山脉'、'未来城市'"
            },
            "width": {
                "type": "integer",
                "description": "图片宽度 (默认1024)",
                "default": 1024
            },
            "height": {
                "type": "integer",
                "description": "图片高度 (默认768)",
                "default": 768
            }
        },
        "required": ["prompt"]
    }
)
def generate_image(prompt: str, width: int = 1024, height: int = 768):
    """Generate an abstract image from text prompt using procedural art."""
    try:
        sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
        from image_gen import generate
        output_dir = getattr(_ws, 'path', None)
        result = generate(prompt, width, height, output_dir=output_dir)
        return {"url": result, "prompt": prompt}
    except Exception as e:
        return {"error": f"图片生成失败: {e}"}
