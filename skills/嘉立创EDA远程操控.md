---
name: 嘉立创EDA远程操控
description: 通过 easyeda-api-skill 的 WebSocket Bridge 让大眼远程操控嘉立创EDA专业版，实现AI自动画原理图/PCB。已验证全链路可用。
triggers: ["要AI自动操控嘉立创EDA画图"]
---

# 嘉立创EDA远程操控

## 链路架构

```
大眼(bash+curl) → HTTP POST /execute → bridge-server.mjs (Node, 端口49620)
  → WebSocket → EDA专业版 Run API Gateway 扩展 → 执行 JS → 返回结果
```

## 已就绪组件

| 组件 | 位置/状态 |
|---|---|
| skill 源码 | `E:\B\easyeda-api-skill`（依赖已 npm install） |
| bridge 启动脚本 | `E:\B\easyeda-api-skill\start_bridge.bat` |
| bridge 运行端口 | 49620（health 返回 edaConnected:true） |
| EDA 扩展 | Run API Gateway v1.0.5，用户已在扩展管理器安装并启用"允许外部交互" |
| EDA 安装路径 | `C:\Program Files (x86)\lceda-pro` |

## 调用方式（关键！）

### 格式
```bash
curl -s -X POST http://127.0.0.1:49620/execute \
  -H "Content-Type: application/json" \
  -d '{"code": "return await eda.dmt_Project.getCurrentProjectInfo();"}'
```

### 三个铁律（踩坑总结）
1. **字段名是 `code`**，不是 script
2. **类名小写**：`eda.dmt_Project` 不是 `DMT_Project`，`eda.pcb_Primitive` 等
3. **必须 `return await` 前缀**，否则返回 null

### Windows cmd 引号坑
cmd 里 curl 的 JSON 引号会转义失败，**用 Python 发请求**（写文件执行，别用 python -c 多行）：
```python
import json, urllib.request
req = urllib.request.Request(
    "http://127.0.0.1:49620/execute",
    data=json.dumps({"code": "return await eda.dmt_Project.getCurrentProjectInfo();"}).encode(),
    headers={"Content-Type": "application/json"}, method="POST")
print(urllib.request.urlopen(req, timeout=15).read().decode())
```

## 常用 API 类（前缀 eda.，全部小写）

- `eda.dmt_Project` — 工程管理：getCurrentProjectInfo / createProject / getAllProjectsUuid / openProject
- `eda.dmt_Board` — PCB 管理：getAllBoardsInfo / createBoard / getCurrentPcbInfo
- `eda.dmt_Schematic` — 原理图管理：copySchematic / copySchematicPage
- `eda.dmt_EditorControl` — 文档激活：activateDocument(tabId)
- `eda.sch_Primitive` — 原理图图元（放器件/连线）
- `eda.pcb_Primitive` — PCB 图元（布局布线）

完整 API 参考：`E:\B\easyeda-api-skill\references\_quick-reference.md`

## 验证过的实测（2026-08-27）

`return await eda.dmt_Project.getCurrentProjectInfo()` 返回了用户当前打开的工程 BTcam3：
- Project uuid、teamUuid
- Board: 主板_lite
- Schematic: Schematic1_1（A4，V1.0）

## 故障排查

- health 返回 edaConnected:false → EDA 没连：让用户重启 EDA / 确认扩展在运行中且开了"允许外部交互"
- 500 + "xxx is not defined" → 类名不对，查 _quick-reference.md 确认小写类名
- 500 + "Cannot read properties of undefined" → eda.xxx 属性不存在，同样查文档
- result 为 null → 忘了 `return await` 前缀
- 400 + Missing "code" field → 字段名用错了

## 能力边界

原理图自动绘制成熟（搜器件/放置/连线/出BOM）；PCB 布局布线偏辅助，需人工把关。适合流程：需求→AI方案→AI画原理图→人工审查→出网表。

## PCB 布局布线（实测通过 2026-08-27）

### 层 ID（EPCB_LayerId）
- `1` = TOP（顶层铜），`2` = BOTTOM（底层铜），`11` = BOARD_OUTLINE（板框）

### 走线（实测通过）
```javascript
// 画线/走线：net=网络名(空字符串=无网络)，layer=层ID，坐标单位 mil，width=线宽
await eda.pcb_PrimitiveLine.create('GND', 1, x1, y1, x2, y2, 12);
```

### 器件移动（实测通过）
```javascript
const ids = await eda.pcb_PrimitiveComponent.getAllPrimitiveId();
const c = await eda.pcb_PrimitiveComponent.get(id);
await eda.pcb_PrimitiveComponent.modify(id, {x, y, rotation});  // 中心坐标
```

### 引脚坐标查询
```javascript
const pins = await eda.pcb_PrimitiveComponent.getAllPinsByPrimitiveId(id);
const net = p.getState_Net();  // 注意：此版本 getState_Net() 拿不到网络名，网络归属要从 pcb_export_full.json（解析源文本）获取
p.getState_X(); p.getState_Y(); p.getState_PadNumber();
```

### 已验证的坑
- 走线图元在源文本里 type 是 `LINE`（不是 TRACK），带 `netName` 字段
- 铺铜 `pcb_PrimitivePoured.create()` 是空实现，不可用
- 自动布局/布线 API（importAutoRouteJsonFile 等）v1.0.5 未暴露，手动布局+手动走线
- 读网络归属最可靠方式：拉全量源文本解析 PAD_NET 行（compId+padNum→net）
- getState_Net() 返回的网络名不可靠（显示 '?'），但源文本 PAD_NET 完整

### 自动布线器（route_v2.py，实测 2026-08-27 碰撞清零）

**前提：坐标单位 mil，网格 4mil，障碍=每器件引脚包围盒外扩 12mil（含 U1/U2 引脚带），所有器件引脚四边扇出到器件矩形外 4mil，A* 4 方向寻路，路径转直角折线（只合并同轴向，禁斜线）。**

**踩过的两个致命 bug（线压引脚根因）：**
1. A* 路径简化成斜线段时只验证 L 型路径（走直角）是否通畅，**没验证斜线段本身经过的格子**——斜线横穿器件引脚带。修复：禁斜线，输出纯直角折线。
2. L/Z 快速连线（水平/垂直直接连接）**直接 return 不查障碍**，横穿沿途器件。修复：所有直接连线分支也做逐格扫障碍检查。

**审计铁律（每次画完必须跑）：**
- 每条线段与【所有非本网络引脚】的距离 ≥ 14mil（半线宽6+安全余量），0 碰撞才算过
- 机械固定脚（net 为空，如 USB 座 4 角固定脚）豁免，不算碰撞
- 连通性：每网络所有引脚的 (des,pad) 必须有线段端点触达（距离<15mil）
- 验证必须从 EDA 源文本拉真实落盘 LINE 重新审计，不能只信本地 json

## 读取原理图内容（实测通过 2026-08-27）

链路：`openDocument(pageUuid)` 打开原理图页 → `getDocumentSource()` 拿源文本 → 解析。

```javascript
// 1. 打开原理图页（uuid 从 getAllSchematicsInfo/getAllSchematicPagesInfo 拿）
await eda.dmt_EditorControl.openDocument('766f1abc012b8f9b');
await new Promise(r => setTimeout(r, 2000));  // 等文档加载
// 2. 确认当前焦点文档类型 (1=原理图, 3=PCB)
const doc = await eda.dmt_SelectControl.getCurrentDocumentInfo();
// 3. 读取文档源（原理图约 250KB，PCB 约 1.5MB，别直接打屏）
const src = await eda.sys_FileManager.getDocumentSource();
```

源文本格式：每行 `{header json}||{data json}|`，行尾带 `|` 分隔符，解析时 `data.rstrip('|')` 再 json.loads。
- `header.type == 'COMPONENT'` → 器件实例，`data.partId` 是器件型号（如 TYPE-C 6PLTH6.8-DJ.1）
- 位号/名称在 ATTR 行里，key 可能是 Designator/Name（本图未提取到，待补）
- `header.type == 'WIRE'` → 连线，坐标在 x1/y1/x2/y2

其他读取 API：`sch_Netlist.getNetlist()`（网表，已废弃建议用 sch_ManufactureData.getNetlistFile）、`sys_FileManager.getSchematicFile(fileName,password,fileType)`（原理图文件）。
