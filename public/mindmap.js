/**
 * 思维导图模块 — 内嵌在 fileBrowser 内的视图，与文件管理视图互斥切换
 * 依赖：AntV X6（全局变量 X6）
 * 复用 index.html 的 esc() 函数
 */

// ── 视图切换按钮图标（与文字同步切换）──
var _MM_BTN_MM_ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="18" cy="5" r="3"/><circle cx="6" cy="12" r="3"/><circle cx="18" cy="19" r="3"/><line x1="8.59" y1="13.51" x2="15.42" y2="17.49"/><line x1="15.41" y1="6.51" x2="8.59" y2="10.49"/></svg>';
var _MM_BTN_FB_ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>';
function _setMindMapBtn(label, iconSvg) {
  var btn = document.getElementById('fbMindMapBtn');
  if (!btn) return;
  var lb = btn.querySelector('.fb-btn-label');
  if (lb) lb.textContent = label; else btn.textContent = label;
  var sv = btn.querySelector('svg');
  if (sv && iconSvg) sv.outerHTML = iconSvg;
}

// ── 注册思维导图专用 S 曲线 connector（cubic-horizontal，XMind 同款）──
// 控制点水平延伸，形成平滑 S 曲线，比 X6 内置 smooth 更适合思维导图
(function () {
  try {
    if (typeof X6 !== 'undefined' && X6.Graph && typeof X6.Graph.registerConnector === 'function') {
      X6.Graph.registerConnector('cubic-horizontal', function (sourcePoint, targetPoint, routePoints, options) {
        var sx = sourcePoint.x, sy = sourcePoint.y;
        var tx = targetPoint.x, ty = targetPoint.y;
        var cx = (sx + tx) / 2;  // 控制点 x 取中点，水平延伸
        // 三次贝塞尔 S 曲线：两控制点分别与源/目标同 y，x 为中点
        return 'M ' + sx + ' ' + sy + ' C ' + cx + ' ' + sy + ', ' + cx + ' ' + ty + ', ' + tx + ' ' + ty;
      }, true);
    }
  } catch (_) {}
})();

// ── 注入选择框样式：去掉 X6 默认的黄色边框 + 白色半透明填充（"白色荧光"）──
// 只保留细蓝色虚线边框，干净清爽
(function () {
  if (document.getElementById('mm-selection-css')) return;
  var style = document.createElement('style');
  style.id = 'mm-selection-css';
  style.textContent = [
    '/* 节点选择框：去黄色边框 + 白色荧光，改细蓝虚线 */',
    '.x6-node-selection-box,',
    '.x6-edge-selection-box,',
    '.x6-selected-box,',
    'rect[class*="selection-box"],',
    'rect[class*="selected-box"] {',
    '  fill: transparent !important;',
    '  stroke: rgba(74,163,255,0.55) !important;',
    '  stroke-width: 1.2 !important;',
    '  stroke-dasharray: 4 3 !important;',
    '  rx: 4 !important;',
    '  ry: 4 !important;',
    '}',
    '/* rubberband 框选时的预览框（如果用 X6 原生 rubberband）*/',
    '.x6-rubberband {',
    '  fill: rgba(74,163,255,.08) !important;',
    '  stroke: #4aa3ff !important;',
    '  stroke-width: 1 !important;',
    '  stroke-dasharray: 4 3 !important;',
    '}',
    '/* 工具栏按钮 hover 高亮 */',
    '.mm-tb-btn { transition: background .12s, border-color .12s, color .12s; }',
    '.mm-tb-btn:hover { background: var(--hover) !important; border-color: var(--border2) !important; }'
  ].join('\n');
  document.head.appendChild(style);
})();

// ── 状态 ──
var _mmGraph = null;
var _mmData = null;       // 当前 mindmap 数据 {nodes, edges, ...}
var _mmContainer = null;
var _mmEditable = false;
var _mmSaveTimer = null;
var _mmDirty = false;
var _mmViewActive = false; // 思维导图视图是否当前激活
var _mmSilentApply = false;  // 静默模式：AI 改图/撤销重做增量更新时 true，事件监听忽略脏标记

// ── 节点类型样式 ──
var MM_TYPES = {
  idea:      { name: '想法',   color: '#c8d0e0', stroke: 1.2, dash: '',    fill: 'transparent', rx: 8 },
  decision:  { name: '决策',   color: '#f2b84b', stroke: 2,   dash: '',    fill: 'rgba(242,184,75,.08)', rx: 8 },
  question:  { name: '问题',   color: '#8b5cf6', stroke: 1.5, dash: '5,3', fill: 'transparent', rx: 8 },
  task_ref:  { name: '任务',   color: '#3ddc84', stroke: 1.5, dash: '',    fill: 'rgba(61,220,132,.06)', rx: 8 },
};

// ── 连线样式（router）字典 ──
// cubic = 思维导图标准 S 曲线（cubic-horizontal），XMind/MindNode 同款，控制点水平延伸不穿框
var MM_ROUTERS = {
  cubic:     { name: 'S曲线(推荐)', router: null,                                  connector: { name: 'cubic-horizontal' } },
  rounded:   { name: '圆角直线',   router: null,                                  connector: { name: 'rounded', args: { radius: 8 } } },
  manhattan: { name: '正交圆角',   router: { name: 'manhattan', args: { padding: 8 } }, connector: { name: 'rounded', args: { radius: 6 } } },
  normal:    { name: '直线',       router: null,                                  connector: { name: 'normal' } },
  curve:     { name: '贝塞尔',     router: null,                                  connector: { name: 'smooth' } },
  orth:      { name: '正交直角',   router: { name: 'orth', args: { padding: 8 } },   connector: { name: 'normal' } }
};
var _mmRouterKey = localStorage.getItem('mm_router') || 'cubic';

// ── 布局算法字典 ──
// custom = 自定义，保持用户手动调整的位置，不自动重排
var MM_LAYOUTS = {
  'tree-h':  { name: '水平树形(默认)', algo: 'tree',  dir: 'LR' },
  'tree-v':  { name: '垂直树形',       algo: 'tree',  dir: 'TB' },
  'tree-rl': { name: '反向树形',       algo: 'tree',  dir: 'RL' },
  'dagre-lr':{ name: 'dagre横向',      algo: 'dagre', dir: 'LR' },
  'dagre-tb':{ name: 'dagre树形',      algo: 'dagre', dir: 'TB' },
  'custom':  { name: '自定义(保持)',   algo: 'custom', dir: null }
};
var _mmLayoutKey = localStorage.getItem('mm_layout') || 'tree-h';
if (!MM_LAYOUTS[_mmLayoutKey]) _mmLayoutKey = 'tree-h';
var _mmUserMoved = false;  // 用户是否手动拖动过节点（未拖动时 AI 改图走全局自动布局）
// 单条边颜色板（右键边可设）
var MM_EDGE_COLORS = ['#9aa5b8', '#4aa3ff', '#3ddc84', '#f2b84b', '#8b5cf6', '#ff6b6b', '#888888'];
// 颜色编码 → 中文名映射（菜单显示用）
var MM_COLOR_NAMES = {
  '#9aa5b8': '灰蓝', '#4aa3ff': '蓝色', '#3ddc84': '绿色', '#f2b84b': '橙色',
  '#8b5cf6': '紫色', '#ff6b6b': '红色', '#888888': '灰色', '#e8eaed': '浅灰'
};
function mmColorName(c) { return (c && MM_COLOR_NAMES[c.toLowerCase()]) || c || '默认色'; }

var MM_FONT = 13, MM_LINE_H = 19, MM_PAD_Y = 10, MM_NODE_W = 200;

// ── 文本换行（16字/行，最多4行）──
function mmWrap(t) {
  t = String(t || '').replace(/[\r\n\t]+/g, ' ');
  var ls = [];
  for (var i = 0; i < t.length && ls.length < 4; i += 16) ls.push(t.substring(i, i + 16));
  if (t.length > 64) ls[3] = ls[3].substring(0, 15) + '…';
  return ls.length ? ls : [' '];
}

// 文件管理视图的子元素 ID（切换时统一隐藏/显示）
var _FB_FILE_ELEMENTS = ['fbActionBar', 'fbXferPanel', 'fbWorkspace', 'fbBreadcrumb', 'fbGrid', 'fbViewer'];

// ── 切换到思维导图视图 ──
async function showMindMapView() {
  if (!currentTopic) { alert('请先选择一个话题'); return; }
  _mmViewActive = true;

  // 隐藏文件管理子元素
  _FB_FILE_ELEMENTS.forEach(function (id) {
    var el = document.getElementById(id);
    if (el) el.style.display = 'none';
  });
  // 隐藏上传栏
  var upBar = document.querySelector('.fb-upload-bar');
  if (upBar) upBar.style.display = 'none';

  // 显示思维导图视图
  var view = document.getElementById('mindMapView');
  if (!view) return;
  view.style.display = 'flex';

  // 更新顶栏标题和按钮
  var hdr = document.getElementById('fbHeader');
  if (hdr) {
    var span = hdr.querySelector('span');
    if (span) span.textContent = '思维导图';
  }
  _setMindMapBtn('文件管理', _MM_BTN_FB_ICON);

  // 隐藏文件管理专属按钮（粘贴、队列、修改工作区）
  var pasteBtn = document.getElementById('fbPasteBtn');
  if (pasteBtn) pasteBtn.style.display = 'none';
  var xferBtn = document.getElementById('fbXferBtn');
  if (xferBtn) xferBtn.style.display = 'none';
  var wsBtn = document.getElementById('fbChangeWsBtn');
  if (wsBtn) wsBtn.style.display = 'none';

  view.innerHTML = '<div style="padding:20px;text-align:center;color:var(--tdim)">加载中…</div>';
  if (_mmGraph) { try { _mmGraph.dispose(); } catch (_) {} _mmGraph = null; }

  try {
    var tid = currentTopic.id;
    var r = await fetch('/api/topic/' + encodeURIComponent(tid) + '/mindmap');
    var d = await r.json();
    _mmData = (d && d.mindmap) || { nodes: [], edges: [] };
    if (!_mmData.nodes) _mmData.nodes = [];
    if (!_mmData.edges) _mmData.edges = [];
    // 恢复全局 router：优先 JSON，fallback localStorage，再 fallback 默认
    if (_mmData.router && MM_ROUTERS[_mmData.router]) {
      _mmRouterKey = _mmData.router;
    } else {
      _mmRouterKey = localStorage.getItem('mm_router') || 'cubic';
    }
    // 恢复布局：优先 JSON，fallback localStorage
    if (_mmData.layout && MM_LAYOUTS[_mmData.layout]) {
      _mmLayoutKey = _mmData.layout;
      localStorage.setItem('mm_layout', _mmLayoutKey);
    } else {
      _mmLayoutKey = localStorage.getItem('mm_layout') || 'tree-h';
    }
    if (!MM_LAYOUTS[_mmLayoutKey]) _mmLayoutKey = 'tree-h';
    // 恢复用户拖动标记（拖动过 → AI 改图走局部排版，否则全局自动布局）
    _mmUserMoved = !!_mmData.user_moved;
    _renderMindMapUI(view);
    // 初始化撤销/重做按钮状态
    setTimeout(mmUpdateHistoryButtons, 100);
  } catch (e) {
    view.innerHTML = '<div style="padding:20px;text-align:center;color:var(--danger)">加载失败: ' + esc(e.message) + '</div>';
  }
}

// ── 从后端重拉数据并增量更新（AI 改图后由 /api/working 轮询触发）──
// 增量更新：保留 graph 和视图状态（缩放/平移），只更新有变化的节点/边
// autoLayout=true 时对新节点（坐标为0或新增的）做局部重排，不影响用户已调整的节点
var _mmRefreshing = false;  // 防重入
async function mmRefreshFromBackend(autoLayout) {
  if (_mmRefreshing) return;             // 上一次刷新还没完，跳过
  if (!_mmViewActive || !currentTopic) return;
  _mmRefreshing = true;
  try {
    var tid = currentTopic.id;
    var r = await fetch('/api/topic/' + encodeURIComponent(tid) + '/mindmap');
    var d = await r.json();
    var newData = (d && d.mindmap) || { nodes: [], edges: [] };
    if (!newData.nodes) newData.nodes = [];
    if (!newData.edges) newData.edges = [];
    // 恢复全局 router（AI 可能改了图，但 router 应保持用户上次选择）
    if (newData.router && MM_ROUTERS[newData.router]) {
      _mmRouterKey = newData.router;
    }
    // 恢复布局选择
    if (newData.layout && MM_LAYOUTS[newData.layout]) {
      _mmLayoutKey = newData.layout;
    }
    // 恢复用户拖动标记
    if (typeof newData.user_moved === 'boolean') _mmUserMoved = newData.user_moved;

    // 增量更新：保留 graph 和视图状态，只更新节点/边
    if (_mmGraph) {
      _mmApplyDataIncremental(newData, autoLayout);
      _mmData = newData;
    } else {
      // graph 不存在（首次加载或被销毁），走完整渲染
      _mmData = newData;
      var view = document.getElementById('mindMapView');
      if (view) _renderMindMapUI(view);
    }
  } catch (e) {
    // 静默失败，不打扰用户
    console.warn('mmRefreshFromBackend failed:', e);
  } finally {
    _mmRefreshing = false;
  }
}

// ── 增量应用数据到现有 graph（不销毁、不重建、不重置视图）──
// newData: 新拉取的 {nodes, edges}
// autoLayout: 是否对新增/未布局节点做局部重排（AI 改图 true，撤销/重做 false）
// restorePosition: 是否强制恢复节点 x/y 位置（撤销/重做必须 true，
//                  AI 改图 false 以保留用户手动调整的位置）
function _mmApplyDataIncremental(newData, autoLayout, restorePosition) {
  if (!_mmGraph) return;
  var graph = _mmGraph;
  // 静默模式：AI 改图/撤销重做的增量更新不触发脏标记，
  // 因为数据已是后端持久化状态，不需要再保存回后端（否则会产生重复的 history 条目）
  _mmSilentApply = true;
  try {

  // 收集现有节点 mmid → cell
  var existingNodes = {};  // mmid -> cell
  graph.getCells().forEach(function (c) {
    if (c.isNode && c.isNode()) {
      var d = c.getData() || {};
      var mmid = d.id || c.id;
      existingNodes[mmid] = c;
    }
  });

  // 新数据节点 mmid 集合
  var newNodeIds = {};
  newData.nodes.forEach(function (n) { newNodeIds[n.id] = true; });

  // 1. 删除不再存在的节点
  Object.keys(existingNodes).forEach(function (mmid) {
    if (!newNodeIds[mmid]) {
      try { existingNodes[mmid].remove(); } catch (_) {}
    }
  });

  // 2. 新增/更新节点
  var addedCells = [];       // 新增的节点 cell
  var modifiedCells = [];    // 被 AI 修改的现有节点 cell（文本/类型变化）
  newData.nodes.forEach(function (n) {
    var existing = existingNodes[n.id];
    if (existing) {
      // 更新现有节点（文本、类型等可能被 AI 改了）
      try {
        var attrs = existing.getAttrs() || {};
        var modified = false;
        if (attrs.label && attrs.label.text) {
          var newText = mmWrap(n.text).join('\n');
          if (attrs.label.text !== newText) {
            existing.setAttrByPath('label/text', newText);
            modified = true;
          }
        }
        // 更新 data + 颜色/类型样式同步
        // 关键：撤销/重做改色操作时，必须把 data.color 重新应用到 body/stroke，
        // 否则 cell 的 attrs 还停留在旧颜色，看上去撤销没生效
        var d = existing.getData() || {};
        var newColor = n.color || null;
        var newType = n.type || d.type || 'idea';
        var colorOrTypeChanged = (d.note !== n.note) || (d.type !== newType) || ((d.color || null) !== newColor);
        if (colorOrTypeChanged) {
          existing.setData(Object.assign({}, d, { note: n.note, type: newType, color: newColor, links: n.links }));
          modified = true;
        }
        // 颜色/类型变了：重算 label/fill + body/stroke（用户 color 优先于类型色）
        if (colorOrTypeChanged) {
          var nt = MM_TYPES[newType] || MM_TYPES.idea;
          var finalColor = newColor || nt.color;
          try {
            existing.setAttrByPath('label/fill', finalColor);
            existing.setAttrByPath('body/stroke', finalColor);
            existing.setAttrByPath('body/strokeWidth', nt.stroke);
            existing.setAttrByPath('body/strokeDasharray', nt.dash);
            existing.setAttrByPath('body/fill', nt.fill);
          } catch (_) {}
        }
        // 撤销/重做场景：强制恢复节点位置（用户拖动后的位置变化必须能撤销）
        // AI 改图场景：跳过位置更新，保留用户手动调整的位置
        if (restorePosition && (n.x !== undefined || n.y !== undefined)) {
          var curPos = existing.getPosition();
          var nx = n.x !== undefined ? n.x : curPos.x;
          var ny = n.y !== undefined ? n.y : curPos.y;
          if (Math.abs(curPos.x - nx) > 0.5 || Math.abs(curPos.y - ny) > 0.5) {
            existing.setPosition(nx, ny);
          }
        }
        if (modified) modifiedCells.push(existing);
      } catch (_) {}
    } else {
      // 新增节点（复用 buildMindMapX6 的节点创建逻辑）
      var cell = _mmAddNodeToGraph(graph, n, _mmEditable);
      if (cell) {
        // 标记为新节点（供局部重排识别）
        try {
          var nd = cell.getData() || {};
          cell.setData(Object.assign({}, nd, { _mmNew: true }));
        } catch (_) {}
        addedCells.push(cell);
      }
    }
  });

  // 3. 智能更新边（只删变化的，保留未变的——避免全删全建的视觉跳动和性能开销）
  var oldEdgeMap = {};  // "source->target" -> edgeCell
  graph.getCells().forEach(function (c) {
    if (c.isEdge && c.isEdge()) {
      var s = c.getSourceCellId(), t = c.getTargetCellId();
      if (s && t) oldEdgeMap[s + '->' + t] = c;
    }
  });
  var newEdgePairs = {};  // 新数据中的边对
  newData.edges.forEach(function (e) { newEdgePairs[e.source + '->' + e.target] = e; });
  // 3a. 删除不再存在的边
  Object.keys(oldEdgeMap).forEach(function (key) {
    if (!newEdgePairs[key]) { try { oldEdgeMap[key].remove(); } catch (_) {} }
  });
  // 3b. 添加新边 + 同步已存在边的样式（color/dash/router/label）
  // 关键修复：AI 改了边样式（如标橙色虚线表示矛盾）后，前端轮询拉到新数据，
  // 必须同步到已存在的 edge cell，否则 mmSave 整图覆盖会用旧默认色冲掉 AI 的样式
  var newEdgeSources = {};
  newData.edges.forEach(function (e) {
    var key = e.source + '->' + e.target;
    var existing = oldEdgeMap[key];
    if (!existing) {
      _mmAddEdgeToGraph(graph, e);
      newEdgeSources[e.source] = true;
    } else {
      // 已存在的边：同步样式字段（AI 可能改了颜色/虚线/标签/router）
      _mmSyncEdgeCell(existing, e);
    }
  });

  // 4. 重排：用户从未拖动过节点 → 全局自动布局；拖动过 → 只局部重排受影响的子树
  // 注意：mmLayoutSubtree 是局部排列（只重排选中节点的子树，根节点和其他子树不动），
  //       所以即使 custom 自定义布局模式下也安全执行——不会破坏用户手动调整的位置。
  if (autoLayout) {
    if (!_mmUserMoved) {
      // 用户未自定义过位置：整图全局自动布局，效果最整齐
      try { mmAutoLayout(); } catch (e) { console.warn('[mm] autoLayout failed:', e); }
    } else {
    var rootsToLayout = {};  // cellId -> cell，需要做子树排列的根节点

    // 4a. 新增节点：找其已存在的最近祖先，对祖先做子树排列
    addedCells.forEach(function (cell) {
      var parent = _mmFindExistingAncestor(cell.id, graph);
      if (parent) {
        rootsToLayout[parent.id] = parent;
      } else {
        rootsToLayout[cell.id] = cell;
      }
    });

    // 4b. 被 AI 修改的节点：对该节点做子树排列（排列它后面的内容）
    modifiedCells.forEach(function (cell) {
      rootsToLayout[cell.id] = cell;
    });

    // 4c. 新边的源节点：对该节点做子树排列（新加的连线需要重排子树）
    Object.keys(newEdgeSources).forEach(function (srcId) {
      var srcCell = graph.getCellById(srcId);
      if (srcCell && srcCell.isNode()) {
        rootsToLayout[srcId] = srcCell;
      }
    });

    var laidCount = 0;
    Object.keys(rootsToLayout).forEach(function (rid) {
      // silent=true：批量调用不弹 toast，避免刷屏
      try { mmLayoutSubtree(rootsToLayout[rid], true); laidCount++; } catch (e) { console.warn('[mm] layoutSubtree failed:', e); }
    });
    if (laidCount > 0) mmToast('AI 改图已自动排列 ' + laidCount + ' 处后续枝干');
    }
  }
  // autoLayout=false 时静默跳过（撤销/重做场景）

  } finally {
    _mmSilentApply = false;
  }
}

// 查找节点的最近"已存在"祖先（沿 incoming 边向上找，第一个在 addedCells 之外的节点）
function _mmFindExistingAncestor(cellId, graph) {
  var seen = {};
  seen[cellId] = true;
  function up(cid) {
    var inEdges = graph.getIncomingEdges(cid) || [];
    for (var i = 0; i < inEdges.length; i++) {
      var srcId = inEdges[i].getSourceCellId();
      if (!srcId || seen[srcId]) continue;
      seen[srcId] = true;
      // 只要不是本次新增的节点，就当作"已存在祖先"
      var srcCell = graph.getCellById(srcId);
      if (srcCell) {
        var d = srcCell.getData() || {};
        // 检查是否在 addedCells 中——简化处理：只要不是新建的就算已存在
        // 这里用一个标记：新增节点会有 _mmNew 标记
        if (!d._mmNew) return srcCell;
        return up(srcId);
      }
    }
    return null;
  }
  return up(cellId);
}

// 向 graph 添加节点（复用 buildMindMapX6 的节点创建逻辑，返回 cell）
function _mmAddNodeToGraph(graph, rn, editable) {
  var t = MM_TYPES[rn.type] || MM_TYPES.idea;
  var text = String(rn.text || '未命名').replace(/[\r\n\t]+/g, ' ');
  var lines = mmWrap(text);
  var h = lines.length * MM_LINE_H + MM_PAD_Y * 2;
  var nodeSpec = {
    id: rn.id, shape: 'rect', width: MM_NODE_W, height: h,
    x: rn.x || 0, y: rn.y || 0,
    attrs: {
      body: { fill: t.fill, stroke: rn.color || t.color, strokeWidth: t.stroke, rx: t.rx, ry: t.rx, strokeDasharray: t.dash },
      label: { text: lines.join('\n'), fill: t.color, fontSize: MM_FONT, textAnchor: 'middle', textVerticalAnchor: 'middle', lineHeight: MM_LINE_H }
    },
    data: { id: rn.id, type: rn.type || 'idea', note: rn.note || '', color: rn.color || null, collapsed: rn.collapsed || false, links: rn.links || [] }
  };
  if ((rn.type || 'idea') === 'task_ref') {
    nodeSpec.attrs.body.strokeWidth = 2;
  }
  // 始终添加 ports（不管是否编辑模式），通过 visibility 控制显示
  var portVis = editable ? 'visible' : 'hidden';
  nodeSpec.ports = {
    groups: {
      top: { position: 'top', attrs: { circle: { r: 2.5, magnet: true, stroke: t.color, fill: '#fff', strokeWidth: 1.2, visibility: portVis } } },
      right: { position: 'right', attrs: { circle: { r: 2.5, magnet: true, stroke: t.color, fill: '#fff', strokeWidth: 1.2, visibility: portVis } } },
      bottom: { position: 'bottom', attrs: { circle: { r: 2.5, magnet: true, stroke: t.color, fill: '#fff', strokeWidth: 1.2, visibility: portVis } } },
      left: { position: 'left', attrs: { circle: { r: 2.5, magnet: true, stroke: t.color, fill: '#fff', strokeWidth: 1.2, visibility: portVis } } }
    },
    items: [{ id: 'pt', group: 'top' }, { id: 'pr', group: 'right' }, { id: 'pb', group: 'bottom' }, { id: 'pl', group: 'left' }]
  };
  try {
    graph.addNode(nodeSpec);
    return graph.getCellById(rn.id);
  } catch (_) {
    return null;
  }
}

// 向 graph 添加边（复用 buildMindMapX6 的边创建逻辑）
function _mmAddEdgeToGraph(graph, e) {
  var src = e.source, tgt = e.target;
  if (!src || !tgt) return;
  var globalRouter = MM_ROUTERS[_mmRouterKey] || MM_ROUTERS.cubic;
  var dashMode = e.dash || (e.style === 'dashed' ? 'dashed' : 'solid');
  var isDashed = (dashMode === 'dashed');
  var stroke = e.color || 'rgba(255,255,255,.55)';
  var rKey = e.router, rCfg = rKey ? (MM_ROUTERS[rKey] || globalRouter) : globalRouter;
  var lineAttrs = { stroke: stroke, strokeWidth: 1.5, strokeDasharray: isDashed ? '4,3' : '', targetMarker: { name: 'block', size: 6 } };
  if (e.bidir) lineAttrs.sourceMarker = { name: 'block', size: 6 }; // 双向箭头
  var edgeSpec = {
    id: e.id,
    source: src, target: tgt,
    attrs: { line: lineAttrs },
    labels: e.label ? [{
      position: 0.5,
      markup: [{ tagName: 'rect', selector: 'bg' }, { tagName: 'text', selector: 'label' }],
      attrs: {
        label: { text: e.label, fontSize: 12, fill: '#000', fontWeight: 600, textAnchor: 'middle', textVerticalAnchor: 'middle' },
        bg: { ref: 'label', refX: -3, refY: -2, refWidth: 6, refHeight: 4, fill: e.color || '#e8eaed', stroke: 'none' }
      }
    }] : undefined,
    data: { id: e.id, label: e.label || '', style: e.style || 'solid', router: rKey || null, color: e.color || null, dash: dashMode, bidir: !!e.bidir }
  };
  if (rCfg.router) edgeSpec.router = rCfg.router;
  if (rCfg.connector) edgeSpec.connector = rCfg.connector;
  // 自动选边界最近点 + 曼哈顿路由避障
  edgeSpec.connectionPoint = 'boundary';
  edgeSpec.anchor = 'center';
  if (!edgeSpec.router) edgeSpec.router = { name: 'manhattan', args: { padding: 12 } };
  try { graph.addEdge(edgeSpec); } catch (_) {}
}

// 边标签样式说明（已内联到 edgeSpec.attrs，不再用工厂函数）：
// - attrs.label: 文字属性（fill:'#000' 黑色）
// - attrs.labelBody: 背景矩形属性（fill = 线颜色，无线色时白底）
// - 通过 edge 的 label: { position: 0.5 } 定位到边中点
// X6 v1 边默认 markup 含 <rect selector="labelBody"/> + <text selector="label"/>
// 创建时用 attrs.labelBody 设背景，更新时用 setAttrByPath('labelBody/fill', color)

// ── 同步已存在边的样式字段（color/dash/router/label）到 X6 edge cell ──
// 用于增量更新：AI 改了边样式（如标橙色虚线表示矛盾）后，前端轮询拉到新数据，
// 必须同步到已存在的 edge cell，否则 mmSave 整图覆盖会用旧默认色冲掉 AI 的样式。
// router 变化时直接重建该边（X6 v1 动态改 router/connector 不稳定）。
function _mmSyncEdgeCell(edgeCell, e) {
  try {
    var edata = edgeCell.getData() || {};
    var newColor = e.color || null;
    var newDash = e.dash || (e.style === 'dashed' ? 'dashed' : 'solid');
    var newLabel = e.label || '';
    var newRouter = e.router;  // 可能是 null（跟随全局）、undefined（未指定）、字符串
    if (newRouter === undefined) newRouter = edata.router || null;
    var newStyle = e.style || 'solid';
    var newBidir = (e.bidir === undefined) ? !!edata.bidir : !!e.bidir;

    // 注意：不做 diff 短路。调用方（右键菜单 action）会先 cell.setData 把新值写进 data，
    // 这里再读 edata 做 diff 永远是 false，导致 attrs 不更新、视觉不变化。
    // X6 的 setAttrByPath/setRouter/setConnector 是幂等的，单条边更新开销可忽略，无需 diff 优化。

    // 更新 data
    edgeCell.setData(Object.assign({}, edata, {
      color: newColor, dash: newDash, label: newLabel, style: newStyle, router: newRouter, bidir: newBidir
    }));

    // router：用 setRouter/setConnector 动态修改（X6 v1 支持，无需重建边）
    var globalRouter = MM_ROUTERS[_mmRouterKey] || MM_ROUTERS.cubic;
    var rCfg = newRouter ? (MM_ROUTERS[newRouter] || globalRouter) : globalRouter;
    try {
      if (rCfg.router) {
        edgeCell.setRouter(rCfg.router);
      } else {
        // 清除 router（设为 null 让 X6 用默认直连）
        edgeCell.setRouter({ name: 'normal' });
      }
      if (rCfg.connector) {
        edgeCell.setConnector(rCfg.connector);
      } else {
        edgeCell.setConnector({ name: 'normal' });
      }
    } catch (_) {}

    // 更新 attrs（线条颜色/虚线/双向箭头）
    var stroke = newColor || 'rgba(255,255,255,.55)';
    var isDashed = (newDash === 'dashed');
    try {
      edgeCell.setAttrByPath('line/stroke', stroke);
      edgeCell.setAttrByPath('line/strokeDasharray', isDashed ? '4,3' : '');
      if (newBidir) edgeCell.setAttrByPath('line/sourceMarker', { name: 'block', size: 6 });
      // X6 把 sourceMarker 映射成 DOM marker-start，置 null 走通用路径删不掉已画箭头；
      // 用零尺寸透明 marker 覆盖，视觉等同移除
      else edgeCell.setAttrByPath('line/sourceMarker', { name: 'path', d: 'M 0 0', fill: 'none', stroke: 'none' });
    } catch (_) {}

    // 更新 label（文字色跟随线颜色）
    try {
      if (newLabel) {
        edgeCell.setLabels([{ position: 0.5, markup: [{ tagName: 'rect', selector: 'bg' }, { tagName: 'text', selector: 'label' }], attrs: { label: { text: newLabel, fontSize: 12, fill: '#000', fontWeight: 600, textAnchor: 'middle', textVerticalAnchor: 'middle' }, bg: { ref: 'label', refX: -3, refY: -2, refWidth: 6, refHeight: 4, fill: newColor || '#e8eaed', stroke: 'none' } } }]);
      } else {
        edgeCell.setLabels([]);
      }
    } catch (_) {}
  } catch (_) {}
}

// ── 切换回文件管理视图 ──
function showFileView() {
  // 关闭前强制保存
  if (_mmDirty) mmSave(true);

  _mmViewActive = false;
  // 销毁 X6 图（内部会自动 disconnect ResizeObserver 和移除事件监听）
  if (_mmGraph) { try { _mmGraph.dispose(); } catch (_) {} _mmGraph = null; }

  // 隐藏思维导图视图
  var view = document.getElementById('mindMapView');
  if (view) view.style.display = 'none';

  // 显示文件管理子元素（恢复默认 display）
  var defaults = {
    'fbActionBar': 'none',     // action bar 默认隐藏（选中文件时才显示）
    'fbXferPanel': 'none',     // 传输队列默认隐藏
    'fbWorkspace': 'block',
    'fbBreadcrumb': 'block',
    'fbGrid': 'grid',
    'fbViewer': 'none'
  };
  _FB_FILE_ELEMENTS.forEach(function (id) {
    var el = document.getElementById(id);
    if (el) el.style.display = defaults[id] || '';
  });
  // 显示上传栏
  var upBar = document.querySelector('.fb-upload-bar');
  if (upBar) upBar.style.display = '';

  // 恢复顶栏标题和按钮
  var hdr = document.getElementById('fbHeader');
  if (hdr) {
    var span = hdr.querySelector('span');
    if (span) span.textContent = '文件管理';
  }
  _setMindMapBtn('思维导图', _MM_BTN_MM_ICON);

  // 恢复文件管理专属按钮
  var xferBtn = document.getElementById('fbXferBtn');
  if (xferBtn) xferBtn.style.display = '';
  var wsBtn = document.getElementById('fbChangeWsBtn');
  if (wsBtn) wsBtn.style.display = '';
  // pasteBtn 由 pasteFiles 逻辑控制，不强制恢复
}

// ── 切换入口 ──
function toggleMindMapView() {
  if (_mmViewActive) {
    showFileView();
  } else {
    showMindMapView();
  }
}

// ── 渲染思维导图 UI ──
function _renderMindMapUI(content) {
  var isMob = mmIsMobile();
  var html = '';
  html += '<div style="flex:1 1 auto;min-height:0;display:flex;flex-direction:column;border:1px solid var(--border2);border-radius:var(--radius-md);overflow:hidden;background:transparent;position:relative">';
  // 顶栏：左组[添加][编辑][↶撤销][↷重做]  右组[缩放%][设置]
  // 左组=操作类（编辑历史），右组=视图类（视图状态/配置）
  html += '<div style="flex:0 0 auto;padding:4px 10px;border-bottom:1px solid var(--border);display:flex;justify-content:space-between;align-items:center;gap:6px;background:transparent;user-select:none;flex-wrap:wrap">';
  html += '<div style="display:flex;gap:4px;align-items:center;flex-wrap:wrap">';
  html += '<button class="mm-tb-btn" onclick="mmAddNodeBtn()" style="font-size:11px;padding:3px 8px;background:transparent;color:var(--fg);border:1px solid var(--border);border-radius:4px;cursor:pointer" title="添加节点">添加</button>';
  html += '<button class="mm-tb-btn" onclick="mmToggleEdit()" id="mmEditBtn" style="font-size:11px;padding:3px 8px;background:transparent;color:var(--fg);border:1px solid var(--border);border-radius:4px;cursor:pointer" title="切换编辑模式">编辑</button>';
  html += '<button class="mm-tb-btn" onclick="mmUndo()" id="mmUndoBtn" style="font-size:13px;padding:3px 9px;background:transparent;color:var(--fg);border:1px solid var(--border);border-radius:4px;cursor:pointer" title="撤销 (Ctrl+Z)">↶</button>';
  html += '<button class="mm-tb-btn" onclick="mmRedo()" id="mmRedoBtn" style="font-size:13px;padding:3px 9px;background:transparent;color:var(--fg);border:1px solid var(--border);border-radius:4px;cursor:pointer" title="重做 (Ctrl+Shift+Z)">↷</button>';
  html += '</div>';
  html += '<div style="display:flex;gap:4px;align-items:center">';
  html += '<span id="mmZoomLabel" style="font-size:11px;color:var(--tdim);font-variant-numeric:tabular-nums;min-width:36px;text-align:right;cursor:pointer" onclick="mmZoomToFit()" title="点击适应画布">100%</span>';
  html += '<button class="mm-tb-btn" onclick="mmShowSettingsMenu(event)" style="font-size:11px;padding:3px 8px;background:transparent;color:var(--fg);border:1px solid var(--border);border-radius:4px;cursor:pointer" title="布局/连线/适应等设置">设置</button>';
  html += '</div>';
  html += '</div>';
  // 画布容器
  html += '<div id="mindMapX6Container" style="flex:1 1 0%;min-height:0;overflow:hidden;position:relative"></div>';
  // 移动端底部操作栏容器（编辑模式下显示，选中节点/边时出现）
  if (isMob) {
    html += '<div id="mmMobileBar" style="display:none;flex:0 0 auto;padding:6px 8px;border-top:1px solid var(--border);background:var(--surface2);justify-content:space-around;align-items:center;gap:4px;user-select:none"></div>';
  }
  html += '</div>';
  content.innerHTML = html;

  var x6c = document.getElementById('mindMapX6Container');
  if (!x6c) return;

  // 不预设高度，让 buildMindMapX6 处理（它会设置 height:100% + flex:1 1 0%）
  x6c.style.height = '';
  x6c.style.flex = '';

  _mmContainer = x6c;
  _mmGraph = buildMindMapX6(x6c, _mmData, _mmEditable);
  // buildMindMapX6 内部已注册 ResizeObserver 和 mm:panel-resize 监听，无需外部重复
  if (_mmGraph && _mmEditable) {
    var btn = document.getElementById('mmEditBtn');
    if (btn) { btn.textContent = '编辑中'; btn.style.background = 'var(--accent)'; btn.style.color = '#fff'; }
    // 移动端编辑模式显示底部操作栏提示
    if (isMob) mmUpdateMobileBar();
  }
}

// ── 标记脏 + debounce 自动保存 ──
// immediate=true 时用短防抖（150ms），用于拖动结束、属性修改、增删节点等关键操作
// 确保用户按 Ctrl+Z 时改动已持久化到后端 history 栈
// immediate=false（默认）用 2s 防抖，用于普通非关键操作
function mmMarkDirty(immediate) {
  // 静默模式（AI 改图/撤销重做增量更新）不标记脏
  if (_mmSilentApply) return;
  _mmDirty = true;
  if (_mmSaveTimer) clearTimeout(_mmSaveTimer);
  var delay = immediate ? 150 : 2000;
  _mmSaveTimer = setTimeout(function () { mmSave(true); }, delay);
}

// 立即强制 flush 脏数据（用于 Ctrl+Z/Ctrl+Shift+Z 之前，确保刚改动的可撤销）
async function mmFlushDirty() {
  if (_mmSaveTimer) { clearTimeout(_mmSaveTimer); _mmSaveTimer = null; }
  if (_mmDirty) {
    await mmSave(true);
  }
}

// 直接修改 _mmData.edges 里某条边的字段（绕过 X6 setData 不可靠问题）
// 用法：_mmSyncEdgeField(edgeCell, {router:'curve', color:'#fff', dash:'dashed'})
function _mmSyncEdgeField(edgeCell, fields) {
  if (!_mmData || !_mmData.edges) return;
  var edata = edgeCell.getData() || {};
  var eid = edata.id || edgeCell.id;
  for (var i = 0; i < _mmData.edges.length; i++) {
    if (_mmData.edges[i].id === eid) {
      for (var k in fields) {
        if (fields.hasOwnProperty(k)) _mmData.edges[i][k] = fields[k];
      }
      return;
    }
  }
}

// ── 收集 X6 数据 ──
function mmCollectData() {
  if (!_mmGraph) return null;
  var cells = _mmGraph.getCells();
  var nodes = [], edges = [];
  var idMap = {};

  cells.forEach(function (c) {
    if (c.isNode && c.isNode()) {
      var data = c.getData() || {};
      var mmid = data.id || c.id;
      idMap[c.id] = mmid;
      var attrs = c.getAttrs();
      var text = (attrs && attrs.label && attrs.label.text) ? attrs.label.text.replace(/\n/g, ' ') : '';
      var pos = c.getPosition();
      nodes.push({
        id: mmid,
        text: text,
        note: data.note || '',
        type: data.type || 'idea',
        x: Math.round(pos.x),
        y: Math.round(pos.y),
        color: data.color || null,
        collapsed: data.collapsed || false,
        links: data.links || []
      });
    }
  });

  cells.forEach(function (c) {
    if (c.isEdge && c.isEdge()) {
      var src = c.getSourceCellId(), tgt = c.getTargetCellId();
      if (src && tgt && idMap[src] && idMap[tgt]) {
        var edata = c.getData() || {};
        edges.push({
          id: edata.id || ('e_' + src + '_' + tgt),
          source: idMap[src],
          target: idMap[tgt],
          label: edata.label || '',
          style: edata.style || 'solid',
          router: edata.router || null,    // 单条边 router 覆盖（null 走全局）
          color: edata.color || null,      // 单条边颜色覆盖
          dash:  edata.dash  || null,      // 'solid' | 'dashed' | null（走全局默认）
          bidir: !!edata.bidir             // 双向箭头
        });
      }
    }
  });

  return { schema: 1, topic_id: _mmData.topic_id || '', title: _mmData.title || '', router: _mmRouterKey, layout: _mmLayoutKey, user_moved: _mmUserMoved, nodes: nodes, edges: edges };
}

// ── 保存 ──
async function mmSave(silent) {
  if (!_mmGraph) return;
  var tid = currentTopic ? currentTopic.id : '';
  if (!tid) return;

  var collected = mmCollectData();
  if (!collected) return;
  // 防御性：强制确保全局 router/layout 字段被写入
  collected.router = _mmRouterKey;
  collected.layout = _mmLayoutKey;

  try {
    var r = await fetch('/api/topic/' + encodeURIComponent(tid) + '/mindmap', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mindmap: collected })
    });
    var d = await r.json();
    if (d.ok) {
      _mmData = d.mindmap;
      _mmDirty = false;
      // 同步本地变更时间戳，避免自己保存触发自己刷新
      if (_mmData && _mmData.updated_at) window._mmLastMmUpdated = _mmData.updated_at;
      if (_mmSaveTimer) { clearTimeout(_mmSaveTimer); _mmSaveTimer = null; }
      // 保存后更新撤销/重做按钮（新改动会清空 redo 栈）
      setTimeout(mmUpdateHistoryButtons, 200);
    } else if (!silent) {
      alert('保存失败: ' + (d.error || ''));
    }
  } catch (e) {
    if (!silent) alert('保存出错: ' + e.message);
  }
}

// ── 撤销/重做（git 风格，单次请求搞定：后端返回快照+diff+按钮状态）──
async function mmUndo(steps) {
  if (!_mmViewActive || !currentTopic) return;
  // 撤销前强制 flush 脏数据：刚改完还没保存的必须先持久化，否则后端 history 栈里找不到这一步
  await mmFlushDirty();
  steps = steps || 1;
  var tid = currentTopic.id;
  try {
    var r = await fetch('/api/topic/' + encodeURIComponent(tid) + '/mindmap/undo', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ steps: steps })
    });
    var d = await r.json();
    if (d.ok && d.mindmap) {
      // 撤销：restorePosition=true 强制恢复节点位置（用户拖动可撤销）
      if (_mmGraph) {
        _mmApplyDataIncremental(d.mindmap, false, true);
        _mmData = d.mindmap;
        if (d.mindmap.updated_at) window._mmLastMmUpdated = d.mindmap.updated_at;
      }
      if (d.diff && d.diff.summary) mmToast('↶ 撤销: ' + d.diff.summary);
      // 直接用返回的按钮状态，不再额外请求
      mmApplyHistoryButtonState(d.can_undo, d.can_redo);
    } else if (d.hint) {
      mmToast(d.hint);
    }
  } catch (e) {
    mmToast('撤销失败: ' + e.message);
  }
}

async function mmRedo(steps) {
  if (!_mmViewActive || !currentTopic) return;
  // 重做前同样 flush（理论上不会有脏数据，但防御性处理）
  await mmFlushDirty();
  steps = steps || 1;
  var tid = currentTopic.id;
  try {
    var r = await fetch('/api/topic/' + encodeURIComponent(tid) + '/mindmap/redo', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ steps: steps })
    });
    var d = await r.json();
    if (d.ok && d.mindmap) {
      // 重做：restorePosition=true 强制恢复节点位置
      if (_mmGraph) {
        _mmApplyDataIncremental(d.mindmap, false, true);
        _mmData = d.mindmap;
        if (d.mindmap.updated_at) window._mmLastMmUpdated = d.mindmap.updated_at;
      }
      if (d.diff && d.diff.summary) mmToast('↷ 重做: ' + d.diff.summary);
      mmApplyHistoryButtonState(d.can_undo, d.can_redo);
    } else if (d.hint) {
      mmToast(d.hint);
    }
  } catch (e) {
    mmToast('重做失败: ' + e.message);
  }
}

// 直接应用按钮状态（不发请求，撤销/重做后用）
function mmApplyHistoryButtonState(canUndo, canRedo) {
  var undoBtn = document.getElementById('mmUndoBtn');
  var redoBtn = document.getElementById('mmRedoBtn');
  if (undoBtn) {
    undoBtn.style.opacity = canUndo ? '1' : '0.35';
    undoBtn.style.cursor = canUndo ? 'pointer' : 'not-allowed';
  }
  if (redoBtn) {
    redoBtn.style.opacity = canRedo ? '1' : '0.35';
    redoBtn.style.cursor = canRedo ? 'pointer' : 'not-allowed';
  }
}

// 初始化时查一次按钮状态（仅视图首次加载时调用）
async function mmUpdateHistoryButtons() {
  if (!_mmViewActive || !currentTopic) return;
  try {
    var tid = currentTopic.id;
    var r = await fetch('/api/topic/' + encodeURIComponent(tid) + '/mindmap/history?limit=1');
    var d = await r.json();
    mmApplyHistoryButtonState(d.can_undo, d.can_redo);
  } catch (_) {}
}

// 撤销/重做快捷键（思维导图视图激活时生效）
document.addEventListener('keydown', function (e) {
  if (!_mmViewActive) return;
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'z' && !e.shiftKey) {
    e.preventDefault();
    mmUndo();
  } else if ((e.ctrlKey || e.metaKey) && (e.key.toLowerCase() === 'y' || (e.key.toLowerCase() === 'z' && e.shiftKey))) {
    e.preventDefault();
    mmRedo();
  }
});

// ── 动态显示/隐藏所有节点的 ports（连接点小圆点）──
// 切换编辑模式时调用，不重建 graph，只修改 port 的 visibility 属性
function mmSetPortsVisible(graph, visible) {
  if (!graph) return;
  var vis = visible ? 'visible' : 'hidden';
  graph.getNodes().forEach(function (node) {
    try {
      var ports = node.getPorts();
      ports.forEach(function (p) {
        try { node.setPortProp(p.id, 'attrs/circle/visibility', vis); } catch (_) {}
      });
    } catch (_) {}
  });
}

// ── 切换编辑模式 ──
// 不重建 graph，直接修改 interacting/panning/ports 配置
// connecting 和 highlighting 在构造时已始终设置，无需重建
// 优点：无画布跳动、无性能开销（不销毁重建所有节点/边/事件）
function mmToggleEdit() {
  _mmEditable = !_mmEditable;
  localStorage.setItem('mm_editable', _mmEditable ? '1' : '0');
  var btn = document.getElementById('mmEditBtn');
  if (btn) {
    btn.textContent = _mmEditable ? '编辑中' : '编辑';
    btn.style.background = _mmEditable ? 'var(--accent)' : 'transparent';
    btn.style.color = _mmEditable ? '#fff' : 'var(--fg)';
  }
  if (!_mmGraph) return;

  // 1. 修改 interacting：控制节点可拖动、可连线
  _mmGraph.options.interacting = {
    nodeMovable: _mmEditable,
    edgeMovable: false,
    nodeResizable: false,
    edgeLabelMovable: false,
    magnetConnectable: _mmEditable,
    stopDelegateOnDragging: true
  };

  // 2. panning 不需要修改——始终用 leftMouseDown + mouseWheel
  // 编辑模式下节点拖动由 interacting.nodeMovable 控制，会自动阻止 panning（节点事件优先）
  // 连线从 port 拖出，port 事件优先于 panning，不冲突

  // 3. 显示/隐藏 ports（连接点小圆点）
  mmSetPortsVisible(_mmGraph, _mmEditable);

  // 4. 启用/禁用选择模块（PC 端编辑模式下才需要多选 + 批量操作）
  try {
    if (_mmEditable) {
      _mmGraph.enableSelection();
    } else {
      _mmGraph.cleanSelection();
      _mmGraph.disableSelection();
    }
  } catch (_) {}

  // 5. 移动端：更新底部操作栏
  if (mmIsMobile()) mmUpdateMobileBar();

  mmMarkDirty();
}

// ── 缩放适应 ──
function mmZoomToFit() {
  if (!_mmGraph) return;
  try { _mmGraph.zoomToFit({ padding: 30, maxScale: 1.2 }); } catch (_) {}
  // 用户主动 fit 后清掉保存的视图状态，下次进入重新 fit
  try {
    if (currentTopic && currentTopic.id) localStorage.removeItem('mm_view_' + currentTopic.id);
  } catch (_) {}
}

// ── 思维导图视图状态（缩放/平移）持久化：按话题区分 ──
// 保存到 localStorage，键 mm_view_{topicId}，值 {zoom, tx, ty}
// PC 端用户调整过的缩放/平移在下次打开时恢复，避免每次重新调整
var _mmSaveViewTimer = null;
function mmSaveView() {
  if (!_mmGraph || !currentTopic || !currentTopic.id) return;
  try {
    var z = _mmGraph.zoom();
    var tr = _mmGraph.translate();
    if (!z || !isFinite(z) || !isFinite(tr.tx) || !isFinite(tr.ty)) return;
    // 跳过明显的初始 fit 状态（zoom=1 且 translate 在 0 附近），避免覆盖上次真实状态
    // 但其实用户手动操作触发 scale/translate 事件时，值就是真实的，这里不再额外过滤
    var payload = JSON.stringify({ zoom: z, tx: tr.tx, ty: tr.ty, ts: Date.now() });
    localStorage.setItem('mm_view_' + currentTopic.id, payload);
  } catch (_) {}
}
function mmSaveViewDebounced() {
  if (_mmSaveViewTimer) clearTimeout(_mmSaveViewTimer);
  _mmSaveViewTimer = setTimeout(mmSaveView, 300);
}
// 恢复上次保存的视图状态。成功返回 true，没保存过/失败返回 false
function mmRestoreView() {
  if (!_mmGraph || !currentTopic || !currentTopic.id) return false;
  try {
    var raw = localStorage.getItem('mm_view_' + currentTopic.id);
    if (!raw) return false;
    var v = JSON.parse(raw);
    if (!v || !isFinite(v.zoom) || !isFinite(v.tx) || !isFinite(v.ty)) return false;
    // zoom=1, tx=0, ty=0 通常是初始 fit 的默认状态，不算用户调整过，不恢复
    if (v.zoom === 1 && v.tx === 0 && v.ty === 0) return false;
    _mmGraph.zoomTo(v.zoom);
    _mmGraph.translate(v.tx, v.ty);
    var lbl = document.getElementById('mmZoomLabel');
    if (lbl) lbl.textContent = Math.round(v.zoom * 100) + '%';
    return true;
  } catch (_) { return false; }
}

// ── 全局连线样式 ──
function mmApplyRouter(key) {
  if (!MM_ROUTERS[key]) return;
  _mmRouterKey = key;
  localStorage.setItem('mm_router', key);
  // 同步到 _mmData，确保保存到 mindmap.json（跨设备/跨浏览器恢复）
  if (_mmData) _mmData.router = key;
  mmReRender();
  // 同步工具栏 select 显示
  var sel = document.getElementById('mmRouterSelect');
  if (sel) sel.value = key;
  mmMarkDirty();  // 触发保存，让全局选择持久化
}

// ── 全局布局算法 ──
function mmApplyLayout(key) {
  if (!MM_LAYOUTS[key]) return;
  _mmLayoutKey = key;
  localStorage.setItem('mm_layout', key);
  if (_mmData) _mmData.layout = key;
  // 同步工具栏 select 显示
  var sel = document.getElementById('mmLayoutSelect');
  if (sel) sel.value = key;
  // custom 布局不自动重排，保持当前位置；其他布局立即重排
  if (key !== 'custom' && _mmGraph) {
    mmAutoLayout();
  } else {
    mmMarkDirty();
  }
}
function mmReRender() {
  if (!_mmContainer || !_mmData) return;
  _mmData = mmCollectData() || _mmData;
  var prevZoom = _mmGraph ? _mmGraph.zoom() : 1;
  var prevTx = null, prevTy = null;
  if (_mmGraph) {
    try { var tr = _mmGraph.translate(); prevTx = tr.tx; prevTy = tr.ty; } catch (_) {}
    try { _mmGraph.dispose(); } catch (_) {}
    _mmGraph = null;
  }
  // 用 cloneNode 替换 container，彻底清除所有旧事件监听器（避免重建后监听器叠加导致 panning 失效）
  var oldContainer = _mmContainer;
  var newContainer = oldContainer.cloneNode(false);
  if (oldContainer.parentNode) oldContainer.parentNode.replaceChild(newContainer, oldContainer);
  _mmContainer = newContainer;
  // skipFit=true 跳过 doFit，手动恢复视图状态
  _mmGraph = buildMindMapX6(_mmContainer, _mmData, _mmEditable, true);
  // 恢复之前的缩放/平移状态
  if (_mmGraph && prevTx !== null) {
    try {
      _mmGraph.zoomTo(prevZoom);
      _mmGraph.translate(prevTx, prevTy);
    } catch (_) {}
  }
  // 清除移动端选中状态（旧 cell 已随 graph 销毁失效）
  if (typeof mmClearSelection === 'function') mmClearSelection();
}

// ── 自动布局分派器：根据 _mmLayoutKey 调用对应算法 ──
function mmAutoLayout() {
  if (!_mmGraph) return;
  var cfg = MM_LAYOUTS[_mmLayoutKey] || MM_LAYOUTS['tree-h'];
  if (cfg.algo === 'custom') return;  // 自定义布局不重排
  if (cfg.algo === 'tree') {
    _mmLayoutTree(cfg.dir);
  } else if (cfg.algo === 'dagre') {
    _mmLayoutDagre(cfg.dir);
  }
  mmZoomToFit();
  mmMarkDirty();
}

// ── 自实现思维导图专用树布局（支持变尺寸节点，绝不重叠）──
// 算法：递归计算子树所需尺寸 → 父节点居中于子树 → 每节点用真实尺寸分配空间
// dir: 'LR'=左→右(水平), 'TB'=上→下(垂直), 'RL'=右→左(水平反向)
function _mmLayoutTree(dir) {
  if (!dir) dir = 'LR';
  var cells = _mmGraph.getCells();
  var nodeCells = [], idMap = {};
  cells.forEach(function (c) {
    if (c.isNode && c.isNode()) {
      var sz = c.getSize();
      var d = c.getData() || {};
      var mmid = d.id || c.id;
      idMap[c.id] = mmid;
      nodeCells.push({ cell: c, id: mmid, w: sz.width, h: sz.height });
    }
  });
  if (nodeCells.length === 0) return;

  var edgeList = [];
  cells.forEach(function (c) {
    if (c.isEdge && c.isEdge()) {
      var s = c.getSourceCellId(), t = c.getTargetCellId();
      if (s && t && idMap[s] && idMap[t]) {
        edgeList.push({ s: idMap[s], t: idMap[t] });
      }
    }
  });

  var nodeMap = {};
  nodeCells.forEach(function (n) { nodeMap[n.id] = { id: n.id, children: [], w: n.w, h: n.h, cell: n.cell }; });
  var childSeen = {};
  edgeList.forEach(function (e) {
    if (nodeMap[e.s] && nodeMap[e.t] && !childSeen[e.t]) {
      nodeMap[e.s].children.push(nodeMap[e.t]);
      childSeen[e.t] = true;
    }
  });

  var hasParent = {};
  edgeList.forEach(function (e) { hasParent[e.t] = true; });
  var roots = nodeCells.filter(function (n) { return !hasParent[n.id]; }).map(function (n) { return n.id; });
  if (roots.length === 0 && nodeCells.length > 0) roots = [nodeCells[0].id];

  // 间距参数（水平用 HGAP/VGAP，垂直互换）— HGAP 加大给曼哈顿路由留绕路空间
  var HGAP = 120, VGAP = 16, ROOT_VGAP = 40;

  // 递归计算子树所需"厚度"（沿排列方向的尺寸）
  // 水平树(LR/RL)：厚度=高度(垂直方向)；垂直树(TB)：厚度=宽度(水平方向)
  var isHorizontal = (dir === 'LR' || dir === 'RL');
  var subSizeCache = {};
  function subtreeSize(id) {
    if (subSizeCache[id] != null) return subSizeCache[id];
    var node = nodeMap[id];
    // 节点沿排列方向的尺寸
    var mySize = isHorizontal ? node.h : node.w;
    if (!node.children || node.children.length === 0) {
      subSizeCache[id] = mySize;
      return mySize;
    }
    var total = 0;
    node.children.forEach(function (ch, i) {
      if (i > 0) total += VGAP;
      total += subtreeSize(ch.id);
    });
    var s = Math.max(mySize, total);
    subSizeCache[id] = s;
    return s;
  }

  // positions[id] = {x, y} 节点中心点坐标
  var positions = {};
  function place(id, depth, crossTop) {
    var node = nodeMap[id];
    var subS = subtreeSize(id);
    // 沿主方向（深度）的尺寸：水平树用宽，垂直树用高
    var mainSize = isHorizontal ? node.w : node.h;
    var mainPos = depth * (mainSize + HGAP);
    var crossCenter = crossTop + subS / 2;

    if (isHorizontal) {
      // LR: 主方向=x，交叉方向=y；RL: x 取负（镜像）
      positions[id] = dir === 'RL' ? { x: -mainPos, y: crossCenter } : { x: mainPos, y: crossCenter };
    } else {
      // TB: 主方向=y，交叉方向=x
      positions[id] = { x: crossCenter, y: mainPos };
    }

    if (!node.children || node.children.length === 0) return;

    var childrenTotalS = 0;
    node.children.forEach(function (ch, i) {
      if (i > 0) childrenTotalS += VGAP;
      childrenTotalS += subtreeSize(ch.id);
    });
    var childCursor = crossTop + (subS - childrenTotalS) / 2;

    node.children.forEach(function (ch, i) {
      if (i > 0) childCursor += VGAP;
      var chSubS = subtreeSize(ch.id);
      place(ch.id, depth + 1, childCursor);
      childCursor += chSubS;
    });
  }

  var cursor = 0;
  roots.forEach(function (r, i) {
    if (i > 0) cursor += ROOT_VGAP;
    var s = subtreeSize(r);
    place(r, 0, cursor);
    cursor += s;
  });

  // 写回 X6 节点位置（中心点 → 左上角）
  nodeCells.forEach(function (n) {
    var p = positions[n.id];
    if (p) n.cell.position(Math.round(p.x - n.w / 2), Math.round(p.y - n.h / 2));
  });
}

// ── dagre 布局（备用，适合非树形结构）──
function _mmLayoutDagre(dir) {
  if (typeof dagre === 'undefined' || !dagre.graphlib) { _mmLayoutTree(dir); return; }
  var cells = _mmGraph.getCells();
  var nodeCells = [], idMap = {};
  cells.forEach(function (c) {
    if (c.isNode && c.isNode()) {
      var sz = c.getSize();
      var d = c.getData() || {};
      var mmid = d.id || c.id;
      idMap[c.id] = mmid;
      nodeCells.push({ cell: c, id: mmid, w: sz.width, h: sz.height });
    }
  });
  if (nodeCells.length === 0) return;
  var edgeList = [];
  cells.forEach(function (c) {
    if (c.isEdge && c.isEdge()) {
      var s = c.getSourceCellId(), t = c.getTargetCellId();
      if (s && t && idMap[s] && idMap[t]) edgeList.push({ s: idMap[s], t: idMap[t] });
    }
  });
  var rankdir = (dir === 'TB') ? 'TB' : 'LR';
  var g = new dagre.graphlib.Graph();
  g.setGraph({ rankdir: rankdir, nodesep: 50, ranksep: 140, marginx: 40, marginy: 40 });
  g.setDefaultEdgeLabel(function () { return {}; });
  nodeCells.forEach(function (n) { g.setNode(n.id, { width: n.w, height: n.h }); });
  edgeList.forEach(function (e) { g.setEdge(e.s, e.t); });
  dagre.layout(g);
  nodeCells.forEach(function (n) {
    var dn = g.node(n.id);
    if (dn) n.cell.position(Math.round(dn.x - n.w / 2), Math.round(dn.y - n.h / 2));
  });
}

// ── 排列指定节点的后续枝干（只重排该节点子树，其他节点不动）──
// 思路：以选中节点为根，用 _mmLayoutTree 的算法重排其子树
//       根节点位置保持不变，子树相对于根节点重新布局
function mmLayoutSubtree(rootCell, silent) {
  if (!_mmGraph || !rootCell || !rootCell.isNode()) return;
  var rootPos = rootCell.getPosition();
  var rootSize = rootCell.getSize();
  var rootId = rootCell.id;

  // 收集子树所有节点（含根）
  var subtreeIds = [rootId];
  var seen = {};
  seen[rootId] = true;
  function dfs(cid) {
    var outEdges = _mmGraph.getOutgoingEdges(cid) || [];
    outEdges.forEach(function (e) {
      var tgt = e.getTargetCellId();
      if (tgt && !seen[tgt]) {
        seen[tgt] = true;
        subtreeIds.push(tgt);
        dfs(tgt);
      }
    });
  }
  dfs(rootId);

  if (subtreeIds.length <= 1) {
    // 没有子节点，无需排列
    if (!silent) mmToast('该节点没有后续枝干');
    return;
  }

  // 构建子树 nodeMap（复用 _mmLayoutTree 的数据结构）
  var nodeMap = {};
  subtreeIds.forEach(function (cid) {
    var c = _mmGraph.getCellById(cid);
    if (!c || !c.isNode()) return;
    var sz = c.getSize();
    var d = c.getData() || {};
    nodeMap[cid] = { id: cid, children: [], w: sz.width, h: sz.height, cell: c };
  });
  // 构建父子关系（只含子树内部边）
  var childSeen = {};
  subtreeIds.forEach(function (cid) {
    if (!nodeMap[cid]) return;
    var outEdges = _mmGraph.getOutgoingEdges(cid) || [];
    outEdges.forEach(function (e) {
      var tgt = e.getTargetCellId();
      if (tgt && nodeMap[tgt] && !childSeen[tgt]) {
        nodeMap[cid].children.push(nodeMap[tgt]);
        childSeen[tgt] = true;
      }
    });
  });

  // 间距参数（与 _mmLayoutTree 一致，水平树 LR）— HGAP 加大给曼哈顿路由留绕路空间
  var isHorizontal = true;  // 子树排列固定用水平树
  var HGAP = 120, VGAP = 16;

  // 递归计算子树所需"厚度"（沿交叉方向）
  var subSizeCache = {};
  function subtreeSize(id) {
    if (subSizeCache[id] != null) return subSizeCache[id];
    var node = nodeMap[id];
    var mySize = isHorizontal ? node.h : node.w;
    if (!node.children || node.children.length === 0) {
      subSizeCache[id] = mySize;
      return mySize;
    }
    var total = 0;
    node.children.forEach(function (ch, i) {
      if (i > 0) total += VGAP;
      total += subtreeSize(ch.id);
    });
    var s = Math.max(mySize, total);
    subSizeCache[id] = s;
    return s;
  }

  // 分配坐标（相对坐标，根节点为原点）
  // positions[id] = {x, y} 节点中心点相对坐标
  var positions = {};
  function place(id, depth, crossTop) {
    var node = nodeMap[id];
    var subS = subtreeSize(id);
    var mainSize = isHorizontal ? node.w : node.h;
    var mainPos = depth * (mainSize + HGAP);
    var crossCenter = crossTop + subS / 2;
    if (isHorizontal) {
      positions[id] = { x: mainPos, y: crossCenter };
    } else {
      positions[id] = { x: crossCenter, y: mainPos };
    }
    if (!node.children || node.children.length === 0) return;
    var childrenTotalS = 0;
    node.children.forEach(function (ch, i) {
      if (i > 0) childrenTotalS += VGAP;
      childrenTotalS += subtreeSize(ch.id);
    });
    var childCursor = crossTop + (subS - childrenTotalS) / 2;
    node.children.forEach(function (ch, i) {
      if (i > 0) childCursor += VGAP;
      var chSubS = subtreeSize(ch.id);
      place(ch.id, depth + 1, childCursor);
      childCursor += chSubS;
    });
  }

  // 根节点深度=0，crossTop=0（相对原点）
  place(rootId, 0, 0);

  // 根节点相对坐标应为 (0, 子树厚度/2)，但我们希望根节点保持原位
  // 计算根节点相对坐标的偏移，然后把所有节点平移到根节点原位
  var rootRel = positions[rootId];
  // 根节点中心点在原图的绝对位置
  var rootAbsCx = rootPos.x + rootSize.width / 2;
  var rootAbsCy = rootPos.y + rootSize.height / 2;
  // 偏移：让根节点相对坐标 (rootRel.x, rootRel.y) 对齐到 (rootAbsCx, rootAbsCy)
  var offX = rootAbsCx - rootRel.x;
  var offY = rootAbsCy - rootRel.y;

  // 写回位置（中心点 → 左上角）
  subtreeIds.forEach(function (cid) {
    if (!nodeMap[cid]) return;
    var p = positions[cid];
    if (!p) return;
    var node = nodeMap[cid];
    // 根节点保持原位（不改动）
    if (cid === rootId) return;
    var absCx = p.x + offX;
    var absCy = p.y + offY;
    node.cell.position(Math.round(absCx - node.w / 2), Math.round(absCy - node.h / 2));
  });

  mmMarkDirty();
  if (!silent) mmToast('已排列 ' + (subtreeIds.length - 1) + ' 个后续节点');
}

// 轻量提示（不阻塞，2秒后消失）
function mmToast(msg) {
  var t = document.getElementById('mmToast');
  if (!t) {
    t = document.createElement('div');
    t.id = 'mmToast';
    t.style.cssText = 'position:fixed;left:50%;bottom:80px;transform:translateX(-50%);background:var(--surface2);border:1px solid var(--border2);color:var(--fg);padding:8px 16px;border-radius:6px;z-index:10002;font-size:12px;box-shadow:0 4px 16px rgba(0,0,0,.4);transition:opacity .3s';
    document.body.appendChild(t);
  }
  t.textContent = msg;
  t.style.opacity = '1';
  t.style.display = 'block';
  if (t._timer) clearTimeout(t._timer);
  t._timer = setTimeout(function () { t.style.opacity = '0'; }, 2000);
}

// ── 工具栏添加节点按钮 ──
function mmAddNodeBtn() {
  if (!_mmGraph) return;
  var cw = _mmContainer.clientWidth, ch = _mmContainer.clientHeight;
  var cx = cw / 2, cy = ch / 2;
  try {
    var gp = _mmGraph.clientToGraph(cx, cy);
    mmAddNodeAt(gp.x, gp.y);
  } catch (_) {
    mmAddNodeAt(cx, cy);
  }
}

// ── 在指定位置添加节点 ──
function mmAddNodeAt(x, y) {
  if (!_mmGraph) return;
  var newId = 'm_' + Date.now() + '_' + Math.floor(Math.random() * 1000);
  var t = MM_TYPES.idea;
  var lines = mmWrap('新想法');
  var h = lines.length * MM_LINE_H + MM_PAD_Y * 2;
  var portVis = _mmEditable ? 'visible' : 'hidden';
  var cell = _mmGraph.addNode({
    id: newId, shape: 'rect', width: MM_NODE_W, height: h,
    x: x - MM_NODE_W / 2, y: y - h / 2,
    attrs: {
      body: { fill: t.fill, stroke: t.color, strokeWidth: t.stroke, rx: t.rx, ry: t.rx, strokeDasharray: t.dash },
      label: { text: lines.join('\n'), fill: t.color, fontSize: MM_FONT, textAnchor: 'middle', textVerticalAnchor: 'middle', lineHeight: MM_LINE_H }
    },
    ports: {
      groups: {
        top: { position: 'top', attrs: { circle: { r: 2.5, magnet: true, stroke: t.color, fill: '#fff', strokeWidth: 1.2, visibility: portVis } } },
        right: { position: 'right', attrs: { circle: { r: 2.5, magnet: true, stroke: t.color, fill: '#fff', strokeWidth: 1.2, visibility: portVis } } },
        bottom: { position: 'bottom', attrs: { circle: { r: 2.5, magnet: true, stroke: t.color, fill: '#fff', strokeWidth: 1.2, visibility: portVis } } },
        left: { position: 'left', attrs: { circle: { r: 2.5, magnet: true, stroke: t.color, fill: '#fff', strokeWidth: 1.2, visibility: portVis } } }
      },
      items: [{ id: 'pt', group: 'top' }, { id: 'pr', group: 'right' }, { id: 'pb', group: 'bottom' }, { id: 'pl', group: 'left' }]
    },
    data: { id: newId, type: 'idea', note: '', links: [] }
  });
  if (_mmEditable && cell) {
    mmShowEditor(cell);
  }
  mmMarkDirty();
  return cell;
}

// ── 节点编辑器 ──
function mmShowEditor(cell) {
  var old = document.getElementById('mmEditor');
  if (old) old.remove();
  var fb = document.getElementById('fileBrowser');
  if (!fb) return;

  var data = cell.getData() || {};
  var attrs = cell.getAttrs();
  var text = (attrs && attrs.label && attrs.label.text) ? attrs.label.text.replace(/\n/g, ' ') : '';
  var note = data.note || '';
  var type = data.type || 'idea';
  var nodeColor = data.color || null;
  var isMob = mmIsMobile();

  var div = document.createElement('div');
  div.id = 'mmEditor';
  // 移动端：底部全宽 sheet；PC 端：居中弹窗
  if (isMob) {
    div.style.cssText = 'position:absolute;left:0;right:0;bottom:0;background:var(--surface2);border:1px solid var(--border2);border-radius:12px 12px 0 0;padding:16px;z-index:10000;max-height:80vh;overflow-y:auto;box-shadow:0 -8px 32px rgba(0,0,0,.4)';
  } else {
    div.style.cssText = 'position:absolute;left:50%;top:50%;transform:translate(-50%,-50%);background:var(--surface2);border:1px solid var(--border2);border-radius:8px;padding:16px 20px;z-index:10000;min-width:380px;max-width:90vw;box-shadow:0 8px 32px rgba(0,0,0,.4)';
  }
  var typeOpts = Object.keys(MM_TYPES).map(function (k) {
    return '<option value="' + k + '"' + (k === type ? ' selected' : '') + '>' + MM_TYPES[k].name + '</option>';
  }).join('');
  // 颜色选择器：7 种预设色 + 默认色
  var colorSwatches = MM_EDGE_COLORS.concat(['#e8eaed']).map(function (c) {
    return '<span data-color="' + c + '" style="display:inline-block;width:22px;height:22px;border-radius:50%;background:' + c + ';border:2px solid ' + (nodeColor === c ? 'var(--accent)' : 'var(--border)') + ';cursor:pointer;margin:2px"></span>';
  }).join('');
  var defaultSwatch = '<span data-color="" style="display:inline-block;width:22px;height:22px;border-radius:50%;background:transparent;border:2px dashed var(--border);cursor:pointer;margin:2px;font-size:10px;line-height:20px;text-align:center;color:var(--tdim)">默认</span>';
  div.innerHTML =
    '<div style="font-size:13px;color:var(--fg);margin-bottom:8px">编辑节点</div>' +
    '<label style="font-size:11px;color:var(--tdim)">标题</label>' +
    '<textarea id="mmEditorText" rows="3" style="width:100%;background:var(--bg);color:var(--fg);border:1px solid var(--border);border-radius:4px;padding:8px;font-size:13px;font-family:inherit;resize:vertical;box-sizing:border-box;margin-bottom:8px">' + esc(text) + '</textarea>' +
    '<label style="font-size:11px;color:var(--tdim)">备注</label>' +
    '<textarea id="mmEditorNote" rows="2" style="width:100%;background:var(--bg);color:var(--fg);border:1px solid var(--border);border-radius:4px;padding:8px;font-size:12px;font-family:inherit;resize:vertical;box-sizing:border-box;margin-bottom:8px">' + esc(note) + '</textarea>' +
    '<label style="font-size:11px;color:var(--tdim)">类型</label>' +
    '<select id="mmEditorType" style="width:100%;background:var(--bg);color:var(--fg);border:1px solid var(--border);border-radius:4px;padding:4px;font-size:12px;box-sizing:border-box;margin-bottom:8px">' + typeOpts + '</select>' +
    '<label style="font-size:11px;color:var(--tdim)">节点颜色</label>' +
    '<div id="mmEditorColors" style="margin-bottom:12px">' + colorSwatches + defaultSwatch + '</div>' +
    '<div style="display:flex;justify-content:space-between;gap:8px">' +
    '<button id="mmEditorDel" style="padding:6px 14px;background:transparent;color:var(--danger);border:1px solid var(--danger);border-radius:4px;cursor:pointer;font-size:12px">删除</button>' +
    '<div style="display:flex;gap:8px">' +
    '<button id="mmEditorCancel" style="padding:6px 14px;background:transparent;color:var(--dim);border:1px solid var(--border);border-radius:4px;cursor:pointer;font-size:12px">取消</button>' +
    '<button id="mmEditorOk" style="padding:6px 14px;background:var(--accent);color:#fff;border:none;border-radius:4px;cursor:pointer;font-size:12px">确定</button>' +
    '</div></div>';
  fb.appendChild(div);

  // 颜色选择器交互
  var swatches = div.querySelectorAll('#mmEditorColors span[data-color]');
  var selectedColor = nodeColor;
  swatches.forEach(function (sw) {
    sw.onclick = function () {
      selectedColor = sw.getAttribute('data-color') || null;
      swatches.forEach(function (s2) { s2.style.border = '2px solid var(--border)'; });
      sw.style.border = '2px solid var(--accent)';
    };
  });

  var ta = document.getElementById('mmEditorText');
  // 移动端不自动 focus 避免软键盘立即弹出挡住视线
  if (!isMob) { ta.focus(); ta.select(); }

  var close = function () { div.remove(); };
  var apply = function () {
    var newText = ta.value.trim() || '未命名';
    var newNote = document.getElementById('mmEditorNote').value;
    var newType = document.getElementById('mmEditorType').value;
    var t = MM_TYPES[newType] || MM_TYPES.idea;
    var lines = mmWrap(newText);
    var h = lines.length * MM_LINE_H + MM_PAD_Y * 2;
    cell.attr('label/text', lines.join('\n'));
    // 字色 + 边框色：用户选了 color 则覆盖类型色，否则用类型色（与右键菜单 mmChangeNodeColor 一致）
    cell.attr('label/fill', selectedColor || t.color);
    cell.attr('body/stroke', selectedColor || t.color);
    cell.attr('body/strokeWidth', t.stroke);
    cell.attr('body/fill', t.fill);
    cell.attr('body/strokeDasharray', t.dash);
    cell.resize(MM_NODE_W, h);
    cell.setData(Object.assign({}, cell.getData(), { text: newText, note: newNote, type: newType, color: selectedColor }));
    mmMarkDirty();
    close();
  };

  document.getElementById('mmEditorCancel').onclick = close;
  document.getElementById('mmEditorOk').onclick = apply;
  document.getElementById('mmEditorDel').onclick = function () {
    cell.remove();
    mmMarkDirty();
    close();
  };
  ta.onkeydown = function (e) {
    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); apply(); }
    else if (e.key === 'Escape') { e.preventDefault(); close(); }
  };
}

// ── PC 端编辑模式：自定义框选（rubberband）──
// 在 container 上 capture 阶段监听 mousedown，编辑模式下空白处左键启动框选
// Shift+左键留给画布平移，节点/port 的 mousedown 不拦截（X6 先处理）
var _mmRubber = null;
function _mmInitRubberband(graph, container) {
  if (!container) return;
  // 标记是否已经绑定，避免重复
  if (container._mmRubberBound) return;
  container._mmRubberBound = true;

  container.addEventListener('mousedown', function (e) {
    if (!_mmEditable) return;            // 非编辑模式不框选
    if (e.button !== 0) return;          // 只响应左键
    if (e.shiftKey) return;              // Shift+左键留给画布平移
    if (mmIsMobile()) return;            // 移动端不用鼠标框选

    // 判断是否点击在空白处（不是节点/port/边）
    var target = e.target;
    var isBlank = false;
    // X6 v1 的 SVG 结构：svg > graph-background(rect) / graph-svg-content(g) > nodes/edges
    if (target.tagName === 'svg' ||
        target.tagName === 'rect' ||
        target.tagName === 'g' ||
        target.tagName === 'path') {
      // 检查是否是节点或 port 或边
      var nodeEl = target.closest ? target.closest('.x6-node') : null;
      var edgeEl = target.closest ? target.closest('.x6-edge') : null;
      var portEl = target.classList && target.classList.contains('x6-port') ? target : null;
      if (!nodeEl && !edgeEl && !portEl) {
        isBlank = true;
      }
    }
    if (!isBlank) return;

    // 空白处左键：启动框选，阻止 panning
    e.stopPropagation();
    e.preventDefault();

    var rect = container.getBoundingClientRect();
    var sx = e.clientX - rect.left;
    var sy = e.clientY - rect.top;

    _mmRubber = {
      startX: sx, startY: sy,
      startClientX: e.clientX,   // 保存客户端坐标，用于精确相交计算
      startClientY: e.clientY,
      el: document.createElement('div')
    };
    _mmRubber.el.style.cssText = 'position:absolute;left:' + sx + 'px;top:' + sy + 'px;width:0;height:0;border:1px solid #4aa3ff;background:rgba(74,163,255,.12);pointer-events:none;z-index:9999';
    container.style.position = 'relative';
    container.appendChild(_mmRubber.el);

    function onMove(ev) {
      if (!_mmRubber) return;
      var cx = ev.clientX - rect.left;
      var cy = ev.clientY - rect.top;
      var rx = Math.min(_mmRubber.startX, cx);
      var ry = Math.min(_mmRubber.startY, cy);
      var rw = Math.abs(cx - _mmRubber.startX);
      var rh = Math.abs(cy - _mmRubber.startY);
      _mmRubber.el.style.left = rx + 'px';
      _mmRubber.el.style.top = ry + 'px';
      _mmRubber.el.style.width = rw + 'px';
      _mmRubber.el.style.height = rh + 'px';
    }

    function onUp(ev) {
      document.removeEventListener('mousemove', onMove);
      document.removeEventListener('mouseup', onUp);
      if (!_mmRubber) return;

      // 选框在客户端坐标系下的范围（直接用鼠标坐标，不经过 clientToGraph 转换，避免漂移）
      var selLeft = Math.min(_mmRubber.startClientX, ev.clientX);
      var selTop = Math.min(_mmRubber.startClientY, ev.clientY);
      var selRight = Math.max(_mmRubber.startClientX, ev.clientX);
      var selBottom = Math.max(_mmRubber.startClientY, ev.clientY);
      var selW = selRight - selLeft;
      var selH = selBottom - selTop;

      // 选框太小（单击而非拖动）：清除选择
      if (selW < 5 && selH < 5) {
        try { graph.cleanSelection(); } catch (_) {}
      } else {
        // 选中与选框相交的节点：把节点 bbox 用 localToClient 转到客户端坐标系再比较
        // 这样完全规避 clientToGraph 的精度问题，所见即所选
        var toSelect = [];
        graph.getNodes().forEach(function (node) {
          var nb = node.getBBox();  // graph 坐标系
          var tl = graph.localToClient(nb.x, nb.y);
          var br = graph.localToClient(nb.x + nb.width, nb.y + nb.height);
          // 客户端坐标系下检查矩形相交
          if (selLeft <= br.x && selRight >= tl.x &&
              selTop <= br.y && selBottom >= tl.y) {
            toSelect.push(node);
          }
        });
        if (toSelect.length > 0) {
          if (!ev.shiftKey && !ev.ctrlKey) {
            try { graph.cleanSelection(); } catch (_) {}
          }
          toSelect.forEach(function (n) {
            try { graph.select(n); } catch (_) {}
          });
        }
      }

      if (_mmRubber.el && _mmRubber.el.parentNode) _mmRubber.el.remove();
      _mmRubber = null;
    }

    document.addEventListener('mousemove', onMove);
    document.addEventListener('mouseup', onUp);
  }, true);  // capture 阶段，先于 X6 的 panning 处理
}

// ── PC 端：右键拖动平移画布（配合 panning.eventTypes 的 rightMouseDown）──
// 作用：右键拖动 → 平移画布；右键点击（不拖动）→ 正常显示 contextmenu 菜单
// 实现：记录右键 mousedown 位置，在 contextmenu 事件 capture 阶段检测位移
//       位移超过阈值 → 说明拖动了，阻止 contextmenu（不弹菜单）
function _mmInitRightPan(graph, container) {
  if (!container) return;
  if (container._mmRightPanBound) return;
  container._mmRightPanBound = true;

  var downX = 0, downY = 0;
  // 记录右键按下位置
  container.addEventListener('mousedown', function (e) {
    if (e.button !== 2) return;  // 只关注右键
    downX = e.clientX;
    downY = e.clientY;
  }, true);

  // contextmenu 事件在 mouseup 后触发；检测位移判断是否拖动过
  // capture + stopImmediatePropagation 确保先于 X6 的 contextmenu 监听器
  container.addEventListener('contextmenu', function (e) {
    var dx = Math.abs(e.clientX - downX);
    var dy = Math.abs(e.clientY - downY);
    if (dx > 3 || dy > 3) {
      // 右键拖动平移过，阻止菜单弹出
      e.preventDefault();
      e.stopImmediatePropagation();
    }
  }, true);
}

// ── 批量操作菜单（多选时右键）──
function mmShowBatchMenu(e, cells) {
  mmHideMenu();
  var x = e.clientX, y = e.clientY;
  var nodeCount = cells.length;

  var items = [
    { label: '已选中 ' + nodeCount + ' 个节点', sep: true },
    { label: '批量删除', action: function () {
      mmHideMenu();
      if (!confirm('确定删除选中的 ' + nodeCount + ' 个节点及其连线？')) return;
      cells.forEach(function (c) { try { c.remove(); } catch (_) {} });
      try { _mmGraph.cleanSelection(); } catch (_) {}
      mmMarkDirty();
      mmToast('已删除 ' + nodeCount + ' 个节点');
    } },
    { label: '改类型 ▸', sub: true, subItems: Object.keys(MM_TYPES).map(function (k) {
      return { label: MM_TYPES[k].name, action: function () {
        mmHideMenu();
        cells.forEach(function (c) { mmChangeNodeType(c, k); });
        mmToast('已批量改为「' + MM_TYPES[k].name + '」');
      } };
    }) },
    { label: '改颜色 ▸', sub: true, subItems: MM_EDGE_COLORS.concat([null]).map(function (c) {
      return { label: c === null ? '默认色' : '● ' + c, colorName: c === null ? '默认色' : mmColorName(c), action: function () {
        mmHideMenu();
        cells.forEach(function (cell) { mmChangeNodeColor(cell, c); });
        mmToast(c ? '已批量设置颜色' : '已恢复默认色');
      } };
    }) },
    { label: '排列后续枝干', action: function () {
      mmHideMenu();
      var count = 0;
      cells.forEach(function (c) { try { mmLayoutSubtree(c, true); count++; } catch (_) {} });
      if (count > 0) mmToast('已排列 ' + count + ' 处后续枝干');
      mmMarkDirty();
    } },
    { label: '', sep: true },
    { label: '取消选择', action: function () {
      mmHideMenu();
      try { _mmGraph.cleanSelection(); } catch (_) {}
    } }
  ];

  var menu = document.createElement('div');
  menu.id = 'mmMenu';
  menu.style.cssText = 'position:fixed;left:' + x + 'px;top:' + y + 'px;background:var(--surface2);border:1px solid var(--border2);border-radius:6px;padding:4px 0;z-index:10001;min-width:180px;box-shadow:0 4px 16px rgba(0,0,0,.4);font-size:12px';

  items.forEach(function (it) {
    if (it.sep) {
      if (it.label) {
        // 有 label 的 sep：当作小标题显示
        var s = document.createElement('div');
        s.textContent = it.label;
        s.style.cssText = 'padding:4px 14px;color:var(--tdim);font-size:11px;border-bottom:1px solid var(--border);margin-bottom:2px';
        menu.appendChild(s);
      } else {
        // 无 label 的 sep：纯分隔线
        var line = document.createElement('div');
        line.style.cssText = 'height:1px;background:var(--border);margin:4px 0';
        menu.appendChild(line);
      }
    } else if (it.sub) {
      mmAddSubmenuItem(menu, it);
    } else {
      var el = document.createElement('div');
      el.textContent = it.label;
      el.style.cssText = 'padding:6px 14px;cursor:pointer;color:var(--fg);transition:background .12s';
      el.onmouseenter = function () { el.style.background = 'var(--hover)'; };
      el.onmouseleave = function () { el.style.background = ''; };
      el.onclick = it.action;
      menu.appendChild(el);
    }
  });

  document.body.appendChild(menu);
  var r = menu.getBoundingClientRect();
  if (r.right > window.innerWidth) menu.style.left = (window.innerWidth - r.width - 8) + 'px';
  if (r.bottom > window.innerHeight) menu.style.top = (window.innerHeight - r.height - 8) + 'px';
}

// ── 右键/长按菜单 ──
function mmShowMenu(e, cell) {
  mmHideMenu();
  var x = e.clientX, y = e.clientY;
  var menu = document.createElement('div');
  menu.id = 'mmMenu';
  menu.style.cssText = 'position:fixed;left:' + x + 'px;top:' + y + 'px;background:var(--surface2);border:1px solid var(--border2);border-radius:6px;padding:4px 0;z-index:10001;min-width:150px;box-shadow:0 4px 16px rgba(0,0,0,.4);font-size:12px';
  var d = cell.getData() || {};
  var items = [
    { label: '编辑', action: function () { mmShowEditor(cell); } },
    { label: '添加子节点', action: function () { mmHideMenu(); mmAddChildNode(cell); } },
    { label: '排列后续枝干', action: function () { mmHideMenu(); mmLayoutSubtree(cell); } },
    { label: '改类型 ▸', sub: true, subItems: Object.keys(MM_TYPES).map(function (k) {
      return { label: ((d.type || 'idea') === k ? '✓ ' : '') + MM_TYPES[k].name, action: function () { mmHideMenu(); mmChangeNodeType(cell, k); } };
    }) },
    { label: '改颜色 ▸', sub: true, subItems: MM_EDGE_COLORS.concat(['#e8eaed', null]).map(function (c) {
      return { label: (d.color === c ? '✓ ' : '') + (c === null ? '默认色' : '● ' + c), colorName: c === null ? '默认色' : mmColorName(c), action: function () { mmHideMenu(); mmChangeNodeColor(cell, c); } };
    }) },
    { label: '折叠/展开子节点', action: function () { mmHideMenu(); mmToggleCollapse(cell); } },
    { label: '', sep: true },
    { label: '让大眼展开', action: function () { mmHideMenu(); mmAskMainChat('expand', cell); } },
    { label: '让大眼分析', action: function () { mmHideMenu(); mmAskMainChat('analyze', cell); } },
    { label: '让大眼找关联', action: function () { mmHideMenu(); mmAskMainChat('relate', cell); } },
    { label: '', sep: true },
    { label: '复制 ID', action: function () { var dd = cell.getData() || {}; try { navigator.clipboard.writeText(dd.id || cell.id); } catch (_) {} mmHideMenu(); } },
    { label: '删除', action: function () { cell.remove(); mmMarkDirty(); mmHideMenu(); } }
  ];
  if (d.type === 'task_ref' && d.links && d.links.length) {
    var dagLink = null;
    for (var i = 0; i < d.links.length; i++) { if (d.links[i].kind === 'dag') { dagLink = d.links[i]; break; } }
    if (dagLink) {
      items.unshift({ label: '打开流程图', action: function () { mmHideMenu(); openFlowChart(); } });
    }
  }
  if (mmIsMobile()) {
    // 移动端：底部 sheet 样式，子菜单点击展开
    mmRenderMenuSheet(menu, items, '节点操作');
    return;
  }
  // PC 端：跟随触摸点/鼠标点，子菜单 hover 展开
  items.forEach(function (it) {
    if (it.sep) {
      var s = document.createElement('div');
      s.style.cssText = 'height:1px;background:var(--border);margin:4px 0';
      menu.appendChild(s);
    } else if (it.sub) {
      mmAddSubmenuItem(menu, it);
    } else {
      var el = document.createElement('div');
      el.textContent = it.label;
      el.style.cssText = 'padding:6px 14px;cursor:pointer;color:var(--fg);transition:background .12s';
      el.onmouseenter = function () { el.style.background = 'var(--hover)'; };
      el.onmouseleave = function () { el.style.background = ''; };
      el.onclick = it.action;
      menu.appendChild(el);
    }
  });

  document.body.appendChild(menu);
  var r = menu.getBoundingClientRect();
  if (r.right > window.innerWidth) menu.style.left = (window.innerWidth - r.width - 8) + 'px';
  if (r.bottom > window.innerHeight) menu.style.top = (window.innerHeight - r.height - 8) + 'px';
}

function mmShowBlankMenu(e, gx, gy) {
  mmHideMenu();
  var items = [
    { label: '添加节点', action: function () { mmHideMenu(); mmAddNodeAt(gx, gy); } }
  ];
  if (mmIsMobile()) {
    mmRenderMenuSheet(null, items, '画布操作');
    return;
  }
  var x = e.clientX, y = e.clientY;
  var menu = document.createElement('div');
  menu.id = 'mmMenu';
  menu.style.cssText = 'position:fixed;left:' + x + 'px;top:' + y + 'px;background:var(--surface2);border:1px solid var(--border2);border-radius:6px;padding:4px 0;z-index:10001;min-width:150px;box-shadow:0 4px 16px rgba(0,0,0,.4);font-size:12px';
  var el = document.createElement('div');
  el.textContent = '添加节点';
  el.style.cssText = 'padding:6px 14px;cursor:pointer;color:var(--fg);transition:background .12s';
  el.onmouseenter = function () { el.style.background = 'var(--hover)'; };
  el.onmouseleave = function () { el.style.background = ''; };
  el.onclick = function () { mmHideMenu(); mmAddNodeAt(gx, gy); };
  menu.appendChild(el);
  document.body.appendChild(menu);
}

function mmHideMenu() {
  var m = document.getElementById('mmMenu');
  if (m) m.remove();
}

// ── 边菜单：改 router / 颜色 / 虚线 / 标签 / 删除 ──
function mmShowEdgeMenu(e, cell) {
  mmHideMenu();
  var x = (e && e.clientX) || 0, y = (e && e.clientY) || 0;
  if (x === 0 && y === 0 && cell) {
    // 用边中点兜底
    try {
      var bbox = cell.getBBox();
      x = bbox.x + bbox.width / 2; y = bbox.y + bbox.height / 2;
    } catch (_) {}
  }
  var edata = cell.getData() || {};
  var curRouter = edata.router || _mmRouterKey;
  var curDash = edata.dash || (edata.style === 'dashed' ? 'dashed' : 'solid');
  var curColor = edata.color || '';
  var curBidir = !!edata.bidir;

  // 连线样式子菜单
  var routerItems = Object.keys(MM_ROUTERS).map(function (k) {
    return { label: (k === curRouter ? '✓ ' : '') + MM_ROUTERS[k].name, active: k === curRouter, action: function () {
      var d = cell.getData() || {};
      d.router = k; cell.setData(d);
      _mmSyncEdgeField(cell, { router: k });  // 直接同步到 _mmData，确保保存
      // 增量更新单条边（router 变化时 _mmSyncEdgeCell 会重建该边），不重建整个 graph
      _mmSyncEdgeCell(cell, { id: d.id, source: cell.getSourceCellId(), target: cell.getTargetCellId(), label: d.label, style: d.style, router: k, color: d.color, dash: d.dash });
      mmMarkDirty();
    } };
  });
  // "跟随全局" 选项
  routerItems.unshift({ label: (!edata.router ? '✓ ' : '') + '跟随全局', active: !edata.router, action: function () {
    var d = cell.getData() || {};
    d.router = null; cell.setData(d);
    _mmSyncEdgeField(cell, { router: null });
    // router 从有变 null（跟随全局），需要重建边应用全局 router
    _mmSyncEdgeCell(cell, { id: d.id, source: cell.getSourceCellId(), target: cell.getTargetCellId(), label: d.label, style: d.style, router: null, color: d.color, dash: d.dash });
    mmMarkDirty();
  } });

  // 颜色子菜单
  var colorItems = MM_EDGE_COLORS.map(function (c) {
    return { label: (c === curColor ? '✓ ' : '') + '● ' + c, colorName: mmColorName(c), active: c === curColor, action: function () {
      var d = cell.getData() || {};
      d.color = c; cell.setData(d);
      _mmSyncEdgeField(cell, { color: c });
      _mmSyncEdgeCell(cell, { id: d.id, source: cell.getSourceCellId(), target: cell.getTargetCellId(), label: d.label, style: d.style, router: d.router, color: c, dash: d.dash });
      mmMarkDirty();
    } };
  });
  colorItems.push({ label: (!curColor ? '✓ ' : '') + '默认色', active: !curColor, action: function () {
    var d = cell.getData() || {};
    d.color = null; cell.setData(d);
    _mmSyncEdgeField(cell, { color: null });
    _mmSyncEdgeCell(cell, { id: d.id, source: cell.getSourceCellId(), target: cell.getTargetCellId(), label: d.label, style: d.style, router: d.router, color: null, dash: d.dash });
    mmMarkDirty();
  } });

  // 虚线/实线
  var lineStyleItems = [
    { label: (curDash === 'solid' ? '✓ ' : '') + '实线', active: curDash === 'solid', action: function () {
      var d = cell.getData() || {}; d.dash = 'solid'; d.style = 'solid'; cell.setData(d);
      _mmSyncEdgeField(cell, { dash: 'solid', style: 'solid' });
      _mmSyncEdgeCell(cell, { id: d.id, source: cell.getSourceCellId(), target: cell.getTargetCellId(), label: d.label, style: 'solid', router: d.router, color: d.color, dash: 'solid' });
      mmMarkDirty();
    } },
    { label: (curDash === 'dashed' ? '✓ ' : '') + '虚线', active: curDash === 'dashed', action: function () {
      var d = cell.getData() || {}; d.dash = 'dashed'; d.style = 'dashed'; cell.setData(d);
      _mmSyncEdgeField(cell, { dash: 'dashed', style: 'dashed' });
      _mmSyncEdgeCell(cell, { id: d.id, source: cell.getSourceCellId(), target: cell.getTargetCellId(), label: d.label, style: 'dashed', router: d.router, color: d.color, dash: 'dashed' });
      mmMarkDirty();
    } }
  ];

  // 统一的 items 数组（数据驱动，PC/移动端共用）
  var items = [
    { label: '连线样式 ▸', sub: true, subItems: routerItems },
    { label: '颜色 ▸', sub: true, subItems: colorItems },
    { label: '线型 ▸', sub: true, subItems: lineStyleItems },
    { label: (curBidir ? '✓ ' : '') + '双向箭头', action: function () {
      var d = cell.getData() || {};
      var nb = !curBidir;
      d.bidir = nb; cell.setData(d);
      _mmSyncEdgeField(cell, { bidir: nb });
      _mmSyncEdgeCell(cell, { id: d.id, source: cell.getSourceCellId(), target: cell.getTargetCellId(), label: d.label, style: d.style, router: d.router, color: d.color, dash: d.dash, bidir: nb });
      mmMarkDirty();
    } },
    { label: '', sep: true },
    { label: '编辑标签', action: function () {
      mmShowLabelEditor(cell, edata.label || '', function (nl) {
        var d = cell.getData() || {};
        d.label = nl; cell.setData(d);
        _mmSyncEdgeField(cell, { label: nl });
        // 直接更新 label 渲染（文字色跟随当前线颜色）
        if (nl) {
          try {
            cell.setLabels([{ position: 0.5, markup: [{ tagName: 'rect', selector: 'bg' }, { tagName: 'text', selector: 'label' }], attrs: { label: { text: nl, fontSize: 12, fill: '#000', fontWeight: 600, textAnchor: 'middle', textVerticalAnchor: 'middle' }, bg: { ref: 'label', refX: -3, refY: -2, refWidth: 6, refHeight: 4, fill: d.color || '#e8eaed', stroke: 'none' } } }]);
          } catch (_) {}
        } else {
          try { cell.setLabels([]); } catch (_) {}
        }
        mmMarkDirty();
      });
    } },
    { label: '删除连线', action: function () {
      cell.remove();
      mmMarkDirty();
    } }
  ];

  // 移动端：底部 sheet
  if (mmIsMobile()) {
    mmRenderMenuSheet(null, items, '边操作');
    return;
  }

  // PC 端：跟随鼠标点，子菜单 hover 展开
  var menu = document.createElement('div');
  menu.id = 'mmMenu';
  menu.style.cssText = 'position:fixed;left:' + x + 'px;top:' + y + 'px;background:var(--surface2);border:1px solid var(--border2);border-radius:6px;padding:4px 0;z-index:10001;min-width:170px;box-shadow:0 4px 16px rgba(0,0,0,.4);font-size:12px';

  items.forEach(function (it) {
    if (it.sep) {
      var s = document.createElement('div');
      s.style.cssText = 'height:1px;background:var(--border);margin:4px 0';
      menu.appendChild(s);
    } else if (it.sub) {
      mmAddSubmenuItem(menu, it);
    } else {
      var el = document.createElement('div');
      el.textContent = it.label;
      el.style.cssText = 'padding:6px 14px;cursor:pointer;color:var(--fg);transition:background .12s';
      el.onmouseenter = function () { el.style.background = 'var(--hover)'; };
      el.onmouseleave = function () { el.style.background = ''; };
      el.onclick = function () { mmHideMenu(); it.action(); };
      menu.appendChild(el);
    }
  });

  document.body.appendChild(menu);
  var r = menu.getBoundingClientRect();
  if (r.right > window.innerWidth) menu.style.left = (window.innerWidth - r.width - 8) + 'px';
  if (r.bottom > window.innerHeight) menu.style.top = (window.innerHeight - r.height - 8) + 'px';
}

// ── 节点级 AI 操作：接入主聊天大眼（不建独立 AI 面板）──
// 思维导图与大眼协同走"双通道"：读通道（_handle_chat 注入 mindmap 摘要）+ 写通道（mindmap 工具集）
// 节点右键的 AI 项只做一件事：把指令填充到主聊天输入框并发送，让大眼用工具操作思维导图
function mmAskMainChat(action, cell) {
  if (!currentTopic) { alert('请先选择话题'); return; }
  var d = (cell && cell.getData()) || {};
  var text = d.text || '';
  var nodeId = d.id || (cell ? cell.id : '');
  var prompt = '';
  if (action === 'expand') {
    prompt = '请在思维导图里展开节点「' + text + '」（id: ' + nodeId + '），给出 3-6 个相关的子概念，并用 add_mindmap_node 工具把它们加到思维导图里，再用 add_mindmap_edge 连线。';
  } else if (action === 'analyze') {
    prompt = '请分析思维导图里节点「' + text + '」（id: ' + nodeId + '）在整张图中的角色和与其它节点的逻辑关系，给出洞察。';
  } else if (action === 'relate') {
    prompt = '请找出思维导图里节点「' + text + '」（id: ' + nodeId + '）与其它节点可能存在的关联，用 add_mindmap_edge 工具把建议的连线加到思维导图里。';
  } else {
    return;
  }
  // 填充主聊天输入框并发送
  if (typeof msgInput === 'undefined' || typeof doSend !== 'function') {
    alert('主聊天面板未就绪');
    return;
  }
  msgInput.value = prompt;
  msgInput.focus();
  doSend();
}

// ── 核心：构建 X6 图 ──
function buildMindMapX6(container, mmdata, editable, skipFit) {
  if (typeof X6 === 'undefined') return null;
  editable = !!editable;
  skipFit = !!skipFit;

  var rawNodes = (mmdata && mmdata.nodes) || [];
  var rawEdges = (mmdata && mmdata.edges) || [];

  container.style.width = '100%';
  // 保持 flex 自适应，不强制覆盖 height（避免拖拽分界线时容器无法缩小）
  if (!container.style.flex) {
    container.style.flex = '1 1 0%';
  }
  container.style.minHeight = '0';

  var cw = container.clientWidth, ch = container.clientHeight;
  if (cw < 200) cw = container.parentElement ? container.parentElement.clientWidth : window.innerWidth - 60;
  if (ch < 200) ch = container.parentElement ? container.parentElement.clientHeight : window.innerHeight - 200;

  var graph;
  try {
    // panning 用 leftMouseDown + rightMouseDown + mouseWheel
    // 右键拖动平移画布，右键点击（不拖动）触发 contextmenu 菜单（由 _mmInitRightPan 区分）
    // 编辑模式下空白处左键被框选拦截，Shift+左键平移，节点上左键拖动移动节点
    graph = new X6.Graph({
      container: container,
      width: Math.max(cw, 100), height: Math.max(ch, 100),
      panning: { enabled: true, eventTypes: ['leftMouseDown', 'rightMouseDown', 'mouseWheel'] },
      scaling: { min: 0.2, max: 5 },
      // interacting 始终为对象（不传 false），通过 nodeMovable/magnetConnectable 控制编辑状态
      // 这样切换编辑模式时只需修改 graph.options.interacting，不重建 graph
      interacting: {
        nodeMovable: editable,
        edgeMovable: false,
        nodeResizable: false,
        edgeLabelMovable: false,
        magnetConnectable: editable,
        stopDelegateOnDragging: true
      },
      // connecting 始终设置（不管是否编辑模式），通过 interacting.magnetConnectable 控制是否可连线
      connecting: {
        allowNode: true, allowPort: true, allowBlank: false, allowMulti: false, allowLoop: false,
        connectionPoint: 'boundary',
        anchor: 'center',
        connector: (MM_ROUTERS[_mmRouterKey] || MM_ROUTERS.rounded).connector,
        router: { name: 'manhattan', args: { padding: 12 } },
        createEdge: function () {
          var rCfg = MM_ROUTERS[_mmRouterKey] || MM_ROUTERS.rounded;
          var spec = {
            shape: 'edge',
            attrs: { line: { stroke: 'rgba(255,255,255,.55)', strokeWidth: 1.5, targetMarker: { name: 'block', size: 6 } } },
            data: { label: '', style: 'solid', router: null, color: null, dash: 'solid' }
          };
          if (rCfg.router) spec.router = rCfg.router;
          if (rCfg.connector) spec.connector = rCfg.connector;
          return graph.createEdge(spec);
        },
        validateConnection: function (args) {
          var src = args.sourceCell, tgt = args.targetCell;
          if (!src || !tgt) return false;
          if (src.id === tgt.id) return false;
          return true;
        }
      },
      // highlighting 始终设置（不管是否编辑模式）
      highlighting: {
        magnetAvailable: { name: 'stroke', args: { padding: 4, attrs: { 'stroke-width': 3, stroke: '#4aa3ff' } } },
        magnetAdsorbed: { name: 'stroke', args: { padding: 4, attrs: { 'stroke-width': 3, stroke: '#3ddc84' } } }
      },
      // 选择模块：始终启用（PC 端编辑模式下用于多选 + 批量操作）
      selecting: {
        enabled: true,
        multiple: true,
        showNodeSelectionBox: true,
        showEdgeSelectionBox: false,
        pointerEvents: 'auto'
      },
      grid: false,
      background: { color: 'transparent' }
    });
  } catch (e) {
    return null;
  }

  try {
    rawNodes.forEach(function (rn) {
      var t = MM_TYPES[rn.type] || MM_TYPES.idea;
      var text = String(rn.text || '未命名').replace(/[\r\n\t]+/g, ' ');
      var lines = mmWrap(text);
      var h = lines.length * MM_LINE_H + MM_PAD_Y * 2;
      var nodeSpec = {
        id: rn.id, shape: 'rect', width: MM_NODE_W, height: h,
        x: rn.x || 0, y: rn.y || 0,
        attrs: {
          body: { fill: rn.color || t.fill, stroke: t.color, strokeWidth: t.stroke, rx: t.rx, ry: t.rx, strokeDasharray: t.dash },
          label: { text: lines.join('\n'), fill: t.color, fontSize: MM_FONT, textAnchor: 'middle', textVerticalAnchor: 'middle', lineHeight: MM_LINE_H }
        },
        data: { id: rn.id, type: rn.type || 'idea', note: rn.note || '', color: rn.color || null, collapsed: rn.collapsed || false, links: rn.links || [] }
      };
      if ((rn.type || 'idea') === 'task_ref') {
        nodeSpec.attrs.body.strokeWidth = 2;
      }
      // 始终添加 ports（不管是否编辑模式），通过 visibility 控制显示
      // 这样切换编辑模式时不重建 graph，只需动态修改 port visibility
      var portVis = editable ? 'visible' : 'hidden';
      nodeSpec.ports = {
        groups: {
          top: { position: 'top', attrs: { circle: { r: 2.5, magnet: true, stroke: t.color, fill: '#fff', strokeWidth: 1.2, visibility: portVis } } },
          right: { position: 'right', attrs: { circle: { r: 2.5, magnet: true, stroke: t.color, fill: '#fff', strokeWidth: 1.2, visibility: portVis } } },
          bottom: { position: 'bottom', attrs: { circle: { r: 2.5, magnet: true, stroke: t.color, fill: '#fff', strokeWidth: 1.2, visibility: portVis } } },
          left: { position: 'left', attrs: { circle: { r: 2.5, magnet: true, stroke: t.color, fill: '#fff', strokeWidth: 1.2, visibility: portVis } } }
        },
        items: [{ id: 'pt', group: 'top' }, { id: 'pr', group: 'right' }, { id: 'pb', group: 'bottom' }, { id: 'pl', group: 'left' }]
      };
      graph.addNode(nodeSpec);
    });

    // 全局 router（单条边可在 e.router 覆盖）
    var globalRouter = MM_ROUTERS[_mmRouterKey] || MM_ROUTERS.rounded;
    rawEdges.forEach(function (e) {
      var src = e.source, tgt = e.target;
      if (!src || !tgt) return;
      // 单条覆盖优先：dash → style 字段（向后兼容）
      var dashMode = e.dash || (e.style === 'dashed' ? 'dashed' : 'solid');
      var isDashed = (dashMode === 'dashed');
      var stroke = e.color || 'rgba(255,255,255,.55)';
      // 单条 router 覆盖：未设则用全局
      var rKey = e.router, rCfg = rKey ? (MM_ROUTERS[rKey] || globalRouter) : globalRouter;
      var lineAttrs = { stroke: stroke, strokeWidth: 1.5, strokeDasharray: isDashed ? '4,3' : '', targetMarker: { name: 'block', size: 6 } };
      if (e.bidir) lineAttrs.sourceMarker = { name: 'block', size: 6 }; // 双向箭头
      var edgeSpec = {
        source: src, target: tgt,
        attrs: { line: lineAttrs },
        labels: e.label ? [{
          position: 0.5,
          markup: [{ tagName: 'rect', selector: 'bg' }, { tagName: 'text', selector: 'label' }],
          attrs: {
            label: { text: e.label, fontSize: 12, fill: '#000', fontWeight: 600, textAnchor: 'middle', textVerticalAnchor: 'middle' },
            bg: { ref: 'label', refX: -3, refY: -2, refWidth: 6, refHeight: 4, fill: e.color || '#e8eaed', stroke: 'none' }
          }
        }] : undefined,
        data: { id: e.id, label: e.label || '', style: e.style || 'solid', router: rKey || null, color: e.color || null, dash: dashMode, bidir: !!e.bidir }
      };
      if (rCfg.router) edgeSpec.router = rCfg.router;
      if (rCfg.connector) edgeSpec.connector = rCfg.connector;
      // 自动选节点边界最近点，不穿框
      edgeSpec.connectionPoint = 'boundary';
      // 曼哈顿路由自动绕开障碍节点
      if (!edgeSpec.router) edgeSpec.router = { name: 'manhattan', args: { padding: 12 } };
      graph.addEdge(edgeSpec);
    });

    graph.on('scale', function () {
      var lbl = document.getElementById('mmZoomLabel');
      if (lbl) lbl.textContent = Math.round(graph.zoom() * 100) + '%';
      mmSaveViewDebounced();
    });
    graph.on('translate', function () {
      mmSaveViewDebounced();
    });

    container.addEventListener('wheel', function (e) {
      if (!graph) return;
      e.preventDefault(); e.stopPropagation();
      var rect = container.getBoundingClientRect();
      var cx = e.clientX - rect.left, cy = e.clientY - rect.top;
      var factor = e.deltaY < 0 ? 1.12 : 0.89;
      var current = graph.zoom();
      var next = current * factor;
      if (next < 0.2 || next > 5) return;
      try {
        var t = graph.translate();
        var ratio = next / current;
        graph.zoomTo(next);
        graph.translate(cx * (1 - ratio) + t.tx * ratio, cy * (1 - ratio) + t.ty * ratio);
      } catch (_) { graph.zoomTo(next); }
    }, { passive: false, capture: true });

    function doFit() {
      var w = container.clientWidth, h = container.clientHeight;
      if (w < 200) w = container.parentElement ? container.parentElement.clientWidth : window.innerWidth - 60;
      if (h < 200) h = container.parentElement ? container.parentElement.clientHeight : window.innerHeight - 200;
      try {
        // SVG 本身是 width/height:100% + absolute inset:0 自适应容器（实测 X6 v1.34.1 无 viewBox），
        // 手动设像素宽高反而会把它钉死，容器一变就露底黑边。这里只同步 graph 内部尺寸。
        var svg = container.querySelector('svg');
        if (svg) {
          svg.style.width = '';
          svg.style.height = '';
          svg.style.display = 'block';
        }
        graph.resize(w, h);
        if (rawNodes.length > 0) graph.zoomToFit({ padding: 30, maxScale: 1.2 });
      } catch (_) {}
    }
    if (skipFit) {
      // 编辑模式切换：只 resize 不 fit，保留画布状态
      try {
        var w = container.clientWidth, h = container.clientHeight;
        if (w < 200) w = container.parentElement ? container.parentElement.clientWidth : window.innerWidth - 60;
        if (h < 200) h = container.parentElement ? container.parentElement.clientHeight : window.innerHeight - 200;
        var svg = container.querySelector('svg');
        if (svg) {
          svg.style.width = '';
          svg.style.height = '';
          svg.style.display = 'block';
        }
        graph.resize(w, h);
      } catch (_) {}
      // 直接恢复之前保存的视图状态
      setTimeout(mmRestoreView, 50);
    } else {
      doFit();
      setTimeout(doFit, 350);
      // 700ms 后：所有 doFit 完成，恢复用户上次的缩放/平移（如果有保存）
      setTimeout(function () {
        var restored = mmRestoreView();
        if (!restored) setTimeout(doFit, 50);  // 没有保存状态时补一次 fit
      }, 700);
    }
    // window resize 时重新 fit
    var fitTimer = null;
    function onResize() { clearTimeout(fitTimer); fitTimer = setTimeout(doFit, 200); }
    window.addEventListener('resize', onResize);
    // 容器尺寸变化时同步画布尺寸
    // X6 v1 的 graph.resize 不更新 SVG width/height 属性，需手动同步避免黑边
    var resizeDebounce = null;
    function syncCanvasSize() {
      if (!_mmGraph) return;
      var w = container.clientWidth, h = container.clientHeight;
      if (w < 10 || h < 10) return;
      // 根因（真实页面实测）：X6 构造时把 container 内联 style.width 钉成固定像素
      // （覆盖 flex stretch），graph.resize() 不更新它，容器不跟随面板拉宽 → 右侧露底黑边。
      // 修复：恢复 100% 让 flex 布局重新接管（stretch），再读真实尺寸喂给 graph.resize；
      // SVG width/height 恒 100% 自适应跟随，viewBox 不动，zoom/pan 不受影响。
      container.style.width = '100%';
      container.style.height = '100%';
      w = container.clientWidth;
      h = container.clientHeight;
      if (w < 10 || h < 10) return;
      try { graph.resize(w, h); } catch (_) {}
      // 黑边根因（三轮实测 X6 v1.34.1）：SVG 恒为 width/height:100% + 无 viewBox，
      // 一直自适应容器；黑边来自「容器拉宽后内容坐标没跟随」——右侧新增区域露出深色背景。
      // 修复：resize 后 centerContent，保持 zoom 不变、内容居中到新容器
      // （流程图每次 doFit 都 zoomToFit 重新适配，所以从没黑边）。
      try { if (rawNodes.length > 0) graph.centerContent(); } catch (_) {}
    }
    function onContainerResize() {
      clearTimeout(resizeDebounce);
      resizeDebounce = setTimeout(function () {
        requestAnimationFrame(syncCanvasSize);
      }, 30);
    }
    // 面板拖拽过程中同步
    window.addEventListener('mm:panel-resize', onContainerResize);
    // ResizeObserver 兜底（覆盖非拖拽场景）
    var ro = null;
    if (window.ResizeObserver) {
      ro = new ResizeObserver(onContainerResize);
      try { ro.observe(container); } catch (_) {}
    }
    graph.on('dispose', function () {
      window.removeEventListener('resize', onResize);
      window.removeEventListener('mm:panel-resize', onContainerResize);
      if (ro) try { ro.disconnect(); } catch (_) {}
      clearTimeout(fitTimer);
      clearTimeout(resizeDebounce);
    });

    // 事件绑定始终注册，处理函数内用 _mmEditable 检查状态
    // （这样切换编辑模式时不重建 graph 也能正常响应）
    graph.on('node:dblclick', function (args) {
      if (!_mmEditable) return;
      var cell = args.cell;
      if (!cell || !cell.isNode()) return;
      mmShowEditor(cell);
    });
    graph.on('node:contextmenu', function (args) {
      if (!_mmEditable) return;
      var cell = args.cell;
      if (!cell || !cell.isNode()) return;
      args.e && args.e.preventDefault && args.e.preventDefault();
      // PC 端：如果当前有多个选中节点，右键显示批量操作菜单
      if (!mmIsMobile()) {
        var selected = graph.getSelectedCells().filter(function (c) { return c.isNode && c.isNode(); });
        if (selected.length > 1 && selected.indexOf(cell) >= 0) {
          mmShowBatchMenu(args.e, selected);
          return;
        }
      }
      mmShowMenu(args.e, cell);
    });
    graph.on('blank:contextmenu', function (args) {
      if (!_mmEditable) return;
      args.e && args.e.preventDefault && args.e.preventDefault();
      mmShowBlankMenu(args.e, args.x, args.y);
    });
    graph.on('edge:dblclick', function (args) {
      if (!_mmEditable) return;
      var cell = args.cell;
      if (!cell || !cell.isEdge()) return;
      mmShowEdgeMenu(args.e, cell);
    });
    graph.on('edge:contextmenu', function (args) {
      if (!_mmEditable) return;
      var cell = args.cell;
      if (!cell || !cell.isEdge()) return;
      args.e && args.e.preventDefault && args.e.preventDefault();
      mmShowEdgeMenu(args.e, cell);
    });
    // ── 全局改动事件监听：触发保存纳入撤销栈 ──
    // immediate=true 走 150ms 短防抖，确保用户按 Ctrl+Z 时已持久化
    // 节点拖动结束：立即保存（最核心的撤销场景）
    graph.on('node:moved', function () {
      if (_mmEditable) {
        // 用户自定义过节点位置 → 之后 AI 改图改回局部排版
        if (!_mmUserMoved) _mmUserMoved = true;
        mmMarkDirty(true);
      }
    });
    // 连线建立/断开：立即保存
    graph.on('edge:connected', function () { if (_mmEditable) mmMarkDirty(true); });
    // 节点/边增删：立即保存
    graph.on('cell:added', function () { if (_mmEditable) mmMarkDirty(true); });
    graph.on('cell:removed', function () { if (_mmEditable) mmMarkDirty(true); });
    // 边的 target/source 改变（重新连线）：立即保存
    graph.on('edge:change:target', function () { if (_mmEditable) mmMarkDirty(true); });
    graph.on('edge:change:source', function () { if (_mmEditable) mmMarkDirty(true); });
    // 节点 attr 改变（mmChangeNodeType/mmChangeNodeColor 等走 attr API）
    // 注意：hover 高亮也会改 attr，所以这些函数内部必须先 disableHistory 再改 attr
    // 但当前项目没有 hover 改 attr，直接监听即可
    graph.on('node:change:attrs', function (args) {
      if (!_mmEditable) return;
      // 过滤掉非用户操作的 attrs 变更（如 mmRender 初始化时的设置）
      // 这里简单处理：只要在编辑模式就标记，因为 X6 内部初始化不会触发该事件
      mmMarkDirty(true);
    });
    // 边 attr 改变（改颜色/虚线/router 等）：立即保存
    graph.on('edge:change:attrs', function () { if (_mmEditable) mmMarkDirty(true); });
    // 节点 data 改变（如 collapsed、note 等）：立即保存
    graph.on('node:change:data', function () { if (_mmEditable) mmMarkDirty(true); });
    graph.on('edge:change:data', function () { if (_mmEditable) mmMarkDirty(true); });

    graph.on('blank:dblclick', function (args) {
      if (_mmEditable) {
        mmAddNodeAt(args.x, args.y);
      } else {
        try { graph.zoomToFit({ padding: 30, maxScale: 1.2 }); } catch (_) {}
      }
    });

    // ── 触摸交互 ──
    var _mmTouch = null;
    var _mmInertia = null;

    function _mmStopInertia() { if (_mmInertia) { try { cancelAnimationFrame(_mmInertia); } catch (_) {} _mmInertia = null; } }
    function _mmStartInertia(g, vx, vy) {
      _mmStopInertia();
      var friction = 0.94, minSpeed = 0.3;
      function step() {
        if (!g) { _mmInertia = null; return; }
        vx *= friction; vy *= friction;
        if (Math.sqrt(vx * vx + vy * vy) < minSpeed) { _mmInertia = null; return; }
        try { var tr = g.translate(); g.translate(tr.tx + vx, tr.ty + vy); } catch (_) { _mmInertia = null; return; }
        _mmInertia = requestAnimationFrame(step);
      }
      _mmInertia = requestAnimationFrame(step);
    }

    container.addEventListener('touchstart', function (e) {
      if (!graph) return;
      _mmStopInertia();
      var t = e.touches, rect = container.getBoundingClientRect();
      if (t.length === 2) {
        e.preventDefault(); e.stopPropagation();
        try { graph.panning.disable(); } catch (_) {}
        var p0 = { x: t[0].clientX - rect.left, y: t[0].clientY - rect.top };
        var p1 = { x: t[1].clientX - rect.left, y: t[1].clientY - rect.top };
        var dx = p0.x - p1.x, dy = p0.y - p1.y;
        var dist = Math.sqrt(dx * dx + dy * dy) || 1;
        _mmTouch = { mode: 'pinch', lastDist: dist, lastCx: (p0.x + p1.x) / 2, lastCy: (p0.y + p1.y) / 2 };
      } else if (t.length === 1 && (!_mmTouch || _mmTouch.mode !== 'pinch')) {
        // 边缘返回手势：左 1/5 区域 + 文件管理器已打开 → 不启动思维导图拖动，
        // 让 fileBrowser 的关闭手势接管（右滑返回聊天页）
        if (typeof fbOpen !== 'undefined' && fbOpen && _mmViewActive && t[0].clientX < window.innerWidth * 0.2) {
          return;
        }
        var p = { x: t[0].clientX - rect.left, y: t[0].clientY - rect.top };
        var now = Date.now();
        if (_mmTouch && _mmTouch.mode === 'tap-wait' && (now - _mmTouch.tapTime) < 350 && Math.abs(p.x - _mmTouch.tapX) < 30 && Math.abs(p.y - _mmTouch.tapY) < 30) {
          e.preventDefault();
          var cell = null;
          try { cell = graph.getCellByPoint(p.x, p.y); } catch (_) {}
          if (!cell) {
            if (editable) {
              var gp; try { gp = graph.clientToGraph(t[0].clientX, t[0].clientY); } catch (_) { gp = { x: p.x, y: p.y }; }
              mmAddNodeAt(gp.x, gp.y);
            } else { try { graph.zoomToFit({ padding: 30, maxScale: 1.2 }); } catch (_) {} }
          } else if (editable && cell.isNode()) {
            mmShowEditor(cell);
          }
          _mmTouch = null;
          return;
        }
        var tr2 = graph.translate();
        _mmTouch = {
          mode: 'pan', startX: p.x, startY: p.y, startTx: tr2.tx, startTy: tr2.ty,
          panStartX: p.x, panStartY: p.y, moved: false, tapTime: now,
          lastX: p.x, lastY: p.y, lastT: now, vx: 0, vy: 0,
          clientX: t[0].clientX, clientY: t[0].clientY,
          longPressTimer: null, longPressed: false
        };
        if (_mmEditable) {
          (function (touch) {
            touch.longPressTimer = setTimeout(function () {
              if (_mmTouch === touch && !touch.moved) {
                var c = null;
                try { c = graph.getCellByPoint(touch.startX, touch.startY); } catch (_) {}
                var fe = { clientX: touch.clientX, clientY: touch.clientY, preventDefault: function () {} };
                if (c && c.isNode()) {
                  if (mmIsMobile()) {
                    // 移动端：长按节点进入拖动模式（不弹菜单）
                    touch.dragCell = c;
                    touch.dragStartPos = c.getPosition();
                    touch.longPressed = true;
                    // 触觉反馈
                    try { if (navigator.vibrate) navigator.vibrate(30); } catch (_) {}
                    mmSelectCell(c);
                  } else {
                    mmShowMenu(fe, c);
                    touch.longPressed = true;
                  }
                } else {
                  var gp; try { gp = graph.clientToGraph(touch.clientX, touch.clientY); } catch (_) { gp = { x: touch.startX, y: touch.startY }; }
                  mmShowBlankMenu(fe, gp.x, gp.y);
                  touch.longPressed = true;
                }
              }
            }, 500);
          })(_mmTouch);
        }
      }
    }, { passive: false, capture: true });

    container.addEventListener('touchmove', function (e) {
      if (!graph || !_mmTouch) return;
      if (_mmTouch.longPressed && _mmTouch.dragCell) {
        // 移动端拖动节点模式：移动节点位置
        e.preventDefault();
        var t1 = e.touches[0], rect1 = container.getBoundingClientRect();
        var cp = { x: t1.clientX - rect1.left, y: t1.clientY - rect1.top };
        // 将屏幕坐标增量转为图坐标增量
        try {
          var delta = graph.deltaPIXELToGraph(cp.x - _mmTouch.startX, cp.y - _mmTouch.startY);
          _mmTouch.dragCell.position(_mmTouch.dragStartPos.x + delta.x, _mmTouch.dragStartPos.y + delta.y);
        } catch (_) {}
        return;
      }
      if (_mmTouch.longPressed) { e.preventDefault(); return; }
      var t = e.touches, rect = container.getBoundingClientRect();
      if (_mmTouch.mode === 'pinch' && t.length === 2) {
        e.preventDefault(); e.stopPropagation();
        var p0 = { x: t[0].clientX - rect.left, y: t[0].clientY - rect.top };
        var p1 = { x: t[1].clientX - rect.left, y: t[1].clientY - rect.top };
        var dx = p0.x - p1.x, dy = p0.y - p1.y;
        var dist = Math.sqrt(dx * dx + dy * dy);
        var cx = (p0.x + p1.x) / 2, cy = (p0.y + p1.y) / 2;
        if (_mmTouch.lastDist > 0 && dist > 0) {
          var factor = dist / _mmTouch.lastDist;
          try {
            var cur = graph.zoom();
            var next = Math.max(0.2, Math.min(5, cur * factor));
            graph.zoomTo(next, { x: cx, y: cy });
          } catch (_) {}
          _mmTouch.lastDist = dist;
        }
      } else if (_mmTouch.mode === 'pan' && t.length === 1) {
        var p = { x: t[0].clientX - rect.left, y: t[0].clientY - rect.top };
        var ddx = p.x - _mmTouch.startX, ddy = p.y - _mmTouch.startY;
        if (Math.abs(ddx) > 8 || Math.abs(ddy) > 8) {
          if (!_mmTouch.moved) {
            _mmTouch.moved = true;
            if (_mmTouch.longPressTimer) { clearTimeout(_mmTouch.longPressTimer); _mmTouch.longPressTimer = null; }
            var tr = graph.translate();
            _mmTouch.startTx = tr.tx; _mmTouch.startTy = tr.ty;
            _mmTouch.panStartX = p.x; _mmTouch.panStartY = p.y;
          }
          e.preventDefault();
          var nowT = Date.now();
          var dt = nowT - _mmTouch.lastT;
          if (dt > 0) { _mmTouch.vx = (p.x - _mmTouch.lastX) / dt * 16; _mmTouch.vy = (p.y - _mmTouch.lastY) / dt * 16; }
          _mmTouch.lastX = p.x; _mmTouch.lastY = p.y; _mmTouch.lastT = nowT;
          try { graph.translate(_mmTouch.startTx + (p.x - _mmTouch.panStartX), _mmTouch.startTy + (p.y - _mmTouch.panStartY)); } catch (_) {}
        }
      }
    }, { passive: false, capture: true });

    container.addEventListener('touchend', function (e) {
      if (!_mmTouch) return;
      if (_mmTouch.longPressTimer) { clearTimeout(_mmTouch.longPressTimer); _mmTouch.longPressTimer = null; }
      if (_mmTouch.longPressed) {
        e.preventDefault();
        // 拖动节点结束：标记脏 + 视觉反馈
        if (_mmTouch.dragCell) {
          mmMarkDirty();
          mmToast('节点已移动');
        }
        _mmTouch = null;
        return;
      }
      if (_mmTouch.mode === 'pinch') {
        try { graph.panning.enable(); } catch (_) {}
        _mmTouch = null;
      } else if (_mmTouch.mode === 'pan' && !_mmTouch.moved) {
        // 移动端编辑模式：短按未移动 = tap，选中节点或完成连线
        if (_mmEditable && mmIsMobile()) {
          var cell = null;
          try { cell = graph.getCellByPoint(_mmTouch.startX, _mmTouch.startY); } catch (_) {}
          if (_mmConnectMode) {
            // 连线模式：tap 目标节点完成连线
            if (cell && cell.isNode && cell.isNode()) {
              mmMobileConnectTo(cell);
            }
          } else if (cell) {
            // 选中节点/边
            mmSelectCell(cell);
          } else {
            // 点空白：清除选中
            mmClearSelection();
          }
          _mmTouch = null;
          return;
        }
        _mmTouch = { mode: 'tap-wait', tapTime: Date.now(), tapX: _mmTouch.startX, tapY: _mmTouch.startY };
      } else if (_mmTouch.mode === 'pan' && _mmTouch.moved) {
        var speed = Math.sqrt(_mmTouch.vx * _mmTouch.vx + _mmTouch.vy * _mmTouch.vy);
        if (speed > 0.5) _mmStartInertia(graph, _mmTouch.vx, _mmTouch.vy);
        _mmTouch = null;
      } else { _mmTouch = null; }
    }, { passive: false, capture: true });

    // 非编辑模式：禁用选择模块，避免点击节点出现选择框（视觉噪音）
    if (!editable) {
      try { graph.disableSelection(); } catch (_) {}
    }

    // PC 端编辑模式：初始化自定义框选（rubberband）
    // 在空白处左键拖动框选节点，Shift+左键留给画布平移
    _mmInitRubberband(graph, container);

    // 初始化右键拖动平移（右键拖动平移画布，右键点击显示菜单）
    _mmInitRightPan(graph, container);

    return graph;
  } catch (e) {
    try { graph.dispose(); } catch (_) {}
    return null;
  }
}

// 恢复编辑状态
_mmEditable = localStorage.getItem('mm_editable') === '1';

// 点击外部关闭菜单
document.addEventListener('click', function (e) {
  var m = document.getElementById('mmMenu');
  if (m && !m.contains(e.target)) mmHideMenu();
}, true);

// ════════════════════════════════════════════════════════════════════
// 移动端适配层 + 补全功能（PC+移动端通用）
// 设计原则：PC 端零改动，移动端通过设备检测分流到专属交互
// ════════════════════════════════════════════════════════════════════

// ── 设备检测：是否为移动端（触摸 + 窄屏）──
function mmIsMobile() {
  if (typeof window === 'undefined') return false;
  var hasTouch = ('ontouchstart' in window) || (navigator.maxTouchPoints || 0) > 0;
  var narrow = window.innerWidth <= 820;
  return hasTouch && narrow;
}

// ── 移动端底部 Sheet 菜单渲染器 ──
// menu: 已构建的 DOM 节点（mmShowEdgeMenu 场景）或 null（mmShowMenu/mmShowBlankMenu 场景）
// items: 菜单项数组 [{label, action, sub, subItems}] 或 null
// title: sheet 标题
function mmRenderMenuSheet(menu, items, title) {
  mmHideMenu();
  var sheet = document.createElement('div');
  sheet.id = 'mmMenu';
  sheet.style.cssText = 'position:fixed;left:0;right:0;bottom:0;background:var(--surface2);border:1px solid var(--border2);border-radius:12px 12px 0 0;padding:8px 16px 20px;z-index:10001;max-height:70vh;overflow-y:auto;box-shadow:0 -8px 32px rgba(0,0,0,.4);font-size:13px';
  // 标题栏
  var hdr = document.createElement('div');
  hdr.style.cssText = 'text-align:center;font-size:12px;color:var(--tdim);padding:4px 0 8px;border-bottom:1px solid var(--border);margin-bottom:8px';
  hdr.textContent = title || '菜单';
  sheet.appendChild(hdr);

  function renderItems(arr, container) {
    arr.forEach(function (it) {
      if (it.sep) {
        var s = document.createElement('div');
        s.style.cssText = 'height:1px;background:var(--border);margin:4px 0';
        container.appendChild(s);
        return;
      }
      var el = document.createElement('div');
      el.style.cssText = 'padding:10px 14px;cursor:pointer;color:var(--fg);border-radius:6px;transition:background .12s;display:flex;align-items:center;gap:8px' + (it.active ? ';color:var(--accent);font-weight:600' : '');
      el.ontouchstart = function () { el.style.background = 'var(--hover)'; };
      el.ontouchend = function () { el.style.background = ''; };
      // 智能渲染：label 形如 "● #4aa3ff" 或 "✓ ● #4aa3ff" 时，把 ● 替换成真实色块
      var labelStr = String(it.label || '');
      var m = labelStr.match(/^(✓\s*)?●\s*(#[0-9a-fA-F]{3,8})$/);
      if (m) {
        var checked = m[1] || '';
        var color = m[2];
        if (checked) {
          var ck = document.createElement('span');
          ck.textContent = '✓';
          ck.style.cssText = 'color:var(--accent);font-size:12px;width:14px;flex-shrink:0';
          el.appendChild(ck);
        } else {
          var spacer = document.createElement('span');
          spacer.style.cssText = 'width:14px;flex-shrink:0';
          el.appendChild(spacer);
        }
        var dot = document.createElement('span');
        dot.style.cssText = 'display:inline-block;width:16px;height:16px;border-radius:50%;background:' + color + ';border:1px solid rgba(255,255,255,.2);flex-shrink:0';
        el.appendChild(dot);
        var txt = document.createElement('span');
        txt.textContent = it.colorName || color;
        el.appendChild(txt);
        el.title = color;
      } else {
        el.textContent = labelStr;
      }
      if (it.sub && it.subItems) {
        // 子菜单：点击展开/收起，内联显示
        var subWrap = document.createElement('div');
        subWrap.style.cssText = 'display:none;padding:4px 0 4px 20px';
        renderItems(it.subItems, subWrap);
        el.onclick = function (ev) {
          ev.stopPropagation();
          subWrap.style.display = subWrap.style.display === 'none' ? 'block' : 'none';
          el.style.background = subWrap.style.display === 'block' ? 'var(--hover)' : '';
        };
        container.appendChild(el);
        container.appendChild(subWrap);
      } else {
        el.onclick = function (ev) { ev.stopPropagation(); mmHideMenu(); it.action(); };
        container.appendChild(el);
      }
    });
  }

  if (items) {
    renderItems(items, sheet);
  } else if (menu) {
    // mmShowEdgeMenu 场景：menu 已构建但未 append，提取其子节点
    while (menu.firstChild) sheet.appendChild(menu.firstChild);
  }
  document.body.appendChild(sheet);
  // 阻止背景滚动
  sheet.addEventListener('touchmove', function (e) { e.stopPropagation(); }, { passive: true });
}

// ── PC 端子菜单项构建器（hover 展开式）──
function mmAddSubmenuItem(menu, it) {
  var wrap = document.createElement('div');
  wrap.style.cssText = 'padding:6px 14px;cursor:pointer;color:var(--fg);position:relative;transition:background .12s';
  wrap.textContent = it.label;
  var sub = document.createElement('div');
  sub.style.cssText = 'display:none;position:absolute;left:100%;top:0;background:var(--surface2);border:1px solid var(--border2);border-radius:6px;padding:4px 0;min-width:120px;box-shadow:0 4px 16px rgba(0,0,0,.4)';
  (it.subItems || []).forEach(function (si) {
    var el = document.createElement('div');
    el.style.cssText = 'padding:6px 12px;cursor:pointer;color:var(--fg);transition:background .12s;display:flex;align-items:center;gap:8px';
    el.onmouseenter = function () { el.style.background = 'var(--hover)'; };
    el.onmouseleave = function () { el.style.background = ''; };
    el.onclick = function (ev) { ev.stopPropagation(); mmHideMenu(); si.action(); };
    // 智能渲染：label 形如 "● #4aa3ff" 或 "✓ ● #4aa3ff" 时，把 ● 替换成真实色块
    var labelStr = String(si.label || '');
    var m = labelStr.match(/^(✓\s*)?●\s*(#[0-9a-fA-F]{3,8})$/);
    if (m) {
      var checked = m[1] || '';
      var color = m[2];
      if (checked) {
        var ck = document.createElement('span');
        ck.textContent = '✓';
        ck.style.cssText = 'color:var(--accent);font-size:11px;width:12px;flex-shrink:0';
        el.appendChild(ck);
      } else {
        var spacer = document.createElement('span');
        spacer.style.cssText = 'width:12px;flex-shrink:0';
        el.appendChild(spacer);
      }
      var dot = document.createElement('span');
      dot.style.cssText = 'display:inline-block;width:14px;height:14px;border-radius:50%;background:' + color + ';border:1px solid rgba(255,255,255,.2);flex-shrink:0';
      el.appendChild(dot);
      var txt = document.createElement('span');
      txt.textContent = si.colorName || color;
      el.appendChild(txt);
      el.title = color;
    } else {
      el.textContent = labelStr;
    }
    sub.appendChild(el);
  });
  wrap.onmouseenter = function () {
    wrap.style.background = 'var(--hover)';
    sub.style.display = 'block';
    // 先复位为默认右侧展开，再按视口边界纠正，避免贴边时子菜单溢出不可见
    sub.style.left = '100%'; sub.style.right = 'auto';
    sub.style.top = '0'; sub.style.bottom = 'auto';
    var r = sub.getBoundingClientRect();
    if (r.right > window.innerWidth - 4) { sub.style.left = 'auto'; sub.style.right = '100%'; }
    if (r.bottom > window.innerHeight - 4) { sub.style.top = 'auto'; sub.style.bottom = '0'; }
  };
  wrap.onmouseleave = function () { wrap.style.background = ''; sub.style.display = 'none'; };
  wrap.appendChild(sub);
  menu.appendChild(wrap);
}

// ── 设置入口：收纳 自动布局/适应画布/布局算法/连线样式，多级菜单 ──
// PC 端：跟随鼠标，子菜单 hover 展开；移动端：底部 sheet，子菜单点击展开
function mmShowSettingsMenu(e) {
  mmHideMenu();
  // 布局算法子菜单项
  var layoutItems = Object.keys(MM_LAYOUTS).map(function (k) {
    return { label: (k === _mmLayoutKey ? '✓ ' : '') + MM_LAYOUTS[k].name, active: k === _mmLayoutKey, action: function () { mmHideMenu(); mmApplyLayout(k); } };
  });
  // 连线样式子菜单项
  var routerItems = Object.keys(MM_ROUTERS).map(function (k) {
    return { label: (k === _mmRouterKey ? '✓ ' : '') + MM_ROUTERS[k].name, active: k === _mmRouterKey, action: function () { mmHideMenu(); mmApplyRouter(k); } };
  });
  // 统一 items 数组
  var items = [
    { label: '自动布局', action: function () { mmHideMenu(); mmAutoLayout(); } },
    { label: '适应画布', action: function () { mmHideMenu(); mmZoomToFit(); } },
    { label: '', sep: true },
    { label: '布局算法 ▸', sub: true, subItems: layoutItems },
    { label: '连线样式 ▸', sub: true, subItems: routerItems }
  ];

  // 移动端：底部 sheet
  if (mmIsMobile()) {
    mmRenderMenuSheet(null, items, '设置');
    return;
  }

  // PC 端：跟随鼠标，子菜单 hover 展开
  var x = (e && e.clientX) || window.innerWidth / 2, y = (e && e.clientY) || 60;
  var menu = document.createElement('div');
  menu.id = 'mmMenu';
  menu.style.cssText = 'position:fixed;left:' + x + 'px;top:' + y + 'px;background:var(--surface2);border:1px solid var(--border2);border-radius:6px;padding:4px 0;z-index:10001;min-width:150px;box-shadow:0 4px 16px rgba(0,0,0,.4);font-size:12px';
  items.forEach(function (it) {
    if (it.sep) {
      var s = document.createElement('div');
      s.style.cssText = 'height:1px;background:var(--border);margin:4px 0';
      menu.appendChild(s);
    } else if (it.sub) {
      mmAddSubmenuItem(menu, it);
    } else {
      var el = document.createElement('div');
      el.textContent = it.label;
      el.style.cssText = 'padding:6px 14px;cursor:pointer;color:var(--fg);transition:background .12s';
      el.onmouseenter = function () { el.style.background = 'var(--hover)'; };
      el.onmouseleave = function () { el.style.background = ''; };
      el.onclick = function () { mmHideMenu(); it.action(); };
      menu.appendChild(el);
    }
  });
  document.body.appendChild(menu);
  var r = menu.getBoundingClientRect();
  if (r.right > window.innerWidth) menu.style.left = (window.innerWidth - r.width - 8) + 'px';
  if (r.bottom > window.innerHeight) menu.style.top = (window.innerHeight - r.height - 8) + 'px';
}

// ── 移动端选中状态 + 底部操作栏 ──
var _mmSelectedCell = null;     // 当前选中的节点/边
var _mmConnectMode = false;     // 是否处于连线模式（选中源节点后等待点目标节点）

// 选中节点/边，更新底部操作栏
function mmSelectCell(cell) {
  _mmSelectedCell = cell;
  mmUpdateMobileBar();
}

// 清除选中
function mmClearSelection() {
  _mmSelectedCell = null;
  _mmConnectMode = false;
  mmUpdateMobileBar();
}

// 更新移动端底部操作栏（根据选中状态和编辑模式显示不同按钮）
function mmUpdateMobileBar() {
  var bar = document.getElementById('mmMobileBar');
  if (!bar) return;
  // 非编辑模式或无选中：隐藏
  if (!_mmEditable) {
    bar.style.display = 'none';
    _mmSelectedCell = null;
    _mmConnectMode = false;
    return;
  }
  if (!_mmSelectedCell) {
    // 编辑模式但无选中：显示提示
    bar.style.display = 'flex';
    bar.innerHTML = '<span style="font-size:11px;color:var(--tdim);padding:4px 8px">长按节点拖动 · 长按节点弹出菜单 · 双击编辑</span>';
    return;
  }
  bar.style.display = 'flex';
  var isNode = _mmSelectedCell.isNode && _mmSelectedCell.isNode();
  var btns = [];
  if (_mmConnectMode) {
    // 连线模式：提示点目标
    bar.innerHTML = '<span style="font-size:12px;color:var(--accent);padding:4px 8px;flex:1">连线模式：点击目标节点完成连线</span>' +
      '<button onclick="mmCancelConnect()" style="font-size:11px;padding:4px 10px;background:transparent;color:var(--danger);border:1px solid var(--border);border-radius:4px">取消</button>';
    return;
  }
  if (isNode) {
    btns.push({ label: '编辑', title: '编辑', action: function () { mmShowEditor(_mmSelectedCell); } });
    btns.push({ label: '连线', title: '连线', action: function () { _mmConnectMode = true; mmUpdateMobileBar(); } });
    btns.push({ label: '子节点', title: '子节点', action: function () { mmAddChildNode(_mmSelectedCell); } });
    btns.push({ label: '排列', title: '排列', action: function () { mmLayoutSubtree(_mmSelectedCell); } });
    btns.push({ label: '菜单', title: '菜单', action: function () {
      var fe = { clientX: window.innerWidth / 2, clientY: window.innerHeight / 2, preventDefault: function () {} };
      mmShowMenu(fe, _mmSelectedCell);
    } });
    btns.push({ label: '删除', title: '删除', action: function () { _mmSelectedCell.remove(); mmMarkDirty(); mmClearSelection(); } });
  } else {
    // 边
    btns.push({ label: '样式', title: '样式', action: function () {
      var fe = { clientX: window.innerWidth / 2, clientY: window.innerHeight / 2, preventDefault: function () {} };
      mmShowEdgeMenu(fe, _mmSelectedCell);
    } });
    btns.push({ label: '标签', title: '标签', action: function () {
      mmShowLabelEditor(_mmSelectedCell, (_mmSelectedCell.getData() || {}).label || '', function (nl) {
        var d = _mmSelectedCell.getData() || {};
        d.label = nl; _mmSelectedCell.setData(d);
        _mmSyncEdgeField(_mmSelectedCell, { label: nl });
        if (nl) {
          try { _mmSelectedCell.setLabels([{ position: 0.5, markup: [{ tagName: 'rect', selector: 'bg' }, { tagName: 'text', selector: 'label' }], attrs: { label: { text: nl, fontSize: 12, fill: '#000', fontWeight: 600, textAnchor: 'middle', textVerticalAnchor: 'middle' }, bg: { ref: 'label', refX: -3, refY: -2, refWidth: 6, refHeight: 4, fill: d.color || '#e8eaed', stroke: 'none' } } }]); } catch (_) {}
        } else { try { _mmSelectedCell.setLabels([]); } catch (_) {} }
        mmMarkDirty();
      });
    } });
    btns.push({ label: '删除', title: '删除', action: function () { _mmSelectedCell.remove(); mmMarkDirty(); mmClearSelection(); } });
  }
  bar.innerHTML = btns.map(function (b, i) {
    return '<button onclick="mmBarAction(' + i + ')" title="' + b.title + '" style="font-size:11px;padding:6px 10px;background:transparent;color:var(--fg);border:1px solid var(--border);border-radius:6px;cursor:pointer;min-width:40px">' + b.label + '</button>';
  }).join('');
  // 绑定 action
  bar._mmActions = btns.map(function (b) { return b.action; });
}

// 底部操作栏按钮回调
function mmBarAction(idx) {
  var bar = document.getElementById('mmMobileBar');
  if (bar && bar._mmActions && bar._mmActions[idx]) bar._mmActions[idx]();
}

// 取消连线模式
function mmCancelConnect() {
  _mmConnectMode = false;
  mmUpdateMobileBar();
}

// 完成连线（移动端：选中源 → 点目标）
function mmMobileConnectTo(targetCell) {
  if (!_mmConnectMode || !_mmSelectedCell) return false;
  if (!targetCell || !targetCell.isNode || !targetCell.isNode()) return false;
  if (targetCell.id === _mmSelectedCell.id) { mmCancelConnect(); return false; }
  try {
    var rCfg = MM_ROUTERS[_mmRouterKey] || MM_ROUTERS.cubic;
    var spec = {
      source: _mmSelectedCell.id, target: targetCell.id,
      attrs: { line: { stroke: 'rgba(255,255,255,.55)', strokeWidth: 1.5, targetMarker: { name: 'block', size: 6 } } },
      data: { id: 'e_' + _mmSelectedCell.id + '_' + targetCell.id, label: '', style: 'solid', router: null, color: null, dash: 'solid' }
    };
    if (rCfg.router) spec.router = rCfg.router;
    if (rCfg.connector) spec.connector = rCfg.connector;
    _mmGraph.addEdge(spec);
    mmMarkDirty();
    mmToast('已创建连线');
  } catch (e) { console.warn('mmMobileConnectTo failed:', e); }
  _mmConnectMode = false;
  mmClearSelection();
  return true;
}

// ── 边标签自定义输入框（替代 prompt，PC+移动端通用）──
function mmShowLabelEditor(cell, curLabel, onConfirm) {
  var old = document.getElementById('mmLabelEditor');
  if (old) old.remove();
  var fb = document.getElementById('fileBrowser');
  if (!fb) return;
  var isMob = mmIsMobile();
  var div = document.createElement('div');
  div.id = 'mmLabelEditor';
  if (isMob) {
    div.style.cssText = 'position:absolute;left:0;right:0;bottom:0;background:var(--surface2);border:1px solid var(--border2);border-radius:12px 12px 0 0;padding:16px;z-index:10002;box-shadow:0 -8px 32px rgba(0,0,0,.4)';
  } else {
    div.style.cssText = 'position:absolute;left:50%;top:50%;transform:translate(-50%,-50%);background:var(--surface2);border:1px solid var(--border2);border-radius:8px;padding:16px 20px;z-index:10002;min-width:320px;box-shadow:0 8px 32px rgba(0,0,0,.4)';
  }
  div.innerHTML =
    '<div style="font-size:13px;color:var(--fg);margin-bottom:8px">连线标签</div>' +
    '<input id="mmLabelInput" type="text" value="' + esc(curLabel) + '" style="width:100%;background:var(--bg);color:var(--fg);border:1px solid var(--border);border-radius:4px;padding:8px;font-size:13px;box-sizing:border-box;margin-bottom:12px" placeholder="输入标签文字（留空清除标签）">' +
    '<div style="display:flex;justify-content:flex-end;gap:8px">' +
    '<button id="mmLabelCancel" style="padding:6px 14px;background:transparent;color:var(--dim);border:1px solid var(--border);border-radius:4px;cursor:pointer;font-size:12px">取消</button>' +
    '<button id="mmLabelOk" style="padding:6px 14px;background:var(--accent);color:#fff;border:none;border-radius:4px;cursor:pointer;font-size:12px">确定</button>' +
    '</div>';
  fb.appendChild(div);
  var input = document.getElementById('mmLabelInput');
  if (!isMob) input.focus();
  var close = function () { div.remove(); };
  var apply = function () {
    var v = input.value.trim();
    onConfirm(v);
    close();
  };
  document.getElementById('mmLabelCancel').onclick = close;
  document.getElementById('mmLabelOk').onclick = apply;
  input.onkeydown = function (e) {
    if (e.key === 'Enter') { e.preventDefault(); apply(); }
    else if (e.key === 'Escape') { e.preventDefault(); close(); }
  };
}

// ════════════════════════════════════════════════════════════════════
// 补全功能：添加子节点 / 改类型 / 改颜色 / 折叠子节点（PC+移动端通用）
// ════════════════════════════════════════════════════════════════════

// ── 添加子节点：在父节点右侧添加新节点并自动连线 ──
function mmAddChildNode(parentCell) {
  if (!_mmGraph || !parentCell) return;
  var parentData = parentCell.getData() || {};
  var parentId = parentData.id || parentCell.id;
  // 在父节点右侧偏移 240px 处放置新节点
  var pos = parentCell.getPosition();
  var newX = pos.x + MM_NODE_W + 60;
  var newY = pos.y + 40;
  var newId = 'm_' + Date.now() + '_' + Math.floor(Math.random() * 1000);
  var t = MM_TYPES.idea;
  var lines = mmWrap('新想法');
  var h = lines.length * MM_LINE_H + MM_PAD_Y * 2;
  var nodeSpec = {
    id: newId, shape: 'rect', width: MM_NODE_W, height: h,
    x: newX, y: newY,
    attrs: {
      body: { fill: t.fill, stroke: t.color, strokeWidth: t.stroke, rx: t.rx, ry: t.rx, strokeDasharray: t.dash },
      label: { text: lines.join('\n'), fill: t.color, fontSize: MM_FONT, textAnchor: 'middle', textVerticalAnchor: 'middle', lineHeight: MM_LINE_H }
    },
    data: { id: newId, type: 'idea', note: '', links: [] },
    ports: {
      groups: {
        top: { position: 'top', attrs: { circle: { r: 2.5, magnet: true, stroke: t.color, fill: '#fff', strokeWidth: 1.2, visibility: _mmEditable ? 'visible' : 'hidden' } } },
        right: { position: 'right', attrs: { circle: { r: 2.5, magnet: true, stroke: t.color, fill: '#fff', strokeWidth: 1.2, visibility: _mmEditable ? 'visible' : 'hidden' } } },
        bottom: { position: 'bottom', attrs: { circle: { r: 2.5, magnet: true, stroke: t.color, fill: '#fff', strokeWidth: 1.2, visibility: _mmEditable ? 'visible' : 'hidden' } } },
        left: { position: 'left', attrs: { circle: { r: 2.5, magnet: true, stroke: t.color, fill: '#fff', strokeWidth: 1.2, visibility: _mmEditable ? 'visible' : 'hidden' } } }
      },
      items: [{ id: 'pt', group: 'top' }, { id: 'pr', group: 'right' }, { id: 'pb', group: 'bottom' }, { id: 'pl', group: 'left' }]
    }
  };
  var cell = _mmGraph.addNode(nodeSpec);
  // 自动连线
  var rCfg = MM_ROUTERS[_mmRouterKey] || MM_ROUTERS.cubic;
  var edgeSpec = {
    source: parentId, target: newId,
    attrs: { line: { stroke: 'rgba(255,255,255,.55)', strokeWidth: 1.5, targetMarker: { name: 'block', size: 6 } } },
    data: { id: 'e_' + parentId + '_' + newId, label: '', style: 'solid', router: null, color: null, dash: 'solid' }
  };
  if (rCfg.router) edgeSpec.router = rCfg.router;
  if (rCfg.connector) edgeSpec.connector = rCfg.connector;
  edgeSpec.connectionPoint = 'boundary';
  if (!edgeSpec.router) edgeSpec.router = { name: 'manhattan', args: { padding: 12 } };
  _mmGraph.addEdge(edgeSpec);
  // 自动排列父节点的子树（新节点会放到合适位置）
  try { mmLayoutSubtree(parentCell, true); } catch (_) {}
  mmMarkDirty();
  // 打开编辑器
  if (cell && _mmEditable) mmShowEditor(cell);
  return cell;
}

// ── 修改节点类型 ──
function mmChangeNodeType(cell, newType) {
  if (!cell) return;
  var t = MM_TYPES[newType] || MM_TYPES.idea;
  var data = cell.getData() || {};
  // 字色 + 边框色：用户设置了 color 则保留，否则用新类型的类型色
  var finalColor = data.color || t.color;
  cell.attr('label/fill', finalColor);
  cell.attr('body/stroke', finalColor);
  cell.attr('body/strokeWidth', t.stroke);
  cell.attr('body/strokeDasharray', t.dash);
  cell.attr('body/fill', t.fill);
  cell.setData(Object.assign({}, data, { type: newType }));
  mmMarkDirty();
  mmToast('已改为「' + t.name + '」');
}

// ── 修改节点颜色 ──
// 改颜色 = 同时改边框色(body/stroke)和字色(label/fill)，与 AI 改 type 行为一致
// null/默认色 → 恢复类型默认色（边框+字都用类型色 t.color）
function mmChangeNodeColor(cell, color) {
  if (!cell) return;
  var data = cell.getData() || {};
  var t = MM_TYPES[data.type || 'idea'] || MM_TYPES.idea;
  var finalColor = color || t.color;
  cell.attr('body/stroke', finalColor);
  cell.attr('label/fill', finalColor);
  cell.setData(Object.assign({}, data, { color: color }));
  mmMarkDirty();
  mmToast(color ? '已设置颜色' : '已恢复默认色');
}

// ── 折叠/展开子节点 ──
function mmToggleCollapse(cell) {
  if (!cell || !cell.isNode || !cell.isNode()) return;
  var data = cell.getData() || {};
  var collapsed = !data.collapsed;
  cell.setData(Object.assign({}, data, { collapsed: collapsed }));
  // 获取所有子节点（递归）
  var children = _mmGetDescendants(cell);
  children.forEach(function (child) {
    try {
      child.setVisible(!collapsed);
      // 同时隐藏/显示关联的边
      var connectedEdges = _mmGraph.getConnectedEdges(child);
      connectedEdges.forEach(function (edge) {
        try { edge.setVisible(!collapsed); } catch (_) {}
      });
    } catch (_) {}
  });
  mmMarkDirty();
  mmToast(collapsed ? '已折叠 ' + children.length + ' 个子节点' : '已展开子节点');
}

// 获取节点的所有后代（递归，不含自身）
function _mmGetDescendants(cell) {
  if (!_mmGraph || !cell) return [];
  var result = [];
  var seen = {};
  seen[cell.id] = true;
  function collect(c) {
    var outEdges = _mmGraph.getOutgoingEdges(c) || [];
    outEdges.forEach(function (edge) {
      var tgtId = edge.getTargetCellId();
      if (tgtId && !seen[tgtId]) {
        seen[tgtId] = true;
        var tgt = _mmGraph.getCellById(tgtId);
        if (tgt) {
          result.push(tgt);
          collect(tgt);
        }
      }
    });
  }
  collect(cell);
  return result;
}

// ── 轻量 toast 提示（移动端用）──
var _mmToastTimer = null;
function mmToast(msg) {
  var old = document.getElementById('mmToast');
  if (old) old.remove();
  if (_mmToastTimer) { clearTimeout(_mmToastTimer); _mmToastTimer = null; }
  var t = document.createElement('div');
  t.id = 'mmToast';
  t.style.cssText = 'position:fixed;left:50%;top:60%;transform:translate(-50%,-50%);background:rgba(0,0,0,.85);color:#fff;padding:8px 16px;border-radius:6px;z-index:10010;font-size:12px;pointer-events:none;max-width:80vw;text-align:center';
  t.textContent = msg;
  document.body.appendChild(t);
  _mmToastTimer = setTimeout(function () { var el = document.getElementById('mmToast'); if (el) el.remove(); }, 2000);
}
