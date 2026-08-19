"""
文件晶体三问压缩提示词（CMN P1）

包含：
- FILE_CRYSTAL_PROMPT: 单切片三问压缩
- FILE_PYRAMID_PROMPT: 多晶体上层摘要（P2 用，先放这里）
"""

FILE_CRYSTAL_PROMPT = """你正在为 AI 建立文件晶体记忆。对以下文件切片做三问压缩。

【文件来源】{source_path}
【切片标签】{slice_label}
【切片内容】
{slice_content}

请严格按格式输出，答不出就继续提炼直到答出：
<<<结论>>>
这个切片的核心结论是什么？（一句话，不超过50字）

<<<为什么>>>
为什么是这个结论？关键因果/证据/逻辑链（不超过150字）

<<<下一步>>>
基于这个结论，下一步该做什么？（不超过80字）

<<<关键实体>>>
逗号分隔的实体列表
"""

FILE_PYRAMID_PROMPT = """你正在为 AI 建立文件晶体的上层摘要。以下是 N 个下层晶体的浓缩。

【文件来源】{source_path}
【层级】Layer {layer} → Layer {next_layer}
【下层晶体们】
{crystals_json}

请输出一个更浓缩的上层晶体，保留主干因果，允许丢细节：
<<<结论>>>
这群晶体的共同结论是什么？（不超过80字）

<<<为什么>>>
关键因果链（不超过200字）

<<<下一步>>>
若想了解细节，应下钻到哪一层？（指明下层晶体 id）

<<<覆盖范围>>>
这个摘要覆盖了哪些下层晶体（id 列表，逗号分隔）
"""


def format_crystal_prompt(source_path: str, slice_label: str, slice_content: str) -> str:
    """格式化单切片三问压缩提示词。"""
    return FILE_CRYSTAL_PROMPT.format(
        source_path=source_path,
        slice_label=slice_label or "<无标签>",
        slice_content=slice_content,
    )


def format_pyramid_prompt(source_path: str, layer: int, crystals_json: str) -> str:
    """格式化金字塔上层摘要提示词（P2 用）。"""
    return FILE_PYRAMID_PROMPT.format(
        source_path=source_path,
        layer=layer,
        next_layer=layer + 1,
        crystals_json=crystals_json,
    )
