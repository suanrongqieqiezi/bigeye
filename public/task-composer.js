/* 输入框里的「任务模型」选择器。
 *
 * 2026-10-08 改版：
 *   1) 加可见把手（下滑/轻点关闭）—— 之前只有一条几乎看不见的细线；
 *   2) 加搜索框（按 名称 / id / 来源 / 描述 实时过滤，Esc 清空、再 Esc 关闭）；
 *   3) 按来源分组（组名 + 数量，当前模型所在源置顶），不再一长串混排；
 *   4) 打开不再等网络：有缓存先出面板，再后台刷新。
 *      服务端 /api/task-model 首次会很慢（本机 llama.cpp 探活要等满 2s 超时），
 *      所以「渲染」与「取数」必须解耦 —— 否则每次点开都是白等 2 秒。
 */
(function(){
  function tid(){return (typeof currentTopic!=='undefined'&&currentTopic)?currentTopic.id:'';}
  function esc(s){
    return String(s==null?'':s).replace(/[&<>"']/g,function(c){
      return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];
    });
  }

  var ICON_CLOCK='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 1.8"/></svg>';

var ICON_SEARCH='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="11" cy="11" r="7"/><path d="M20 20l-3.6-3.6"/></svg>';

  var style=document.createElement('style');
  style.textContent=`
/* ===== 发送行栅格：第2格=模型，第3格=工作区外访问（列定义是唯一真源，别在别处改） ===== */
footer .input-row{display:grid;grid-template-columns:34px auto auto minmax(0,1fr) 36px;gap:3px;padding:5px 8px;border:1px solid var(--border2);border-radius:18px;background:transparent}
footer .input-row textarea{grid-column:1/-1;grid-row:1;min-height:26px;max-height:120px;border:0!important;background:transparent!important;box-shadow:none!important;padding:2px 5px!important;width:100%;resize:none}
footer .input-row .attach-btn{grid-column:1;grid-row:2;height:30px;width:30px}
footer .input-row .send{grid-column:5;grid-row:2;height:30px;width:30px}
.task-model-btn{grid-column:2;grid-row:2;max-width:220px;text-align:left;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;border:0;background:transparent;color:var(--tdim);font:inherit;font-size:12px;padding:4px 8px;cursor:pointer}

/* ===== 工作区外访问按钮（紧邻模型按钮）=====
   状态只落 data-val：0=仅工作区（常规色），1=允许工作区外（警告色）。
   颜色走 var(--warn)：black/gray/white 三套主题都有定义，白主题下是深橙，可读。 */
.task-outside-btn{grid-column:3;grid-row:2;display:inline-flex;align-items:center;gap:4px;
  max-width:136px;border:0;background:transparent;color:var(--tdim);font:inherit;font-size:12px;
  padding:4px 8px;border-radius:9px;cursor:pointer;overflow:hidden;white-space:nowrap}
.task-outside-btn[hidden]{display:none}
.task-outside-btn:hover{background:var(--hover);color:var(--text)}
.task-outside-btn[data-val="1"]{color:var(--warn)}
.task-outside-btn[data-val="1"]:hover{background:rgba(var(--warn-rgb),.12);color:var(--warn)}
.task-outside-btn .to-ico{flex:0 0 auto;width:13px;height:13px;display:block}
.task-outside-btn .to-ico svg{width:100%;height:100%;display:block}
.task-outside-btn .to-txt{overflow:hidden;text-overflow:ellipsis}
.task-outside-btn .to-car{flex:0 0 auto;width:9px;height:9px;display:block;opacity:.7}
/* 白主题的 --warn 是浅橙(#c47f14)，配白底只有 3.04:1；单独加深到 ≈5.9:1，不动全局变量 */
html[data-theme="white"] .task-outside-btn[data-val="1"]{color:#8a5200}
html[data-theme="white"] .task-outside-btn[data-val="1"]:hover{color:#6f4200;background:rgba(138,82,0,.10)}

/* ===== 访问范围下拉：贴底向上弹（输入框在窗口最底部，往下必出屏）===== */
.to-menu{position:fixed;z-index:2300;min-width:264px;max-width:min(94vw,380px);
  background:var(--surface2);border:1px solid var(--border2);border-radius:14px;
  box-shadow:var(--shadow-lg);padding:6px 6px 10px;display:flex;flex-direction:column;gap:2px}
.to-menu[data-busy="1"]{pointer-events:none;opacity:.72}
.to-hd{padding:7px 10px 5px;font-size:11px;color:var(--tdim)}
.to-opt{display:flex;align-items:flex-start;gap:8px;width:100%;box-sizing:border-box;border:0;
  border-radius:10px;background:transparent;color:var(--text);text-align:left;font:inherit;
  padding:9px 10px;cursor:pointer}
.to-opt:hover{background:var(--hover)}
.to-opt.on{background:rgba(var(--accent-rgb),.13);box-shadow:inset 0 0 0 1px rgba(var(--accent-rgb),.5)}
.to-mark{flex:0 0 auto;width:15px;height:15px;margin-top:1px;color:var(--accent)}
.to-mark svg{width:100%;height:100%;display:block}
.to-body{flex:1 1 auto;min-width:0}
.to-tt{display:block;font-size:13px}
.to-opt.on .to-tt{font-weight:600}
.to-ds{display:block;font-size:11px;color:var(--tdim);line-height:1.5;margin-top:2px}

/* ===== 面板：竖向三段（把手/标题 · 搜索 · 列表滚动区） =====
   注意 .task-model-sheet>div 那条外部规则带 overflow-y:auto（特异度更高），
   不 !important 的话面板会自己滚、内部滚动区失效。 */
.tm-panel{display:flex!important;flex-direction:column!important;overflow:hidden!important;
  width:min(100%,560px);background:var(--surface2);box-shadow:0 -10px 40px rgba(0,0,0,.35);
  padding-bottom:env(safe-area-inset-bottom)}
/* 把手：可见的横杠 + 明确的抓取光标 */
.tm-panel>.task-sheet-grip{flex:0 0 auto;display:flex;align-items:center;justify-content:center;
  width:100%;height:24px;padding:0;border:0;background:transparent;cursor:grab;touch-action:none}
.tm-panel>.task-sheet-grip::after{content:'';width:46px;height:5px;border-radius:3px;background:var(--tdim);opacity:.65}
.tm-panel>.task-sheet-grip:hover::after{opacity:.95}
.tm-panel>.task-sheet-grip:active{cursor:grabbing}
.tm-panel>.task-sheet-grip:focus-visible{outline:2px solid var(--accent);outline-offset:-4px;border-radius:8px}
/* 标题行 */
.tm-head{flex:0 0 auto;display:flex;align-items:baseline;gap:8px;min-width:0;padding:2px 18px 8px}
.tm-title{font-size:13px;font-weight:600;color:var(--text);white-space:nowrap}
.tm-sub{flex:1 1 auto;min-width:0;text-align:right;font-size:11px;color:var(--tdim);
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
/* 搜索框 */
.tm-search{flex:0 0 auto;position:relative;padding:0 14px 8px}
.tm-search>svg{position:absolute;left:25px;top:10px;width:14px;height:14px;color:var(--tdim);pointer-events:none}
.tm-input{width:100%;height:34px;box-sizing:border-box;padding:0 32px;border-radius:10px;
  border:1px solid var(--border2);background:var(--surface3);color:var(--text);
  font:inherit;font-size:13px;outline:none;transition:border-color .15s}
.tm-input::placeholder{color:var(--tdim)}
.tm-input:focus{border-color:var(--accent)}
.tm-clr{position:absolute;right:22px;top:7px;width:20px;height:20px;padding:0;border:0;border-radius:50%;
  background:transparent;color:var(--tdim);font:inherit;font-size:13px;line-height:1;cursor:pointer;
  display:flex;align-items:center;justify-content:center}
.tm-clr:hover{color:var(--text);background:var(--hover)}
/* 列表滚动区 —— 只有这里滚 */
.tm-scroll{flex:1 1 auto;min-height:0;overflow-y:auto;overscroll-behavior:contain;
  padding:0 10px 12px;scrollbar-width:thin}
.tm-scroll::-webkit-scrollbar{width:6px}
.tm-scroll::-webkit-scrollbar-thumb{background:var(--border2);border-radius:3px}
.tm-scroll::-webkit-scrollbar-track{background:transparent}
/* 最近使用（置顶分组） */
.tm-group.tm-recent{margin-bottom:6px;padding-bottom:6px;
  border-bottom:1px solid var(--border2)}
.tm-recent .tm-gh{color:var(--accent)}
.tm-recent .tm-gh .tm-ico{display:inline-flex;width:12px;height:12px;margin-right:1px}
.tm-recent .tm-gh .tm-ico svg{width:100%;height:100%;display:block}

/* 分组 */
.tm-group{margin-top:2px}
.tm-gh{position:sticky;top:0;z-index:1;display:flex;align-items:center;gap:6px;
  padding:7px 8px 5px;background:var(--surface2);font-size:11px;color:var(--tdim)}
.tm-gl{font-weight:600;letter-spacing:.02em;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.tm-gc{margin-left:auto;font-size:10px;opacity:.75}
/* 条目 */
.tm-item{display:flex;align-items:center;gap:8px;width:100%;box-sizing:border-box;border:0;
  border-radius:9px;background:transparent;color:var(--text);text-align:left;
  font:inherit;font-size:13px;padding:9px 10px;cursor:pointer}
.tm-item:hover{background:var(--hover)}
.tm-item:disabled{opacity:.4;cursor:not-allowed}
.tm-item.on{background:rgba(var(--accent-rgb),.13);box-shadow:inset 0 0 0 1px rgba(var(--accent-rgb),.5)}
.tm-item.on .tm-nm{font-weight:600}
.tm-nm{flex:1 1 auto;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.tm-src{flex:0 0 auto;font-size:10px;color:var(--tdim);max-width:96px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.tm-tag{flex:0 0 auto;font-size:10px;padding:1px 6px;border-radius:6px;
  border:1px solid rgba(var(--accent-rgb),.5);color:var(--accent)}
.tm-item.tm-busy{opacity:.5;pointer-events:none}
.tm-empty{padding:28px 16px;text-align:center;color:var(--tdim);font-size:12px;line-height:2}
.tm-retry{border:1px solid var(--border2);background:var(--surface3);color:var(--text);
  border-radius:8px;padding:5px 14px;font:inherit;font-size:12px;cursor:pointer}
.tm-retry:hover{border-color:var(--accent);color:var(--accent)}

/* ===== 访问范围菜单：产出文件夹分节（2026-10-21）=====
   菜单比原来高了一倍，必须自己限高滚动 —— 输入框在窗口最底部，
   向上弹的面板一旦超过视口，顶部那几行会被截在屏幕外。 */
.to-menu{max-height:min(72vh,560px);overflow-y:auto;overscroll-behavior:contain;scrollbar-width:thin}
.to-menu::-webkit-scrollbar{width:6px}
.to-menu::-webkit-scrollbar-thumb{background:var(--border2);border-radius:3px}
.to-sep{height:1px;background:var(--border2);margin:6px 8px;opacity:.75;flex:0 0 auto}
/* 当前产出文件夹：只读展示，不能点（避免用户以为点了能改） */
.to-cur{display:flex;align-items:flex-start;gap:8px;padding:9px 10px;border-radius:10px;
  background:rgba(var(--accent-rgb),.10);box-shadow:inset 0 0 0 1px rgba(var(--accent-rgb),.34);cursor:default}
.to-cur .to-mark{color:var(--accent)}
.to-cur .to-tt{word-break:break-all}
.to-cur .to-ds{word-break:break-all}
.to-bad{display:inline-block;font-style:normal;font-size:10px;line-height:15px;padding:0 5px;
  border-radius:5px;border:1px solid currentColor;color:var(--warn);margin-left:5px;vertical-align:1px}
html[data-theme="white"] .to-bad{color:#8a5200}

/* ===== 产出文件夹选择器：面包屑 + 目录列表 + 底部确认 =====
   .task-model-sheet>div 有 max-height/overflow-y，这里必须 !important 抢回
   （与 .tm-panel 同一处坑），否则内部列表滚不动、整块面板自己滚。 */
.dp-panel{display:flex!important;flex-direction:column!important;overflow:hidden!important;
  width:min(100%,560px);height:auto;max-height:min(78dvh,620px);min-height:min(46dvh,340px);
  background:var(--surface2);
  box-shadow:0 -10px 40px rgba(0,0,0,.35);padding-bottom:env(safe-area-inset-bottom)}
.dp-panel>.task-sheet-grip{flex:0 0 auto;display:flex;align-items:center;justify-content:center;
  width:100%;height:24px;padding:0;border:0;background:transparent;cursor:grab;touch-action:none}
.dp-panel>.task-sheet-grip::after{content:'';width:46px;height:5px;border-radius:3px;background:var(--tdim);opacity:.65}
.dp-panel>.task-sheet-grip:hover::after{opacity:.95}
.dp-head{flex:0 0 auto;padding:2px 18px 8px}
.dp-title{font-size:13px;font-weight:600;color:var(--text)}
.dp-sub{font-size:11px;color:var(--tdim);line-height:1.5;margin-top:3px}
/* 面包屑：单行横滑，右端＝当前目录 */
.dp-crumbs{flex:0 0 auto;display:flex;align-items:center;gap:1px;padding:0 12px 8px;
  overflow-x:auto;overscroll-behavior-x:contain;white-space:nowrap;scrollbar-width:none}
.dp-crumbs::-webkit-scrollbar{display:none}
.dp-crumb{flex:0 0 auto;max-width:190px;border:0;background:transparent;color:var(--accent);
  font:inherit;font-size:12px;padding:3px 5px;border-radius:6px;cursor:pointer;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.dp-crumb:hover{background:var(--hover)}
.dp-crumb.active{color:var(--text);font-weight:600;cursor:default}
.dp-crumb.active:hover{background:transparent}
.dp-sep{flex:0 0 auto;color:var(--tdim);opacity:.5;font-size:11px;margin:0 1px}
.dp-list{flex:1 1 auto;min-height:0;overflow-y:auto;overscroll-behavior:contain;
  border-top:1px solid var(--border2);padding:4px 8px 10px;scrollbar-width:thin}
.dp-list::-webkit-scrollbar{width:6px}
.dp-list::-webkit-scrollbar-thumb{background:var(--border2);border-radius:3px}
.dp-item{display:flex;align-items:center;gap:9px;width:100%;box-sizing:border-box;border:0;
  border-radius:9px;background:transparent;color:var(--text);text-align:left;
  font:inherit;font-size:13px;padding:10px;cursor:pointer}
.dp-item:hover{background:var(--hover)}
.dp-item .dp-ico{flex:0 0 auto;width:16px;height:16px;color:var(--tdim)}
.dp-item .dp-ico svg{width:100%;height:100%;display:block}
.dp-item.up .dp-ico{color:var(--accent)}
.dp-item .dp-nm{flex:1 1 auto;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
/* 盘符剩余空间：字号比盘名小一档、右对齐贴在箭头上，像资源管理器而不是"按钮上的注解"。
   刻意不着色：这台机器 C/E/Q 三个盘剩余都不到 5%，按阈值告警会整列变橙，
   反而盖住盘名；"能不能放下"看数字就够了。等宽数字保证上下几位对齐。 */
.dp-item .dp-cap{flex:0 0 auto;font-size:10.5px;line-height:1.2;color:var(--tdim);opacity:.85;
  font-variant-numeric:tabular-nums;white-space:nowrap;letter-spacing:.01em;max-width:65%;
  overflow:hidden;text-overflow:ellipsis}
.dp-item .dp-go{flex:0 0 auto;width:11px;height:11px;color:var(--tdim);opacity:.55}
.dp-item .dp-go svg{width:100%;height:100%;display:block}
.dp-empty{padding:26px 16px;text-align:center;color:var(--tdim);font-size:12px;line-height:1.9}
.dp-err{margin:6px 8px 2px;padding:7px 10px;border-radius:8px;font-size:11px;line-height:1.65;
  background:rgba(var(--warn-rgb),.13);color:var(--warn)}
html[data-theme="white"] .dp-err{color:#8a5200}
.dp-foot{flex:0 0 auto;display:flex;flex-direction:column;gap:9px;
  border-top:1px solid var(--border2);padding:10px 16px 6px}
.dp-path{font-size:11px;color:var(--tdim);line-height:1.55;max-height:36px;overflow:hidden;
  word-break:break-all}
.dp-acts{display:flex;gap:8px;justify-content:flex-end;align-items:center}
/* 新建文件夹：靠左，与右侧「取消/用这个文件夹」分开一档（它改的是列表，不是确认动作）。
   此电脑那一级（没进盘）禁用 —— 盘符不是能建的目录。
   选择器必须写 .dp-acts .dp-btn-new（0,2,0）：.dp-btn 在后文定义且同权重，会反手把 padding 冲掉。
   🚫 这段是模板字符串，注释里不能出现反引号（会直接截断 CSS、把后面整段当 JS 执行）。 */
.dp-acts .dp-btn-new{margin-right:auto;display:inline-flex;align-items:center;gap:5px;padding:8px 12px}
.dp-acts .dp-btn-new svg{width:14px;height:14px;flex:0 0 auto}
.dp-acts .dp-btn-new:disabled{color:var(--tdim);border-color:var(--border2);background:transparent;
  cursor:not-allowed;opacity:.55}
.dp-acts .dp-btn-new:disabled:hover{border-color:var(--border2);color:var(--tdim)}
/* 就地建目录的输入行：插在列表最前面，跟着列表一起被下次 draw 覆盖（＝放弃） */
.dp-newrow{display:flex;align-items:center;gap:7px;padding:8px 10px;margin:2px 0 5px;border-radius:9px;
  background:rgba(var(--accent-rgb),.08);box-shadow:inset 0 0 0 1px rgba(var(--accent-rgb),.32)}
.dp-newrow .dp-ico{flex:0 0 auto;width:16px;height:16px;color:var(--accent)}
.dp-newrow .dp-ico svg{width:100%;height:100%;display:block}
.dp-newrow input{flex:1 1 auto;min-width:0;border:0;background:transparent;color:var(--text);
  font:inherit;font-size:13px;padding:2px 0;outline:none;-webkit-appearance:none}
.dp-newrow input::placeholder{color:var(--tdim)}
.dp-newrow .dp-btn{flex:0 0 auto;font-size:12px;padding:5px 10px;border-radius:8px}
.dp-btn{border:1px solid var(--border2);background:var(--surface3);color:var(--text);
  border-radius:10px;font:inherit;font-size:13px;padding:8px 16px;cursor:pointer}
.dp-btn:hover{border-color:var(--accent);color:var(--accent)}
.dp-btn-ghost{background:transparent;color:var(--tdim)}
.dp-btn-pri{background:linear-gradient(135deg,var(--accent),var(--accent2));color:#04241f;
  border-color:transparent;font-weight:600;box-shadow:0 3px 12px rgba(var(--accent-rgb),.26)}
.dp-btn-pri:hover{color:#04241f;filter:brightness(1.05)}
/* 禁用态用"退成普通灰底"而不是只降透明度：半透明的青绿在深色底上仍然像个
   可点的主按钮（截图实测），而这里禁用代表"还没进任何目录"。 */
.dp-btn-pri:disabled{background:var(--surface3);color:var(--tdim);border-color:var(--border2);
  cursor:not-allowed;filter:none;box-shadow:none;opacity:1}`;
  document.head.appendChild(style);

  function closeSheet(){
    live=null;
    if(window.__closeModelSheets) window.__closeModelSheets();
    else Array.prototype.forEach.call(document.querySelectorAll('.task-model-sheet'),function(s){s.remove();});
  }

  var row=document.querySelector('footer .input-row'); if(!row) return;
  var button=document.createElement('button');
  button.className='task-model-btn'; button.type='button'; button.textContent='选择模型 ▾';
  row.appendChild(button);

  /* ── 工作区外访问（任务级 allow_outside）────────────────────────────
     真源在服务端 topic meta：GET /api/workspace 读、POST /api/workspace 写。
     刻意不做 localStorage 缓存 —— 同一个任务可能被另一个客户端（手机 / 守门员侧）
     改过，本地缓存会撒谎。读不到就停在 unknown，绝不假装成"仅工作区"。 */
  var oIconLock='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="4" y="10.5" width="16" height="10" rx="2.5"/><path d="M8 10.5V7a4 4 0 0 1 8 0v3.5"/></svg>';
  var oIconWarn='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M12 7.5v5.5"/><path d="M12 16.6h.01"/></svg>';
  var oIconCaret='<svg viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M3 4.5 6 7.5 9 4.5"/></svg>';
  var oIconTick='<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 8.5 6.3 12 13 4.6"/></svg>';
  var oIconDir='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 7.6A2.6 2.6 0 0 1 5.6 5h3.7l2 2.4h7.1A2.6 2.6 0 0 1 21 10v6.4A2.6 2.6 0 0 1 18.4 19H5.6A2.6 2.6 0 0 1 3 16.4z"/></svg>';
  var dpIconFolder='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 7.6A2.6 2.6 0 0 1 5.6 5h3.7l2 2.4h7.1A2.6 2.6 0 0 1 21 10v6.4A2.6 2.6 0 0 1 18.4 19H5.6A2.6 2.6 0 0 1 3 16.4z"/></svg>';
  var dpIconDrive='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M4.2 13.6 6 6.9A2.1 2.1 0 0 1 8 5.4h8a2.1 2.1 0 0 1 2 1.5l1.8 6.7"/><rect x="3" y="13.4" width="18" height="5.4" rx="1.8"/><path d="M6.9 16.1h.01"/></svg>';
  var dpIconSwap='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M4 8h13l-3-3"/><path d="M20 16H7l3 3"/></svg>';
  var dpIconTrash='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M4.5 7h15"/><path d="M9.5 7V5.6A1.6 1.6 0 0 1 11.1 4h1.8a1.6 1.6 0 0 1 1.6 1.6V7"/><path d="M6.3 7l.8 11.2A2 2 0 0 0 9.1 20h5.8a2 2 0 0 0 2-1.8L17.7 7"/></svg>';
  var dpIconUp='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 19V5"/><path d="M6 11l6-6 6 6"/></svg>';
  var dpIconArrow='<svg viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M4.5 3 7.5 6 4.5 9"/></svg>';
  // 新建文件夹（文件夹 + 加号），16px 用
  var dpIconNewDir='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 7.6A2.6 2.6 0 0 1 5.6 5h3.7l2 2.4h7.1A2.6 2.6 0 0 1 21 10v6.4A2.6 2.6 0 0 1 18.4 19H5.6A2.6 2.6 0 0 1 3 16.4z"/><path d="M12 11.4v5.2"/><path d="M9.4 14h5.2"/></svg>';

  /* 容量显示（2026-10-23）：产出文件夹选的是"文件往哪儿写"，能不能放下比盘名更该看见。
     服务端只在"此电脑"这一级返回 total/free；拿不到就整块不画（绝不用 0 冒充满盘）。 */
  function fmtCap(n){
    if(typeof n!=='number' || !isFinite(n) || n<=0) return '';
    var u=['B','KB','MB','GB','TB','PB'], i=0, v=n;
    while(v>=1024 && i<u.length-1){ v/=1024; i++; }
    return (v>=10 ? Math.round(v) : v.toFixed(1)) + ' ' + u[i];
  }
  function dpCapHtml(r){
    var free=fmtCap(r&&r.free), total=fmtCap(r&&r.total);
    if(!free) return '';
    var txt=free+' 可用'+(total?' / 共 '+total:'');
    return '<span class="dp-cap" title="'+esc(txt)+'">'+esc(txt)+'</span>';
  }

  var oBtn=document.createElement('button');
  oBtn.type='button'; oBtn.className='task-outside-btn'; oBtn.dataset.val='0'; oBtn.hidden=true;
  row.appendChild(oBtn);

  var oState={tid:'',on:false,known:false,busy:false,out:'',outOk:true};
  var oMenu=null, oLastTid='', oRO=null;

  /* 产出文件夹的显示名：取最后一段（盘符根 'D:\' → 'D:'）。 */
  function outName(p){
    if(!p) return '';
    var s=String(p).replace(/[\\/]+$/,'');
    var i=Math.max(s.lastIndexOf('\\'), s.lastIndexOf('/'));
    var n=i>=0?s.slice(i+1):s;
    return n||s||String(p);
  }

  function oPaint(){
    var on=!!oState.on, out=oState.out||'';
    oBtn.dataset.val=on?'1':'0';
    if(!oState.known && !out){
      oBtn.innerHTML='<span class="to-ico">'+oIconLock+'</span><span class="to-txt">工作区访问</span><span class="to-car">'+oIconCaret+'</span>';
      oBtn.title='访问范围状态未知（没能读到），点开可重新读取并修改';
      return;
    }
    /* 设了产出文件夹就把文件夹名直接顶到这个位置 —— 用户在这个位置最想确认
       的是"这活儿的产出落在哪"。是否越权靠图标形状 + --warn 色表达，
       两者不会互相遮蔽。 */
    var txt = out ? outName(out) : (on?'允许工作区外':'仅工作区');
    var ico = out ? (on?oIconWarn:oIconDir) : (on?oIconWarn:oIconLock);
    oBtn.innerHTML='<span class="to-ico">'+ico+'</span>'
      +'<span class="to-txt">'+esc(txt)+'</span>'
      +'<span class="to-car">'+oIconCaret+'</span>';
    var t = on ? 'AI 可读写工作区之外的文件' : 'AI 只能读写本任务工作区';
    if(out) t += '；产出文件夹: '+out+(oState.outOk?'':'（当前不可访问）');
    oBtn.title=t+'（点此修改）';
  }
  oPaint();

  function oRead(id){
    return fetch('/api/workspace?topic_id='+encodeURIComponent(id))
      .then(function(r){ return r.json(); })
      .then(function(d){
        if(!d || d.error) throw Error((d&&d.error)||'读取失败');
        if(oState.tid!==id) return;               // 已切任务，丢弃
        oState.on=!!d.allow_outside; oState.known=true;
        oState.out=d.output_dir||'';
        // 目录被删/拔盘时服务端会明确说 ok=false：按钮照旧显示它（用户设过），
        // 但 title 与菜单里都标出来，不让它冒充"好用的目录"。
        oState.outOk=(d.output_dir_ok!==false);
        oPaint();
      })
      .catch(function(){ /* 停在 unknown：问不到 ≠ 没开启 */ });
  }

  function oWrite(on){
    var id=tid(); if(!id) return Promise.reject(Error('请先选择任务'));
    return fetch('/api/workspace',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({topic_id:id, allow_outside:!!on})})
      .then(function(r){ return r.json().then(function(d){ return {ok:r.ok,d:d}; }); })
      .then(function(res){
        if(!res.ok || !res.d || !res.d.success) throw Error((res.d&&res.d.error)||'设置失败');
        oState.tid=id; oState.on=!!on; oState.known=true; oPaint();
        try{ if(typeof fbAllowOutside!=='undefined') fbAllowOutside=!!on; }catch(e){}
      });
  }

  function oClose(){
    if(!oMenu) return;
    oMenu.remove(); oMenu=null;
    document.removeEventListener('mousedown',oDocDown,true);
    document.removeEventListener('keydown',oEsc,true);
    document.removeEventListener('click',oPick,true);
    window.removeEventListener('resize',oClose);
    window.removeEventListener('scroll',oClose,true);
    if(oRO){ oRO.disconnect(); oRO=null; }
  }
  function oDocDown(ev){
    if(oMenu && oMenu.contains(ev.target)) return;
    if(oBtn.contains(ev.target)) return;          // 点自己交给 onclick（它会 toggle）
    oClose();
  }
  function oEsc(ev){ if(ev.key==='Escape'){ ev.stopPropagation(); oClose(); } }

  function oOpt(v,title,desc){
    var on=oState.known && oState.on===v;
    return '<button type="button" class="to-opt'+(on?' on':'')+'" data-v="'+(v?1:0)+'">'
      +'<span class="to-mark">'+(on?oIconTick:'')+'</span>'
      +'<span class="to-body"><span class="to-tt">'+title+'</span><span class="to-ds">'+desc+'</span></span>'
      +'</button>';
  }
  function oRender(){
    if(!oMenu) return;
    var out=oState.out||'';
    var html='<div class="to-hd">工作区外访问 · 只对本任务生效</div>'
      +oOpt(false,'仅限工作区','AI 读写文件、Shell 命令都限制在本任务工作区内。')
      +oOpt(true,'允许工作区外','AI 可读写工作区之外的文件，Shell 也能访问整个磁盘。')
      +'<div class="to-sep"></div>'
      +'<div class="to-hd">产出文件夹 · 工作区不变，只是多一个可读写目录</div>';
    if(out){
      html+='<div class="to-cur"><span class="to-mark">'+oIconDir+'</span>'
        +'<span class="to-body"><span class="to-tt">'+esc(outName(out))
        +(oState.outOk?'':'<em class="to-bad">不可用</em>')+'</span>'
        +'<span class="to-ds">'+esc(out)+'</span></span></div>'
        +'<button type="button" class="to-opt" data-act="pick">'
        +'<span class="to-mark">'+dpIconSwap+'</span>'
        +'<span class="to-body"><span class="to-tt">更换文件夹…</span></span></button>'
        +'<button type="button" class="to-opt" data-act="clear">'
        +'<span class="to-mark">'+dpIconTrash+'</span>'
        +'<span class="to-body"><span class="to-tt">清除产出文件夹</span></span></button>';
    }else{
      html+='<button type="button" class="to-opt" data-act="pick">'
        +'<span class="to-mark">'+oIconDir+'</span>'
        +'<span class="to-body"><span class="to-tt">选择本地项目…</span>'
        +'<span class="to-ds">浏览这台电脑的文件夹，选定后 AI 也能读写它（工作区不变）</span></span></button>';
    }
    oMenu.innerHTML=html;
  }

  /* ── 产出文件夹：面包屑目录选择器 ────────────────────────────
     为什么不调系统目录对话框：桌面壳(pywebview)与手机浏览器共用同一套前端，
     原生对话框在手机/远程访问里根本不存在；而且这里要一眼看清"选的是哪一级"，
     原生对话框给不了可回跳的面包屑。 */
  var dpCover=null;

  function pickerClose(){
    dpCover=null;
    if(window.__closeModelSheets) window.__closeModelSheets();
    else Array.prototype.forEach.call(document.querySelectorAll('.task-model-sheet'),function(s){s.remove();});
  }

  function openPicker(){
    var id=tid(); if(!id){ alert('请先选择任务'); return; }
    oClose();                                  // 菜单与弹层不能并存
    var start=oState.out||'';
    if(!start){ try{ start=localStorage.getItem('dayan_last_output_dir')||''; }catch(e){ start=''; } }

    var cover=document.createElement('div'); cover.className='task-model-sheet';
    var panel=document.createElement('div'); panel.className='dp-panel';
    panel.innerHTML=
      '<button type="button" class="task-sheet-grip" aria-label="下滑或点击关闭文件夹选择"></button>'
      +'<div class="dp-head"><div class="dp-title">选择产出文件夹</div>'
      +'<div class="dp-sub">工作区不变，AI 额外可读写这个目录</div></div>'
      +'<div class="dp-crumbs"></div>'
      +'<div class="dp-list"></div>'
      +'<div class="dp-foot"><div class="dp-path">—</div>'
      +'<div class="dp-acts">'
      +'<button type="button" class="dp-btn dp-btn-ghost dp-btn-new" data-a="mkdir" disabled>'
      +dpIconNewDir+'新建文件夹</button>'
      +'<button type="button" class="dp-btn dp-btn-ghost" data-a="cancel">取消</button>'
      +'<button type="button" class="dp-btn dp-btn-pri" data-a="use">用这个文件夹</button>'
      +'</div></div>';
    cover.appendChild(panel);
    cover.addEventListener('click',function(ev){ if(ev.target===cover) pickerClose(); });
    document.body.appendChild(cover);
    dpCover=cover;

    var crumbsEl=panel.querySelector('.dp-crumbs'),
        listEl=panel.querySelector('.dp-list'),
        pathEl=panel.querySelector('.dp-path'),
        newBtn=panel.querySelector('[data-a="mkdir"]'),
        useBtn=panel.querySelector('[data-a="use"]');
    var seq=0, cur='', newRow=null;

    function setPath(p){
      /* 此电脑（还没进任何盘）时底部不该再重复一次"此电脑"，改成状态说明；
         同时把确认按钮退成灰底 —— 空目录上点"用这个文件夹"没有意义。
         新建也一样：盘符列表不是目录，没进盘就没地方建。 */
      pathEl.textContent=p||'还没有进入任何文件夹';
      pathEl.title=p||'';
      useBtn.disabled=!p;
      newBtn.disabled=!p;
    }

    /* ── 就地新建文件夹 ─────────────────────────────────────
       为什么不 prompt：手机端系统弹窗在桌面壳里样式不可控，而且敲错一个字符就要重来。
       输入行直接插在列表最前面：看得见"建在哪个目录里"（就是当前目录），
       列表下次重画时它自然消失 —— 等于放弃，不用另存一份状态。 */
    function closeNewRow(){
      if(newRow && newRow.parentNode) newRow.parentNode.removeChild(newRow);
      newRow=null;
    }
    function openNewRow(){
      if(!cur) return;                       // 此电脑层没有可建目录
      if(newRow){
        var f0=newRow.querySelector('input');
        if(f0){ try{ f0.focus(); if(f0.select) f0.select(); }catch(e){} }
        return;
      }
      var r=document.createElement('div'); r.className='dp-newrow';
      r.innerHTML='<span class="dp-ico">'+dpIconNewDir+'</span>'
        +'<input type="text" maxlength="120" spellcheck="false" autocomplete="off" placeholder="文件夹名">'
        +'<button type="button" class="dp-btn dp-btn-pri" data-a="mkdo">创建</button>'
        +'<button type="button" class="dp-btn dp-btn-ghost" data-a="mkcancel">取消</button>';
      listEl.insertBefore(r, listEl.firstChild);
      listEl.scrollTop=0;                    // 输入行在顶部，先滚上去
      newRow=r;
      var inp=r.querySelector('input');
      inp.value='新建文件夹';                // 与资源管理器一致：预填默认名，全选可直接回车
      inp.addEventListener('keydown',function(ev){
        /* 🔴 必须 stopPropagation：composer-floating.js 在 cover 上挂了 keydown(Esc)→关整个面板，
           冒泡上去的话"取消这一行"会变成"把选择器整个关掉"（真机表现＝按 Esc 面板没了）。 */
        if(ev.key==='Enter'){ ev.preventDefault(); ev.stopPropagation(); submitNew(); }
        else if(ev.key==='Escape'){ ev.preventDefault(); ev.stopPropagation(); closeNewRow(); }
      });
      // 移动端软键盘：等面板把输入行布局完再 focus，否则键盘弹起时机早于定位会滚错
      setTimeout(function(){ try{ inp.focus(); inp.select(); }catch(e){} },30);
    }
    function submitNew(){
      if(!cur || !newRow) return;
      var inp=newRow.querySelector('input'); if(!inp) return;
      var name=(inp.value||'').trim();
      if(!name){ try{ inp.focus(); }catch(e){} return; }
      var btns=newRow.querySelectorAll('[data-a]');
      Array.prototype.forEach.call(btns,function(b){ b.disabled=true; });
      fetch('/api/dir-mkdir',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({parent:cur, name:name})})
        .then(function(r){ return r.json().then(function(d){ return {ok:r.ok,d:d||{}}; }); })
        .then(function(res){
          if(!res.ok || !res.d || !res.d.success) throw Error((res.d&&res.d.error)||'新建失败');
          var np=res.d.path||'';
          closeNewRow();
          /* 建完直接进去：选择器里"当前目录"就是"将要用的目录"，
             进去才等于选中它；不自动进入的话用户还要再点一次。 */
          load(np||cur);
        })
        .catch(function(e){
          if(newRow){
            Array.prototype.forEach.call(btns,function(b){ b.disabled=false; });
            // 就地报错、保留输入内容：改个名就能重试
            try{ inp.value=name; inp.focus(); if(inp.select) inp.select(); }catch(e2){}
          }
          alert('新建失败：'+e.message);
        });
    }

    function drawCrumbs(d){
      var list=d.crumbs||[];
      var html='<button type="button" class="dp-crumb'+(d.path?'':' active')+'" data-p="">此电脑</button>';
      for(var i=0;i<list.length;i++){
        html+='<span class="dp-sep">›</span>'
          +'<button type="button" class="dp-crumb'+(i===list.length-1?' active':'')+'"'
          +' data-p="'+esc(list[i].path)+'" title="'+esc(list[i].path)+'">'+esc(list[i].name)+'</button>';
      }
      crumbsEl.innerHTML=html;
      crumbsEl.scrollLeft=crumbsEl.scrollWidth;   // 面包屑右端＝当前目录，打开即见
    }

    function draw(d){
      var html='';
      if(d.error) html+='<div class="dp-err">'+esc(d.error)+'</div>';
      if(!d.path){
        var roots=d.roots||[];
        for(var i=0;i<roots.length;i++){
          html+='<button type="button" class="dp-item" data-p="'+esc(roots[i].path)+'">'
            +'<span class="dp-ico">'+dpIconDrive+'</span>'
            +'<span class="dp-nm">'+esc(roots[i].name)+'</span>'
            +dpCapHtml(roots[i])
            +'<span class="dp-go">'+dpIconArrow+'</span></button>';
        }
        if(!roots.length) html+='<div class="dp-empty">没有可访问的磁盘</div>';
        listEl.innerHTML=html; setPath(''); return;
      }
      html+='<button type="button" class="dp-item up" data-p="'+esc(d.parent||'')+'">'
        +'<span class="dp-ico">'+dpIconUp+'</span><span class="dp-nm">上一级</span></button>';
      var dirs=d.dirs||[];
      for(var j=0;j<dirs.length;j++){
        html+='<button type="button" class="dp-item" data-p="'+esc(dirs[j].path)+'">'
          +'<span class="dp-ico">'+dpIconFolder+'</span>'
          +'<span class="dp-nm">'+esc(dirs[j].name)+'</span>'
          +'<span class="dp-go">'+dpIconArrow+'</span></button>';
      }
      if(!dirs.length && !d.error) html+='<div class="dp-empty">这个目录里没有子文件夹</div>';
      listEl.innerHTML=html;
      listEl.scrollTop=0;
      setPath(d.path||'');
    }

    function load(p){
      var my=++seq;
      closeNewRow();                 // 换目录＝放弃未提交的新建行（它挂在旧目录上）
      listEl.innerHTML='<div class="dp-empty">加载中…</div>';
      fetch('/api/dir-picker?path='+encodeURIComponent(p||''))
        .then(function(r){ return r.json(); })
        .then(function(d){
          if(my!==seq || !dpCover) return;
          if(d && d.error && !d.path){
            /* 目录打不开（被删/无权限）：留在原地报错，绝不静默跳到别处 ——
               否则用户以为选的是 D:\作品，实际存成了工作区。 */
            listEl.innerHTML='<div class="dp-empty">'+esc(d.error)+'</div>';
            return;
          }
          cur=(d&&d.path)||'';
          drawCrumbs(d||{}); draw(d||{});
        })
        .catch(function(e){
          if(my!==seq) return;
          listEl.innerHTML='<div class="dp-empty">加载失败：'+esc(e.message)+'</div>';
        });
    }

    function save(){
      if(!cur){ alert('请先进入一个文件夹'); return; }
      /* 有没提交的新建行就先拦下：否则用户敲了名字直接点确认，会被静默存成"上一层目录"，
         建出来的文件夹反而没被选中（他很可能是想"建这个、用它"）。 */
      if(newRow){ alert('还有一个没创建完的文件夹：先点「创建」或「取消」。'); return; }
      useBtn.disabled=true;
      fetch('/api/workspace',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({topic_id:id, output_dir:cur})})
        .then(function(r){ return r.json().then(function(d){ return {ok:r.ok,d:d}; }); })
        .then(function(res){
          if(!res.ok || !res.d || !res.d.success) throw Error((res.d&&res.d.error)||'保存失败');
          oState.tid=id; oState.out=cur; oState.outOk=true;
          try{ localStorage.setItem('dayan_last_output_dir', cur); }catch(e){}
          oPaint(); pickerClose();
          /* 通知文件浏览器：给它开一个以该文件夹命名的标签（跨文件唯一入口）。
             失败不影响保存结果 —— 标签是"顺手多看一眼"，不是保存的前置条件。 */
          try{ if(window.__onOutputDirChanged) window.__onOutputDirChanged(cur); }catch(e){}
        })
        .catch(function(e){
          useBtn.disabled=false;
          alert('保存失败：'+e.message);
        });
    }

    panel.addEventListener('click',function(ev){
      var t=ev.target; if(!t || !t.closest) return;
      var act=t.closest('[data-a]');
      if(act){
        var a=act.getAttribute('data-a');
        if(a==='cancel'){ pickerClose(); return; }
        if(a==='mkdir'){ openNewRow(); return; }
        if(a==='mkcancel'){ closeNewRow(); return; }
        if(a==='mkdo'){ submitNew(); return; }
        if(a==='use'){ save(); }
        return;
      }
      var jump=t.closest('[data-p]');
      if(jump) load(jump.getAttribute('data-p'));
    });

    load(start);
  }

  function oClearOut(){
    var id=tid(); if(!id) return;
    oState.busy=true; if(oMenu) oMenu.dataset.busy='1';
    fetch('/api/workspace',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({topic_id:id, output_dir:''})})
      .then(function(r){ return r.json().then(function(d){ return {ok:r.ok,d:d}; }); })
      .then(function(res){
        oState.busy=false;
        if(!res.ok || !res.d || !res.d.success) throw Error((res.d&&res.d.error)||'清除失败');
        oState.out=''; oState.outOk=true; oPaint(); oClose();
        try{ if(window.__onOutputDirChanged) window.__onOutputDirChanged(''); }catch(e){}
      })
      .catch(function(e){
        oState.busy=false;
        if(oMenu) delete oMenu.dataset.busy;
        alert('清除失败：'+e.message);
      });
  }

  function oAct(name){
    if(oState.busy) return;
    if(name==='pick'){ openPicker(); return; }
    if(name==='clear'){ oClearOut(); }
  }
  function oPosition(){
    if(!oMenu) return;
    var r=oBtn.getBoundingClientRect(), mw=oMenu.offsetWidth;
    /* 基准取"输入框顶边"而不是按钮顶边：按钮本身长在输入框内部，以它为基准向上弹，
       菜单底边必然压住输入框上半截，看起来像菜单被切了一刀（截图实测）。
       顺便按可用高度限一次 max-height，太高的菜单就内部滚动，绝不顶出屏幕。 */
    var f=document.querySelector('#mainPanel > footer')||document.querySelector('footer');
    var baseTop=f?Math.min(r.top, f.getBoundingClientRect().top):r.top;
    var avail=baseTop-16;
    oMenu.style.maxHeight=Math.round(Math.max(180, Math.min(560, avail)))+'px';
    var mh=oMenu.offsetHeight;
    var left=Math.min(Math.max(8,r.left), Math.max(8,window.innerWidth-mw-8));
    var top=baseTop-mh-10;
    if(top<8) top=Math.min(window.innerHeight-mh-8, r.bottom+10);   // 上方放不下就翻到下方
    oMenu.style.left=Math.round(left)+'px';
    oMenu.style.top=Math.round(Math.max(8,top))+'px';
  }
  function oOpen(){
    var id=tid(); if(!id){ alert('请先选择任务'); return; }
    var m=document.createElement('div'); m.className='to-menu'; m.style.visibility='hidden';
    document.body.appendChild(m);
    oMenu=m; oRender(); oPosition(); m.style.visibility='';
    document.addEventListener('mousedown',oDocDown,true);
    document.addEventListener('keydown',oEsc,true);
    document.addEventListener('click',oPick,true);
    window.addEventListener('resize',oClose);
    window.addEventListener('scroll',oClose,true);
    /* 锚点跟随：模型按钮宽度一变，访问按钮位置就变，面板得跟着走 */
    if(window.ResizeObserver){
      oRO=new ResizeObserver(function(){ oPosition(); });
      [document.querySelector('.task-model-btn'),oBtn].forEach(function(n){ if(n) oRO.observe(n); });
    }
    if(!oState.known || oState.tid!==id) oRead(id).then(function(){ oRender(); oPosition(); });
    /* 点击那一帧量到的位置可能还会变（模型名变长/附件预览等都会重排整行），
       所以下一帧再校一次、180ms 后再兜一次 —— 面板不能停在旧坐标上。 */
    requestAnimationFrame(function(){ if(oMenu===m) oPosition(); });
    setTimeout(function(){ if(oMenu===m) oPosition(); }, 180);
  }

  oBtn.onclick=function(ev){
    ev.preventDefault(); ev.stopPropagation();
    if(oMenu) oClose(); else oOpen();
  };

  /* 切任务重新对账（含"没选任务"→隐藏按钮）；失败重试由下一次切任务承担 */
  function oTick(){
    var id=tid();
    if(id===oLastTid) return;
    oLastTid=id;
    oClose();
    if(dpCover) pickerClose();                 // 选择器属于旧任务，切走就收掉
    oState.tid=id; oState.known=false; oState.on=false; oState.busy=false;
    oState.out=''; oState.outOk=true;          // 先清空：别让上一个任务的文件夹名闪在新任务上
    oBtn.hidden=!id;
    oPaint();
    if(id) oRead(id);
  }

  /* 选项点击：走捕获阶段委托，面板重建后无需重绑 */
  function oSelect(ev){
    if(!oMenu) return;
    var b=ev.target && ev.target.closest ? ev.target.closest('.to-opt') : null;
    if(!b || oState.busy) return;
    var want=b.getAttribute('data-v')==='1';
    if(oState.known && want===oState.on){ oClose(); return; }
    oState.busy=true; oMenu.dataset.busy='1';
    oWrite(want).then(function(){ oState.busy=false; oClose(); })
      .catch(function(err){
        oState.busy=false;
        if(oMenu){ delete oMenu.dataset.busy; }
        alert('设置失败：'+err.message);
      });
  }
  /* 选项点击（捕获阶段）：挂/摘随面板，不留常驻监听 */
  function oPick(ev){
    if(!oMenu) return;
    var t=ev.target;
    if(!t || !t.closest) return;
    var act=t.closest('[data-act]');            // 产出文件夹那一节的按钮
    if(act){ oAct(act.getAttribute('data-act')); return; }
    if(t.closest('.to-opt')) oSelect(ev);
  }

  setInterval(oTick,800);
  oTick();

  /* ── 数据层 ─────────────────────────────────────────────
     按任务 id 缓存最近几条：切回刚看过的任务时面板同样秒开。 */
  var CACHE_TTL=30000;      // 30s 内不重复请求（用户连点几次时几乎零延迟）
  var MAX_CACHE=8;
  var cacheMap={}, shown='', pending=null, openSeq=0, lastFail=0, live=null;

  function cacheOf(id){ var e=cacheMap[id]; return e?e.d:null; }
  function cacheFresh(id){ var e=cacheMap[id]; return !!e && (Date.now()-e.at)<CACHE_TTL; }
  function putCache(id,d){
    cacheMap[id]={d:d,at:Date.now()};
    var ks=Object.keys(cacheMap);
    for(var i=0;i<ks.length-MAX_CACHE;i++) delete cacheMap[ks[i]];
  }
  function applyLabel(d){
    if(!d) return;
    button.textContent=(d.model||'选择模型')+' ▾';
    button.title='仅此任务：'+(d.provider||'');
  }

  function load(force){
    var id=tid(); if(!id) return Promise.resolve(null);
    if(pending && pending.__id===id) return pending;          // 在飞的请求复用，连点不重复发
    if(!force && cacheFresh(id)) return Promise.resolve(cacheOf(id));
    var p=(async function(){
      try{
        var r=await fetch('/api/task-model?topic_id='+encodeURIComponent(id));
        var d=await r.json();
        if(tid()!==id) return null;                            // 已切任务，丢弃
        if(!r.ok || !d || d.error) throw Error((d&&d.error)||('HTTP '+r.status));
        putCache(id,d); shown=id; applyLabel(d);
        return d;
      }catch(e){
        return null;
      }finally{
        if(pending===p) pending=null;
      }
    })();
    p.__id=id; pending=p;
    return p;
  }

  /* ── 渲染层 ───────────────────────────────────────────── */
  function groupLabel(p,m){
    if(m && m.group) return m.group;                          // 第三方源：服务端给的显示名
    if(typeof PROVIDER_NAMES!=='undefined' && PROVIDER_NAMES[p]) return PROVIDER_NAMES[p];
    try{ if(typeof cpSourceLabel==='function'){ var l=cpSourceLabel(p); if(l) return l; } }catch(e){}
    return p||'其他';
  }

  function buildList(d,q){
    var models=(d&&d.models)||[];
    if(!models.length) return '<div class="tm-empty">暂无模型</div>';
    var ql=String(q||'').trim().toLowerCase();
    var order=[], byKey={};
    for(var i=0;i<models.length;i++){
      var m=models[i], key=m.provider||'unknown';
      if(!byKey[key]){ byKey[key]={key:key,label:groupLabel(key,m),items:[]}; order.push(key); }
      byKey[key].items.push(m);
    }
    // 当前模型所在的源置顶：换完任务最常见的动作就是确认/微调当前模型
    var curKey=null;
    for(var c=0;c<models.length;c++){ if(models[c].current){ curKey=models[c].provider||'unknown'; break; } }
    if(curKey){ var ci=order.indexOf(curKey); if(ci>0){ order.splice(ci,1); order.unshift(curKey); } }

    var html='', total=0;

    // 最近使用：跨任务记录，置顶一行内直达 —— 切模型的高频动在这里，不用翻源。
    var recent=(d&&d.recent)||[];
    if(recent.length){
      var rh=[];
      for(var ri=0;ri<recent.length;ri++){
        var rm=recent[ri];
        if(!ql || (String(rm.name||'')+' '+String(rm.id||'')+' '+groupLabel(rm.provider||'unknown',rm)).toLowerCase().indexOf(ql)>=0) rh.push(rm);
      }
      if(rh.length){
        total+=rh.length;
        html+='<div class="tm-group tm-recent"><div class="tm-gh"><span class="tm-ico">'+ICON_CLOCK+'</span>'
            +'<span class="tm-gl">最近使用</span><span class="tm-gc">'+rh.length+'</span></div>';
        for(var rj=0;rj<rh.length;rj++){
          var rn=rh[rj];
          html+='<button type="button" class="tm-item'+(rn.current?' on':'')+'"'+(rn.available===false?' disabled':'')
               +' data-p="'+esc(rn.provider)+'" data-m="'+esc(rn.id)+'">'
               +'<span class="tm-nm">'+esc(rn.name||rn.id)+'</span>'
               +'<span class="tm-src">'+esc(groupLabel(rn.provider||'unknown',rn))+'</span>'
               +(rn.current?'<span class="tm-tag">当前</span>':'')
               +'</button>';
        }
        html+='</div>';
      }
    }

    for(var g=0;g<order.length;g++){
      var grp=byKey[order[g]], hit=[];
      for(var k=0;k<grp.items.length;k++){
        var it=grp.items[k];
        if(!ql || (String(it.name||'')+' '+String(it.id||'')+' '+grp.label+' '+String(it.desc||'')).toLowerCase().indexOf(ql)>=0) hit.push(it);
      }
      if(!hit.length) continue;
      total+=hit.length;
      html+='<div class="tm-group"><div class="tm-gh"><span class="tm-gl">'+esc(grp.label)+'</span><span class="tm-gc">'+hit.length+'</span></div>';
      for(var h=0;h<hit.length;h++){
        var mm=hit[h];
        html+='<button type="button" class="tm-item'+(mm.current?' on':'')+'"'+(mm.available===false?' disabled':'')
             +' data-p="'+esc(mm.provider)+'" data-m="'+esc(mm.id)+'">'
             +'<span class="tm-nm">'+esc(mm.name||mm.id)+'</span>'
             +(mm.current?'<span class="tm-tag">当前</span>':'')
             +'</button>';
      }
      html+='</div>';
    }
    if(!total) return '<div class="tm-empty">没有匹配「'+esc(q)+'」的模型</div>';
    return html;
  }

  function makeSheet(id,data,seq){
    closeSheet();
    var cover=document.createElement('div'); cover.className='task-model-sheet';
    var panel=document.createElement('div'); panel.className='tm-panel';
    panel.innerHTML=
      '<button type="button" class="task-sheet-grip" aria-label="下滑或点击关闭模型选择"></button>'
      +'<div class="tm-head"><div class="tm-title">当前任务的模型</div><div class="tm-sub">加载中…</div></div>'
      +'<div class="tm-search">'+ICON_SEARCH
      +'<input class="tm-input" type="text" placeholder="搜索模型或来源…" autocomplete="off" spellcheck="false" aria-label="搜索模型">'
      +'<button type="button" class="tm-clr" hidden aria-label="清空搜索">✕</button></div>'
      +'<div class="tm-scroll"></div>';
    cover.appendChild(panel);
    cover.addEventListener('click',function(ev){ if(ev.target===cover) closeSheet(); });
    document.body.appendChild(cover);

    var scroll=panel.querySelector('.tm-scroll'),
        input=panel.querySelector('.tm-input'),
        clr=panel.querySelector('.tm-clr'),
        sub=panel.querySelector('.tm-sub');

    var self={data:null,seq:seq,id:id,cover:cover,panel:panel,scroll:scroll,sub:sub};
    live=self;

    function repaint(q){
      var query=(q==null)?input.value:q;
      if(self.data){
        scroll.innerHTML=buildList(self.data,query);
        sub.textContent=(self.data.model||'')+(self.data.provider?(' · '+groupLabel(self.data.provider,null)):'');
      }else{
        scroll.innerHTML='<div class="tm-empty">加载中…</div>';
        sub.textContent='加载中…';
      }
    }
    function focusCurrent(){
      if(input.value || !self.data) return;
      var cur=scroll.querySelector('.tm-item.on'); if(!cur) return;
      var a=cur.getBoundingClientRect(), b=scroll.getBoundingClientRect();
      if(a.top<b.top+8 || a.bottom>b.bottom-8) scroll.scrollTop+=(a.top-b.top)-(b.height-a.height)/2;
    }
    self.repaint=repaint; self.focusCurrent=focusCurrent; self.data=data||null;

    var raf=0;
    input.addEventListener('input',function(){
      clr.hidden=!input.value;
      if(raf) return;
      raf=requestAnimationFrame(function(){ raf=0; repaint(); });
    });
    input.addEventListener('keydown',function(e){
      if(e.key!=='Escape') return;
      e.stopPropagation();                                    // 别让外层把它当成"关面板"
      if(input.value){ input.value=''; clr.hidden=true; repaint(''); }
      else closeSheet();
    });
    clr.addEventListener('click',function(){ input.value=''; clr.hidden=true; repaint(''); input.focus(); });

    scroll.addEventListener('click',function(ev){
      var b=ev.target && ev.target.closest ? ev.target.closest('.tm-item') : null;
      if(!b || b.disabled || b.__busy) return;
      b.__busy=true; b.classList.add('tm-busy');
      var p=b.getAttribute('data-p'), mid=b.getAttribute('data-m');
      fetch('/api/task-model',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({topic_id:id,provider:p,model:mid})})
        .then(function(r){ return r.json().then(function(d){ return {ok:r.ok,d:d}; }); })
        .then(function(res){
          if(!res.ok || !res.d || !res.d.success) throw Error((res.d&&res.d.error)||'选择失败');
          delete cacheMap[id]; shown='';                        // 失效：下次重取，保证"当前"标记正确
          closeSheet();
          if(tid()===id) load(true);
          // 换了模型＝可能换了供应商：顶栏余额要立刻跟到这个任务新源的数
          // （force 跳过服务端 60s 任务级缓存，否则会继续显示旧源的数）
          if(tid()===id && typeof window.refreshBalance==='function') window.refreshBalance(id,true);
        })
        .catch(function(err){
          b.__busy=false; b.classList.remove('tm-busy');
          alert('切换失败：'+err.message);
        });
    });

    repaint();
    focusCurrent();
    return self;
  }

  function fill(data){
    if(!live || !live.cover.isConnected){ live=null; return; }
    live.data=data;
    live.repaint(null);
    live.focusCurrent();
  }

  function failSheet(){
    if(!live || !live.cover.isConnected) return;
    live.sub.textContent='加载失败';
    live.scroll.innerHTML='<div class="tm-empty">模型列表加载失败<br><button type="button" class="tm-retry">重试</button></div>';
    var rb=live.scroll.querySelector('.tm-retry');
    if(rb) rb.addEventListener('click',function(){
      if(!live) return;
      live.scroll.innerHTML='<div class="tm-empty">加载中…</div>';
      var seq=live.seq;
      load(true).then(function(d){ if(d && live && live.seq===seq) fill(d); else failSheet(); });
    });
  }

  button.onclick=function(e){
    e.preventDefault(); e.stopPropagation();
    var id=tid(); if(!id){ alert('请先选择任务'); return; }
    var seq=++openSeq;
    var has=(cacheMap[id]&&cacheMap[id].d)||null;
    makeSheet(id,has,seq);
    if(cacheFresh(id)) return;                                 // 缓存够新：直接渲染，不发请求
    load(false).then(function(d){
      if(seq!==openSeq) return;                                // 已被下一次打开取代
      if(d) fill(d); else if(!has) failSheet();
    });
  };

  /* 切任务时后台预热：等用户点开时通常已经有缓存 */
  setInterval(function(){
    var id=tid(); if(!id || id===shown) return;
    var c=cacheOf(id);
    if(c){ applyLabel(c); shown=id; }
    if(cacheFresh(id)) return;
    if(Date.now()-lastFail<5000) return;                       // 失败退避：别每 800ms 打一次
    load(true).then(function(d){ if(!d) lastFail=Date.now(); });
  },800);
})();
