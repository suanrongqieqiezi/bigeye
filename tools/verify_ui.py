# -*- coding: utf-8 -*-
"""verify_ui —— 改完界面，自己看一眼：起浏览器 → 量事实 → 取像素 → 主模型读图。

【为什么要有它】
动作链（task/action_chain）只对账到"工具返回成功"，那是**动作层**的成功，
不是**效果层**的成功。改完 CSS 后 edit_file 返回 ok，界面仍可能白屏、错位、
被下层盖住 —— 这些在返回值里一个字节都看不到。

【三个观测陷阱，本工具逐个补】
1. 几何数值量不出「看得见」。
   rect 有宽高，只说明它在布局里占了位置；它可能 opacity:0、被上级盖住、
   被父级 overflow 裁掉。所以除了 facts，还要**取像素**（points 参数）——
   颜色/是否与底色同色，才是"看得见"的证据。
2. 截图会截到动画中间态。
   大眼的显隐是 0.22s 交叠淡化，直接截常拍到两层各半透明，看着像"重影 bug"。
   所以截图前注入 CSS 冻结 transition/animation（freeze=True 默认开）。
3. 画面「看着正常」但 JS 已经炸了。
   所以同时收 console error / pageerror —— 它比截图更早暴露问题，
   而且截图上看不出来（前端出错通常只是某个区域不动）。

【为什么不复用 see_image】
see_image 的默认提问是「人物/发色/瞳色/服装」，对 UI 截图等于没问。
这里自带 UI 专用提问（对齐/溢出/遮挡/空白/文字截断）。

【并发】服务是 ThreadingHTTPServer，本工具在请求线程里跑，阻塞的是自己这一轮，
不会堵住别人的请求。但一次调用约 5~25s（含看图），属正常。
"""
import json
import os
import socket
import sys
import time

from tools import registry
from tools.registry import register_tool


# ── 自注册进常驻工具集（热重载不重载 registry.py，只能由本模块 import 时自己挂名）──
# 为什么坚持常驻而不是塞折叠组：这个工具存在的唯一目的就是「让人想起要验」。
# 放进折叠组 = 还得先 discover_tools 才会想到，那和没有它没区别。
def _self_register():
    try:
        registry.ALWAYS_ON_TOOLS.add("verify_ui")
        registry.CORE_TOOLS.add("verify_ui")
    except Exception as e:  # pragma: no cover
        print(f"[verify_ui] 自注册常驻失败: {e}")


_self_register()


def _root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _ensure_env():
    """补齐 APPDATA / LOCALAPPDATA。

    大眼常从 pythonw.exe / vbs 拉起，这类进程的环境变量是残缺的：
    APPDATA 为空 ⇒ 用户级 site-packages（C:\\Users\\<u>\\AppData\\Roaming\\Python\\Python313）
    不在 sys.path ⇒ import playwright 直接 ModuleNotFoundError。
    """
    home = os.path.expanduser("~")
    for key, sub in (("APPDATA", r"AppData\Roaming"),
                     ("LOCALAPPDATA", r"AppData\Local")):
        if not os.environ.get(key):
            cand = os.path.join(home, sub)
            if os.path.isdir(cand):
                os.environ[key] = cand


def _resolve_url(url):
    """没给 url 时探测本机在跑的大眼服务。"""
    if url:
        u = url.strip()
        if u.startswith(("http://", "https://", "file://")):
            return u
        return "http://" + u
    for port in (9890, 8765, 9896, 9897):
        s = socket.socket()
        s.settimeout(0.35)
        try:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return f"http://127.0.0.1:{port}/"
        finally:
            s.close()
    return None


def _wrap_js(src):
    """把一段 JS 变成 playwright evaluate 能吃的形式。

    模型很容易传裸函数体（`const r=x.getBoundingClientRect(); return r.height`），
    直接 evaluate 会 SyntaxError。含 return 就当函数体包一层，否则当表达式求值。
    """
    s = (src or "").strip()
    if not s:
        return None
    if s.startswith("(") or s.startswith("function") or s.startswith("async"):
        return s
    if "return" in s:
        return "() => { " + s + " }"
    return "() => (" + s + ")"


_FREEZE_JS = """() => {
  if (document.getElementById('__verify_freeze__')) return true;
  const st = document.createElement('style');
  st.id = '__verify_freeze__';
  st.textContent = '*,*::before,*::after{transition:none!important;'
                 + 'animation:none!important;caret-color:transparent!important}';
  (document.head || document.documentElement).appendChild(st);
  return true;
}"""

# DOM 静默等待：连续 quiet 毫秒没有 DOM 变动才算渲染完。
# 为什么不能只靠 wait_ms：实测大眼首屏死等 3.5s 仍截到**骨架屏**（占位圆+灰条），
# 截图"正常"但全是假的 —— 这类假证据比截不出图更误导。固定毫秒猜不准机器快慢，
# 变异静默才反映"它真的不动了"。
_SETTLE_JS = """(quiet) => new Promise((res) => {
  const start = Date.now();
  let last = Date.now(), n = 0, ob = null;
  try {
    ob = new MutationObserver(() => { last = Date.now(); n++; });
    // 🔴 只认 childList + characterData，**绝不监听 attributes**：
    // style/class 的每帧改写（旋转的三点、加载光晕、进度条）会以 ~90 次/秒的
    // 频率触发 attributes 变异，于是页面永远不静默、白等满超时（实测 15s 1304 次）。
    // 骨架屏 → 真实内容 是节点替换（childList），结构一变就够了。
    ob.observe(document.documentElement,
               {subtree: true, childList: true, characterData: true});
  } catch (e) {}
  const tick = setInterval(() => {
    const now = Date.now();
    if (ob && now - last > quiet) {
      clearInterval(tick); ob.disconnect();
      res({settled: true, mutations: n, ms: now - start});
    } else if (!ob || now - start > 8000) {
      clearInterval(tick); if (ob) ob.disconnect();
      res({settled: false, mutations: n, ms: now - start,
           note: '8s 内结构一直在变（轮询/流式），按当前画面截图'});
    }
  }, 100);
})"""


def _shot_dir():
    d = os.path.join(_root(), "data", "verify_shots")
    os.makedirs(d, exist_ok=True)
    return d


def _prune(d, keep=40):
    """只留最近 keep 张。截图是证据，不是资产 —— 攒多了会拖慢目录列举。"""
    try:
        files = [os.path.join(d, f) for f in os.listdir(d) if f.lower().endswith(".png")]
        files.sort(key=lambda p: os.path.getmtime(p), reverse=True)
        for p in files[keep:]:
            try:
                os.remove(p)
            except Exception:
                pass
    except Exception:
        pass


def _resolve_points(page, points, shot_selector, full_page):
    """把采样点统一换算成「相对截图原点」的像素坐标。

    三种坐标体系必须对齐，否则采样点会静默跑到左上角去（踩过：{selector,dx,dy}
    形式的点没解析，全部变成 (0,0)，取回来是白底，看着像"元素是白的"）：
      · 视口截图   → 原点 = 视口左上，直接可用
      · 整页截图   → 原点 = 文档顶部，y 要加 scrollY
      · 元素截图   → 原点 = 该元素左上，要减去元素 rect
    元素不存在时明确标 missing，不要默默返回 (0,0)。
    """
    if not points:
        return []
    js = """(args) => {
      const [pts, sel, fullPage] = args;
      const out = [];
      let ox = 0, oy = 0;
      if (sel) {
        const e = document.querySelector(sel);
        if (e) { const r = e.getBoundingClientRect(); ox = r.left; oy = r.top; }
      } else if (fullPage) {
        // 像素 = 视口坐标 - oy ⇒ 取 oy = -scrollY，把视口坐标抬到文档坐标
        ox = 0; oy = -window.scrollY;
      }
      for (const p of pts) {
        let x = Number(p.x) || 0, y = Number(p.y) || 0;
        let miss = null;
        if (p.selector) {
          const e = document.querySelector(p.selector);
          if (!e) { miss = p.selector; }
          else {
            const r = e.getBoundingClientRect();
            x = r.left + (Number(p.dx) || 0);
            y = r.top + (Number(p.dy) || 0);
          }
        }
        if (miss) { out.push({missing: miss, name: p.name || ''}); continue; }
        out.push({x: Math.round(x - ox), y: Math.round(y - oy), name: p.name || ''});
      }
      return out;
    }"""
    try:
        return page.evaluate(js, [points, shot_selector, bool(full_page)])
    except Exception as e:
        return [{"error": f"采样点解析失败: {type(e).__name__}: {e}"}]


def _sample_pixels(shot, points):
    """从截图取像素。points 必须是已换算好的 {x, y}（见 _resolve_points）。"""
    if not points:
        return []
    try:
        from PIL import Image
        im = Image.open(shot).convert("RGB")
    except Exception as e:
        return [{"error": f"读图失败: {e}"}]
    out = []
    for p in points:
        if p.get("error") or p.get("missing") is not None:
            out.append(p)
            continue
        try:
            x = int(p.get("x", 0))
            y = int(p.get("y", 0))
            if x < 0 or y < 0 or x >= im.size[0] or y >= im.size[1]:
                out.append({"name": p.get("name") or "", "out_of_shot": [x, y],
                            "shot_size": list(im.size)})
                continue
            r, g, b = im.getpixel((x, y))
            out.append({"at": [x, y], "rgb": [r, g, b],
                        "hex": f"#{r:02x}{g:02x}{b:02x}",
                        "name": p.get("name") or ""})
        except Exception as e:
            out.append({"error": str(e), "point": p})
    return out


def _dedup_errors(errors):
    """同一个 404 会同时产生「[HTTP 404] url」和「[error] Failed to load resource」两条。
    带 URL 的那条信息量大得多，所以有了它就把裸的那条丢掉，免得模型只读第一句、
    永远不知道是哪个资源挂了。完全重复的也去掉。"""
    seen, out = set(), []
    has_http = any(e.startswith("[HTTP ") for e in errors)
    for e in errors:
        if has_http and e.startswith("[error] Failed to load resource"):
            continue
        if e in seen:
            continue
        seen.add(e)
        out.append(e)
    return out


UI_QUESTION = (
    "这是一张网页界面截图。请只描述你**实际看到**的画面，逐项回答："
    "1) 整体版式是否正常：有没有元素重叠、错位、明显偏左/偏右、大片空白？"
    "2) 有没有内容被裁掉或溢出容器（文字被截断、卡片超出边框、出现多余滚动条）？"
    "3) 有没有明显的视觉断裂：色块边界生硬、阴影/圆角不一致、上下留白不对称？"
    "4) 界面看起来是深色还是浅色主题，与背景是否协调？"
    "5) 一句话结论：这张界面**是否有肉眼可见的缺陷**。"
    "看不清就写「看不清」，不要脑补，不要评价功能，只讲画面。"
)


def _self_vision_enabled():
    """model_config.json 的 self_vision 开关：开了就让框架把图注入当前对话，我自己看。"""
    try:
        with open(os.path.join(_root(), "model_config.json"), encoding="utf-8") as f:
            return bool(json.load(f).get("self_vision"))
    except Exception:
        return False


def _ask_vision(shot, question, max_side=1280):
    """复用项目根的 vsee.py —— 走主模型自己的多模态接口，不额外占本地模型。"""
    root = _root()
    if root not in sys.path:
        sys.path.insert(0, root)
    import vsee  # noqa: E402
    cfg = vsee._load_cfg()
    model = cfg.get("model") or "deepseek-flash"
    # max_tokens 给足：deepseek-flash 是推理模型，reasoning 与正文共用预算。
    # 实测 3000 会被 reasoning 吃光 → content 空 → vsee 降级输出英文推理过程，
    # 看着像"模型答了"，其实那段不是结论。
    ans = vsee.ask([shot], question or UI_QUESTION, model,
                   max_side=max_side, verbose=False, max_tokens=4096)
    return ans, model


def _clip(s, n=2600):
    s = s if isinstance(s, str) else json.dumps(s, ensure_ascii=False, default=str)
    return s if len(s) <= n else s[:n] + f"…（截断，原 {len(s)} 字符）"


@register_tool(
    name="verify_ui",
    description=(
        "改完界面自己看一眼：用无头浏览器打开页面 → 执行 JS 取布局事实 → 截图 → 交给主模型读图，"
        "并同时收集 console 报错。返回 {ok, shot, facts, pixels, console_errors, vision}。"
        "凡是改动了 HTML/CSS/前端 JS 之后都应该调用它验收 —— 工具返回成功不代表界面看得见。"
        "要点：几何数值（宽高/位置）不能证明「看得见」，必须看图；"
        "颜色是否与底色相同、是否被遮挡，用 points 采样像素。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "url": {"type": "string",
                    "description": "页面地址。不传则自动探测本机在跑的大眼（9890/8765）。支持 file:/// 本地 HTML。"},
            "js": {"type": "string",
                   "description": "取布局事实的 JS。可写表达式（document.querySelectorAll('.x').length）或函数体（const r=...; return r.height）。结果原样返回在 facts 里。"},
            "prep_js": {"type": "string",
                        "description": "截图前执行的准备脚本，如展开面板、切主题、注入测试数据。在冻结动画之前执行。"},
            "theme": {"type": "string", "enum": ["white", "dark"],
                      "description": "可选：截图前切主题。走页面自己的 setTheme()（不要直接改 data-theme，那样不生效）。"},
            "selector": {"type": "string",
                         "description": "只截这个元素（CSS 选择器）。不传则截整页视口。"},
            "full_page": {"type": "boolean",
                          "description": "截整页（含滚动区域）。默认 false 只截视口 —— 界面对齐问题多数在首屏。"},
            "width": {"type": "integer", "description": "视口宽，默认 1440。"},
            "height": {"type": "integer", "description": "视口高，默认 900。"},
            "wait_ms": {"type": "integer",
                        "description": "页面加载后额外等待毫秒，默认 300。真正等渲染完成靠 settle_ms，这个只是兜底。"},
            "settle_ms": {"type": "integer",
                          "description": "DOM 静默等待：连续这么多毫秒没有 DOM 变动才算渲染完，默认 800。"
                                         "比死等 wait_ms 可靠 —— 实测死等 3.5s 仍会截到骨架屏（占位圆+灰条），"
                                         "那种图看着正常但全是假的。传 0 关闭。"},
            "wait_for": {"type": "string",
                         "description": "可选：先等这个选择器出现再截图，比死等毫秒可靠。"},
            "points": {"type": "array",
                       "description": "像素采样点：量「这块到底是不是那个颜色/有没有被盖住」。每项 {x,y} 或 {selector,dx,dy}。",
                       "items": {"type": "object"}},
            "question": {"type": "string",
                         "description": "问图的问题。不传用 UI 专用提问（重叠/溢出/遮挡/空白/主题）。"},
            "no_vision": {"type": "boolean",
                          "description": "true 则只截图+取事实，不花 LLM 看图（省时间，但只能查崩溃不能查好看）。"},
            "freeze": {"type": "boolean",
                       "description": "截图前冻结过渡动画，默认 true。要检查动画中间态时传 false。"},
        },
        "required": [],
    },
)
def verify_ui(url=None, js=None, prep_js=None, theme=None, selector=None,
              full_page=False, width=1440, height=900, wait_ms=300,
              settle_ms=800, wait_for=None, points=None, question=None,
              no_vision=False, freeze=True):
    target = _resolve_url(url)
    if not target:
        return {"ok": False, "error": "没有 url，也没探测到本机在跑的大眼（9890/8765）。请显式传 url。"}

    _ensure_env()
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        return {"ok": False, "error": f"playwright 不可用: {type(e).__name__}: {e}",
                "hint": "确认 APPDATA 已补；或 pip install playwright && playwright install msedge"}

    shot_dir = _shot_dir()
    stamp = time.strftime("%Y%m%d_%H%M%S")
    slug = "page" if not selector else selector.strip("#.[ ")[:20].replace("/", "_") or "el"
    shot = os.path.join(shot_dir, f"{stamp}_{slug}.png")

    facts, console_errors, vision, model = None, [], None, None
    resolved_points = []
    settle_info = None
    ok, err, title = False, None, None

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(channel="msedge", headless=True)
            ctx = browser.new_context(
                viewport={"width": int(width), "height": int(height)},
                device_scale_factor=1,          # 像素坐标准确性靠它：缩放不为 1 时 points 会偏
            )
            page = ctx.new_page()
            page.on("console", lambda m: console_errors.append(f"[{m.type}] {_clip(m.text, 300)}")
                    if m.type in ("error", "warning") else None)
            page.on("pageerror", lambda e: console_errors.append(f"[pageerror] {_clip(str(e), 300)}"))
            # 资源 404/500 必须带 URL —— 浏览器的 console 只说「Failed to load resource」，
            # 不说是哪个资源，等于没有信息（实测第一次就被这句卡住）。
            page.on("response", lambda r: console_errors.append(f"[HTTP {r.status}] {_clip(r.url, 200)}")
                    if r.status >= 400 else None)

            page.goto(target, wait_until="domcontentloaded", timeout=45000)
            title = page.title()

            if wait_for:
                try:
                    page.wait_for_selector(wait_for, timeout=15000)
                except Exception:
                    console_errors.append(f"[verify] wait_for 超时: {wait_for}")
            if wait_ms:
                page.wait_for_timeout(int(wait_ms))

            settle_info = None
            if settle_ms:
                try:
                    settle_info = page.evaluate(_SETTLE_JS, int(settle_ms))
                except Exception as e:
                    settle_info = {"error": f"{type(e).__name__}: {e}"}

            if prep_js:
                try:
                    page.evaluate(_wrap_js(prep_js))
                    if settle_ms:
                        # 点了按钮/展开了面板之后也得等它动完，否则截到的还是操作前的样子
                        settle_info = page.evaluate(_SETTLE_JS, int(settle_ms))
                except Exception as e:
                    console_errors.append(f"[prep_js] {type(e).__name__}: {e}")

            if theme:
                # 项目铁律：切主题走页面自己的 setTheme()，直接改 data-theme 不生效（§96）
                try:
                    page.evaluate(
                        "t => { if (typeof window.setTheme === 'function') { window.setTheme(t); return 'setTheme'; }"
                        " document.documentElement.setAttribute('data-theme', t); return 'attr'; }",
                        theme)
                    page.wait_for_timeout(400)
                except Exception as e:
                    console_errors.append(f"[theme] {type(e).__name__}: {e}")

            if freeze:
                try:
                    page.evaluate(_FREEZE_JS)
                    page.wait_for_timeout(120)   # 冻结后让浏览器重绘一帧，否则截到冻结前的合成层
                except Exception:
                    pass

            if js:
                try:
                    facts = page.evaluate(_wrap_js(js))
                except Exception as e:
                    facts = {"__error__": f"{type(e).__name__}: {e}"}

            # selector 元素若在视口外，先滚进来 —— 否则截出来是一张空白
            if selector:
                try:
                    page.locator(selector).first.scroll_into_view_if_needed(timeout=4000)
                    page.wait_for_timeout(150)
                except Exception:
                    pass
                page.locator(selector).first.screenshot(path=shot, timeout=15000)
            else:
                page.screenshot(path=shot, full_page=bool(full_page), timeout=20000)

            # title 放到最后取：domcontentloaded 时前端还没改 <title>，早取会拿到模板里的旧值
            try:
                title = page.title()
            except Exception:
                pass
            # 采样点必须在浏览器还开着时换算（要在页面里查 DOM 拿 rect）
            resolved_points = _resolve_points(page, points, selector, full_page)
            ok = True
            browser.close()
    except Exception as e:
        err = f"{type(e).__name__}: {e}"

    result = {
        "ok": bool(ok and os.path.exists(shot)),
        "url": target,
        "title": title,
        "shot": shot if os.path.exists(shot) else None,
        "viewport": f"{width}x{height}",
        "facts": facts,
        "settle": settle_info,
        "pixels": _sample_pixels(shot, resolved_points) if os.path.exists(shot) else [],
        "console_errors": _dedup_errors(console_errors)[:20],
    }
    if err:
        result["error"] = err
        return result

    if not no_vision and os.path.exists(shot):
        if _self_vision_enabled():
            # 走「自视」通道：框架认这个标记，会把图片作为一条 user 图片消息注入下一轮，
            # 于是我自己看原图。比让 vsee 转述准确得多，也省掉一次 100s 的二次调用
            # —— 实测 glm-5.3-flash 转述时 reasoning 会把 token 预算吃光，返回的
            # 「[推理过程，非正式结论]」是一段英文自言自语，拿去当验收结论等于没验。
            result["__self_vision__"] = True
            result["images"] = [shot]
            result["vision"] = "（截图已注入下一条消息，请你亲自看）"
            result["verdict"] = (
                "页面打开了，但有报错 —— 先查报错，别只看图说没事（图也照样送你看了）"
                if result["console_errors"] else
                "截图已出并正在送进你的眼睛 —— 自己看：有没有重叠/错位/被遮挡/大片空白、"
                "有没有该出现的内容没出现。看完再下结论。")
            try:
                _prune(shot_dir)
            except Exception:
                pass
            return result
        try:
            vision, model = _ask_vision(shot, question)
            result["vision"] = vision
            result["vision_model"] = model
        except Exception as e:
            result["vision_error"] = f"{type(e).__name__}: {e}"
            result["hint"] = "截图已生成，可手动 see_image(paths=[shot]) 看图"

    try:
        _prune(shot_dir)
    except Exception:
        pass

    # 给一句能直接抄进汇报的判断，省得模型自己发挥
    if result["ok"]:
        if result["console_errors"]:
            result["verdict"] = "页面打开了，但有 console 报错 —— 先把报错当问题查，别看图说没事"
        elif result.get("vision"):
            result["verdict"] = "已出图并看过。以 vision 里「是否有肉眼可见缺陷」那段为准"
        else:
            result["verdict"] = "只拿到截图和事实，没看图 —— 结论最多是「没崩」，不构成验收"
    return result
