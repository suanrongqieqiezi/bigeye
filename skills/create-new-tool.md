# create-new-tool
> 给自己创造新工具，扩展能力。

在 `tools/` 目录下创建 `.py` 文件：

```python
from tools.registry import register_tool

@register_tool(
    name="工具名",
    description="一句话描述这个工具做什么。",
    parameters={
        "type": "object",
        "properties": {
            "参数名": {
                "type": "string",
                "description": "参数说明"
            }
        },
        "required": ["参数名"]
    }
)
def 工具名(参数名: str):
    return "结果"
```

步骤：
1. `write_file("tools/新工具.py", 上面的模板代码)`
2. 在 `server.py` 的 import 区域添加 `import tools.新工具`
3. 重启服务器
