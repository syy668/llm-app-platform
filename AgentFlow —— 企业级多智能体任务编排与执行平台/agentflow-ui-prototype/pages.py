# -*- coding: utf-8 -*-
import os
from build import CSS, ICONS, shell, tag, bar, dag

OUT = os.path.dirname(os.path.abspath(__file__))

# ---------------------------------------------------------------- P01 登录
# 设计要点：登录表单不出现任何"角色"控件。LoginRequest 只接受 username / password，
# 角色由后端 users.role 决定，登录后经 GET /auth/me 获取，前端只做展示与菜单裁剪。
LOGIN_CSS = """
.login{display:grid;grid-template-columns:56% 44%;height:100vh;min-height:840px}
.lg-side{background:#1D2129;color:#fff;padding:52px 60px;position:relative;overflow:hidden;display:flex;flex-direction:column}
.grid-bg{position:absolute;inset:0;background-image:linear-gradient(rgba(255,255,255,.045) 1px,transparent 1px),linear-gradient(90deg,rgba(255,255,255,.045) 1px,transparent 1px);background-size:48px 48px}
.lg-side .inner{position:relative;flex:1;display:flex;flex-direction:column}
.mid{margin-top:auto;margin-bottom:auto}
.lg{display:flex;align-items:center;gap:10px}
.lg .mk{width:30px;height:30px;border-radius:7px;background:#165DFF;display:flex;align-items:center;justify-content:center;font-size:13px;font-weight:500}
.lg .nm{font-size:18px;font-weight:500;letter-spacing:-.3px}
.lg .env{margin-left:8px;font-size:11px;color:#A9AEB8;border:1px solid #4E5969;border-radius:2px;padding:1px 6px;line-height:15px}
.lt{font-size:31px;line-height:43px;font-weight:500;letter-spacing:-.6px}
.lt span{color:#94BFFF}
.ld{margin-top:16px;font-size:13px;color:#9BA3AE;line-height:23px;max-width:430px}
.caps{margin-top:52px;border-top:1px solid #2E333B}
.cap{padding:16px 0;border-bottom:1px solid #2E333B;display:flex;gap:14px;align-items:flex-start}
.cap .k{font-family:ui-monospace,Consolas,monospace;font-size:11px;color:#94BFFF;width:16px;padding-top:2px;flex:none}
.cap b{display:block;color:#E5E6EB;font-weight:400;font-size:13px;margin-bottom:4px}
.cap p{margin:0;color:#9BA3AE;font-size:12px;line-height:19px}
.lg-side .foot{font-size:11px;color:#7A8494;display:flex;justify-content:space-between;position:relative;padding-top:28px}
.panel{background:#fff;display:flex;align-items:center;justify-content:center;padding:40px}
.form{width:352px}
.form h2{margin:0;font-size:23px;font-weight:500;letter-spacing:-.4px}
.form .fh{font-size:12.5px;color:#86909C;margin-top:8px;margin-bottom:28px}
.il{display:block;font-size:12px;color:#4E5969;margin:0 0 7px}
.ip{height:38px;border:1px solid #E5E6EB;border-radius:2px;display:flex;align-items:center;padding:0 11px;font-size:13px;color:#1D2129;margin-bottom:18px;background:#fff;gap:8px}
.ip svg{width:14px;height:14px;stroke:#86909C;fill:none;stroke-width:1.6;flex:none}
.ip .ph{color:#C9CDD4}
.ip.focus{border-color:#165DFF;box-shadow:0 0 0 2px rgba(22,93,255,.10)}
.ip .cur{width:1px;height:15px;background:#165DFF;margin-left:-3px}
.ip .eye{margin-left:auto;display:flex;align-items:center;color:#86909C}
.ip .eye svg{width:15px;height:15px;stroke:#86909C}
.ip.err{border-color:#F53F3F;background:#FFF8F7}
.ip.err svg{stroke:#F53F3F}
.ip.dis{background:#F7F8FA;color:#86909C}
.errline{font-size:12px;color:#F53F3F;margin:-12px 0 16px;display:flex;align-items:center;gap:5px}
.errline svg{width:13px;height:13px;stroke:#F53F3F;fill:none;stroke-width:1.8;flex:none}
.row{display:flex;align-items:center;justify-content:space-between;margin:2px 0 22px;font-size:12.5px;color:#4E5969}
.cb{display:flex;align-items:center;gap:7px}
.cb .bx{width:14px;height:14px;border:1px solid #165DFF;background:#165DFF;border-radius:2px;display:flex;align-items:center;justify-content:center;color:#fff;font-size:10px;line-height:1}
.cb .tt{font-size:12px}
.cb .hint2{font-size:11px;color:#86909C;margin-left:2px}
.lnk{font-size:12px;color:#86909C}
.submit{height:40px;width:100%;background:#165DFF;color:#fff;border:0;border-radius:2px;font-size:14px;font-family:inherit;letter-spacing:3px;display:flex;align-items:center;justify-content:center;gap:8px}
.submit.dis{background:#94BFFF}
.spin{width:13px;height:13px;border:2px solid rgba(255,255,255,.45);border-top-color:#fff;border-radius:50%}
.note{margin-top:20px;font-size:11.5px;color:#86909C;line-height:19px;border-top:1px solid #F2F3F5;padding-top:16px}
.note b{font-weight:400;color:#4E5969}
.alert{display:flex;gap:9px;padding:10px 12px;border-radius:2px;font-size:12.5px;line-height:19px;margin-bottom:18px;align-items:flex-start}
.alert svg{width:14px;height:14px;flex:none;margin-top:2px;fill:none;stroke-width:1.8}
.a-err{background:#FFECE8;border:1px solid #FBACA3;color:#CB2634}
.a-err svg{stroke:#F53F3F}
.a-warn{background:#FFF7E8;border:1px solid #FFCF8B;color:#B85C00}
.a-warn svg{stroke:#FF7D00}
.alert b{font-weight:500}
.fmt{margin-top:26px;display:flex;justify-content:space-between;font-size:11px;color:#C9CDD4}
.fmt span.mono{font-size:10.5px}
"""

ICO_USER = '<svg viewBox="0 0 24 24"><circle cx="12" cy="8" r="3.6"/><path d="M4.5 20c0-4 3.4-6.2 7.5-6.2s7.5 2.2 7.5 6.2"/></svg>'
ICO_LOCK = '<svg viewBox="0 0 24 24"><rect x="4.5" y="10.5" width="15" height="9.5" rx="1.5"/><path d="M8 10.5V8a4 4 0 0 1 8 0v2.5"/></svg>'
ICO_EYE = '<svg viewBox="0 0 24 24"><path d="M2.5 12S6 5.8 12 5.8 21.5 12 21.5 12 18 18.2 12 18.2 2.5 12 2.5 12Z"/><circle cx="12" cy="12" r="2.8"/></svg>'
ICO_ALERT = '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M12 7.5v5.5"/><path d="M12 16.4v.2"/></svg>'
ICO_INFO = '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M12 11v5.5"/><path d="M12 7.6v.2"/></svg>'
ICO_CHECK = '<svg viewBox="0 0 24 24" style="width:12px;height:12px;stroke:#fff"><path d="M5 12.5l4.5 4.5L19 7.5"/></svg>'

p01 = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8"><title>AgentFlow · 登录</title>
<style>__CSS____LOGIN_CSS__</style></head><body>
<div class="login">
  <aside class="lg-side"><div class="grid-bg"></div>
    <div class="inner">
      <div class="lg"><div class="mk">AF</div><div class="nm">AgentFlow</div><div class="env">内部系统</div></div>
      <div class="mid">
        <div class="lt">把一句业务问题<br>编排成<span>可追溯的 Agent 任务</span></div>
        <div class="ld">企业级多智能体任务编排与执行平台 · 电商经营分析场景</div>
        <div class="caps">
          <div class="cap"><div class="k">01</div><div><b>自然语言发起编排</b><p>目标 + 业务时间范围，Planner 自动生成 DAG 子任务计划</p></div></div>
          <div class="cap"><div class="k">02</div><div><b>LangGraph 多智能体执行</b><p>Executor 调用 MCP 工具：SQL / 图表 / 知识库 / 通知</p></div></div>
          <div class="cap" style="border-bottom:0"><div class="k">03</div><div><b>人工审批断点与全链路 Trace</b><p>高危工具触发 human_gate，审批后断点续跑，全程可回放</p></div></div>
        </div>
      </div>
    </div>
    <div class="foot"><span>v1.0.0 · 仅限授权账号访问</span><span>© 2026 电商数据中台</span></div>
  </aside>

  <main class="panel"><form class="form" onsubmit="return false">
    <h2>登录</h2>
    <div class="fh">使用企业账号登录，角色与数据范围由账号权限自动匹配</div>

    <label class="il">用户名</label>
    <div class="ip focus">__ICO_USER__<span>ops_zhangsan</span><span class="cur"></span></div>

    <label class="il">密码</label>
    <div class="ip">__ICO_LOCK__<span style="letter-spacing:2px">••••••••••</span><span class="eye">__ICO_EYE__</span></div>

    <div class="row">
      <div class="cb"><span class="bx">__ICO_CHECK__</span><span class="tt">在此设备保持登录 7 天</span></div>
      <span class="lnk">忘记密码？联系管理员</span>
    </div>

    <button class="submit" type="submit">登 录</button>

    <div class="note">
      账号由管理员在 <b>用户与权限</b> 中开通，角色（管理员 / 审批人 / 访客）随账号下发，
      登录后自动匹配菜单与数据范围 —— <b>登录页不提供角色选择</b>。
    </div>

    <div class="fmt"><span class="mono">API /api/v1</span><span>v1.0.0</span></div>
  </form></main>
</div></body></html>""".replace("__CSS__", CSS).replace("__LOGIN_CSS__", LOGIN_CSS)\
    .replace("__ICO_USER__", ICO_USER).replace("__ICO_LOCK__", ICO_LOCK)\
    .replace("__ICO_EYE__", ICO_EYE).replace("__ICO_CHECK__", ICO_CHECK)


# ---------------------------------------------------------------- P01b 登录状态矩阵
def _mini(fields, alert="", btn="登 录", btn_cls="submit", extra=""):
    """构造状态卡里的迷你表单片段。"""
    body = alert
    for lab, ic, val, cls in fields:
        body += f'<div class="il" style="margin-bottom:6px">{lab}</div><div class="ip {cls}" style="margin-bottom:14px;height:34px">{ic}<span>{val}</span></div>'
    body += extra
    body += f'<button class="{btn_cls}" style="height:34px;letter-spacing:2px">{btn}</button>'
    return body


PWD = '<span style="letter-spacing:2px">••••••••</span>'
STATES = [
    ("默认态", "初次进入", "t-off",
     _mini([("用户名", ICO_USER, '<span class="ph">请输入用户名</span>', "focus"),
            ("密码", ICO_LOCK, '<span class="ph">请输入密码</span>', "")]),
     "用户名框自动聚焦，密码框回车直接提交；浏览器自动填充走 <code>autocomplete</code>，不拦截。"),

    ("字段校验失败", "本地校验 · 不发请求", "t-mid",
     _mini([("用户名", ICO_USER, "ops_zhangsan", "err"),
            ("密码", ICO_LOCK, '<span class="ph">请输入密码</span>', "err")],
           extra='<div class="errline" style="margin:-8px 0 12px">__AL__请输入密码</div>'),
     "红边 + 框下 12px 红字，聚焦第一个错误字段；不发 API 请求，不计入失败次数。"),

    ("凭证错误", "AUTH_INVALID_CREDENTIALS · 401", "t-err",
     _mini([("用户名", ICO_USER, "ops_zhangsan", ""),
            ("密码", ICO_LOCK, '<span class="ph">请输入密码</span>', "err")],
           alert='<div class="alert a-err">__AL__<div><b>用户名或密码错误</b><br>请确认后重试，连续失败请联系管理员重置密码。</div></div>'),
     "保留用户名、清空密码并聚焦（<b>绝不提示“用户不存在”</b>，后端已用 dummy_verify 抹平时序差异）。"),

    ("账号被禁用", "AUTH_USER_DISABLED · 403", "t-err",
     _mini([("用户名", ICO_USER, "viewer_wangwu", "dis"),
            ("密码", ICO_LOCK, PWD, "dis")],
           alert='<div class="alert a-err">__AL__<div><b>账号已被禁用</b><br>请联系管理员在「用户与权限」中恢复，或更换账号登录。</div></div>',
           btn="账号不可用", btn_cls="submit dis"),
     "403 与 401 分开处理：不清空密码、禁用提交按钮，避免用户反复试同一账号。"),

    ("提交中", "请求在途", "t-run",
     _mini([("用户名", ICO_USER, "ops_zhangsan", "dis"),
            ("密码", ICO_LOCK, PWD, "dis")],
           btn='<span class="spin"></span>登录中', btn_cls="submit dis"),
     "按钮 loading + 全表单禁用防重复提交；超时 10s 视为网络异常。"),

    ("网络异常", "NETWORK_ERROR / 5xx", "t-warn",
     _mini([("用户名", ICO_USER, "ops_zhangsan", ""),
            ("密码", ICO_LOCK, PWD, "")],
           alert='<div class="alert a-warn">__AL__<div><b>无法连接后端服务</b><br>当前 API：<span class="mono">/api/v1</span></div></div>',
           btn="重 试"),
     "仅网络层/5xx 才出现；回显当前 API Base 便于联调，4xx 业务码不走此分支。"),
]

state_cards = ""
for name, code, cls, mini, desc in STATES:
    mini = mini.replace("__AL__", ICO_ALERT)
    state_cards += f"""<div class="card" style="padding:0;overflow:hidden;display:flex;flex-direction:column">
  <div style="display:flex;align-items:center;gap:8px;padding:12px 16px;border-bottom:1px solid #F2F3F5;background:#FAFBFC">
    <span class="tag {cls}"><i></i>{name}</span>
    <span class="mono mut" style="font-size:11px">{code}</span>
  </div>
  <div style="padding:16px">{mini}</div>
  <div style="margin-top:auto;padding:12px 16px 16px;border-top:1px dashed #E5E6EB;font-size:11.5px;color:#4E5969;line-height:19px">{desc}</div>
</div>"""

p01b_body = f"""
<div class="page-head">
  <div><h1>登录页 · 状态矩阵</h1>
    <div class="sub">角色不参与登录交互：<span class="mono">POST /auth/login</span> 只接收 <span class="mono">username</span> / <span class="mono">password</span>，角色来自 <span class="mono">GET /auth/me</span> 的 <span class="mono">role</span></div></div>
  <div class="btns"><span class="tag t-info"><i></i>开发对照用</span></div>
</div>
<div style="display:grid;grid-template-columns:repeat(3,1fr);gap:14px">{state_cards}</div>
<div class="card" style="margin-top:14px;padding:14px 16px">
  <div style="font-size:12px;color:#4E5969;line-height:22px">
    <b style="font-weight:500">前端分支原则</b>　
    ① 只有 <span class="mono">AUTH_INVALID_CREDENTIALS</span> / <span class="mono">AUTH_USER_DISABLED</span> 用 error Alert，其余 4xx 走 warning；
    ② <span class="mono">RATE_LIMITED · 429</span> 错误码已定义但后端当前无抛出点（无失败计数与锁定），前端保留分支不阻塞；
    ③ 登录成功响应是 <span class="mono">TokenPair</span>（access / refresh / expires_in），<b style="font-weight:500">不含 role</b>，必须再调 <span class="mono">GET /auth/me</span> 才能拿到角色。
  </div>
</div>"""

p01b = f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8"><title>AgentFlow · 登录状态矩阵</title>
<style>{CSS}{LOGIN_CSS}
body{{background:#F7F8FA}}
.wrap{{max-width:1320px;margin:0 auto;padding:28px 32px 40px}}
.wrap .page-head h1{{font-size:20px}}
.wrap code,.wrap .mono{{font-family:ui-monospace,SFMono-Regular,Consolas,monospace}}
.wrap code{{background:#F2F3F5;padding:1px 4px;border-radius:2px;font-size:11px;color:#4E5969}}
</style></head><body><div class="wrap">{p01b_body}</div></body></html>"""

# ---------------------------------------------------------------- P02 大盘
KPI = [
    ("今日任务总量", "128", "", "↑ 12 较昨日", "up", ""),
    ("任务完成率", "92.4", "%", "↑ 2.1pt 较昨日", "up", ""),
    ("待审批数量", "3", "", "需处理 · 去收件箱", "", "warn"),
    ("平均端到端耗时", "4m12s", "", "↓ 18s 较上周", "up", ""),
    ("总 Token 消耗", "1.24", "M", "↑ 8.6% 较昨日", "down", ""),
]
kpi_html = ""
for t, v, u, f, fc, extra in KPI:
    kpi_html += f"""<div class="kpi {extra}"><h4>{t}</h4>
<div class="v num">{v}{f'<small>{u}</small>' if u else ''}</div>
<div class="f"><span class="{fc}">{f}</span></div></div>"""

trend = """<svg width="100%" viewBox="0 0 460 216" font-family="inherit">
<g stroke="#F2F3F5" stroke-width="1"><line x1="42" y1="20" x2="450" y2="20"/><line x1="42" y1="62" x2="450" y2="62"/><line x1="42" y1="104" x2="450" y2="104"/><line x1="42" y1="146" x2="450" y2="146"/><line x1="42" y1="180" x2="450" y2="180"/></g>
<g fill="#86909C" font-size="10"><text x="8" y="24">120</text><text x="14" y="66">90</text><text x="14" y="108">60</text><text x="14" y="150">30</text><text x="24" y="184">0</text></g>
<path d="M60 118 L122 96 L184 130 L246 74 L308 52 L370 88 L432 62 L432 180 L60 180 Z" fill="#E8F0FF" opacity="0.9"/>
<path d="M60 118 L122 96 L184 130 L246 74 L308 52 L370 88 L432 62" fill="none" stroke="#165DFF" stroke-width="2"/>
<path d="M60 150 L122 142 L184 155 L246 132 L308 126 L370 140 L432 128" fill="none" stroke="#00B42A" stroke-width="1.5" stroke-dasharray="4 3"/>
<g fill="#165DFF"><circle cx="60" cy="118" r="3"/><circle cx="122" cy="96" r="3"/><circle cx="184" cy="130" r="3"/><circle cx="246" cy="74" r="3"/><circle cx="308" cy="52" r="4"/><circle cx="370" cy="88" r="3"/><circle cx="432" cy="62" r="3"/></g>
<g fill="#86909C" font-size="10"><text x="46" y="200">10-15</text><text x="108" y="200">10-16</text><text x="170" y="200">10-17</text><text x="232" y="200">10-18</text><text x="294" y="200">10-19</text><text x="356" y="200">10-20</text><text x="418" y="200">10-21</text></g>
<g font-size="10"><rect x="240" y="6" width="8" height="8" fill="#165DFF"/><text x="252" y="14" fill="#4E5969">已完成</text><rect x="300" y="6" width="8" height="8" fill="#00B42A"/><text x="312" y="14" fill="#4E5969">失败</text></g>
</svg>"""

donut = """<svg width="100%" viewBox="0 0 300 168" font-family="inherit">
<g transform="translate(96,88) rotate(-90)">
<circle r="54" fill="none" stroke="#F2F3F5" stroke-width="20"/>
<circle r="54" fill="none" stroke="#165DFF" stroke-width="20" stroke-dasharray="176 340"/>
<circle r="54" fill="none" stroke="#00B42A" stroke-width="20" stroke-dasharray="88 340" stroke-dashoffset="-176"/>
<circle r="54" fill="none" stroke="#FF7D00" stroke-width="20" stroke-dasharray="54 340" stroke-dashoffset="-264"/>
<circle r="54" fill="none" stroke="#722ED1" stroke-width="20" stroke-dasharray="20 340" stroke-dashoffset="-318"/>
</g>
<text x="96" y="83" text-anchor="middle" font-size="20" font-weight="500" fill="#1D2129">312</text>
<text x="96" y="100" text-anchor="middle" font-size="10" fill="#86909C">次工具调用</text>
<g font-size="11" fill="#4E5969">
<rect x="182" y="42" width="7" height="7" fill="#165DFF"/><text x="196" y="49">sql-query 52%</text>
<rect x="182" y="66" width="7" height="7" fill="#00B42A"/><text x="196" y="73">chart-render 26%</text>
<rect x="182" y="90" width="7" height="7" fill="#FF7D00"/><text x="196" y="97">kb-search 16%</text>
<rect x="182" y="114" width="7" height="7" fill="#722ED1"/><text x="196" y="121">notify 6%</text>
</g></svg>"""

fail_rows = [("TASK_EXECUTION_ERROR", 12), ("TOOL_TIMEOUT", 7), ("PLAN_INVALID", 5), ("REPLAN_EXHAUSTED", 3), ("APPROVAL_MISSING", 2)]
fail_html = ""
for name, n in fail_rows:
    pct = int(n / 12 * 100)
    fail_html += f"""<div style="display:flex;align-items:center;gap:10px;margin-bottom:14px">
<div class="mono mut" style="width:132px;font-size:11px">{name}</div>
<div style="flex:1;height:12px;background:#F7F8FA;border-radius:2px"><div style="height:12px;width:{pct}%;background:#F53F3F;border-radius:2px;opacity:.85"></div></div>
<div class="num" style="width:22px;text-align:right;font-size:12px">{n}</div></div>"""

ROWS = [
    ("AF20261021-0007", "分析 2026 年 9 月女装类目 GMV 环比下滑原因，输出结论与改进建议", tag("t-run", "执行中"), "张三", "10-21 15:02", "▶ 03:12", "8.2K", "查看 克隆 取消"),
    ("AF20261021-0006", "生成本周库存周转异常 SKU 清单并推送运营群", tag("t-wait", "待审批"), "张三", "10-21 14:47", "—", "5.1K", "查看 审批"),
    ("AF20261021-0005", "对比近 30 天两场大促的转化率差异，输出归因", tag("t-ok", "已完成"), "张三", "10-21 11:20", "4m12s", "12.4K", "查看 克隆"),
    ("AF20261021-0004", "复盘 9 月退货率上升原因（按品类 / 渠道拆分）", tag("t-err", "失败"), "李四", "10-21 09:58", "1m36s", "3.2K", "查看 克隆"),
    ("AF20261020-0003", "输出 Q3 经营看板数据口径说明与异常水位提示", tag("t-ok", "已完成"), "李四", "10-20 17:41", "6m05s", "21.7K", "查看 克隆"),
    ("AF20261020-0001", "女装类目竞品价格带扫描（含全站 SKU）", tag("t-off", "已取消"), "王五", "10-20 15:02", "0m48s", "1.4K", "查看"),
]
tr_html = ""
for no, goal, st, who, ts, dur, tok, act in ROWS:
    acts = "".join(f'<span style="color:#165DFF;font-size:12px;margin-right:10px">{a}</span>' for a in act.split())
    tr_html += f"""<tr><td><div class="mono" style="font-size:12px">{no}</div></td>
<td style="max-width:330px"><div style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:330px">{goal}</div></td>
<td>{st}</td><td class="mut">{who}</td><td class="mut num">{ts}</td>
<td class="num">{dur}</td><td class="num right">{tok}</td><td>{acts}</td></tr>"""

p02_body = f"""
<div class="page-head">
  <div><h1>工作台</h1><div class="sub">2026-10-21 星期三 · 数据更新于 16:52 · 自动刷新 30s</div></div>
  <div class="btns"><button class="btn">导出当前页</button><button class="btn pri">+ 新建任务</button></div>
</div>
<div class="card">
  <div class="filter">
    <div class="sel"><span class="lb">时间</span>今日（10-21）<span class="caret"></span></div>
    <div class="sel"><span class="lb">任务状态</span>全部<span class="caret"></span></div>
    <div class="sel"><span class="lb">范围</span>仅本人<span class="caret"></span></div>
    <span class="chip">已完成 96</span><span class="chip">执行中 4</span><span class="chip hi">待审批 3</span><span class="chip">失败 5</span>
    <div style="flex:1"></div>
    <span class="mut">更新于 16:52:08</span>
    <button class="btn sm">刷新</button>
    <div style="display:flex;align-items:center;gap:6px;font-size:12px;color:#4E5969"><span class="sw on" style="transform:scale(.85)"><i></i></span>自动</div>
  </div>
</div>
<div class="kpis">{kpi_html}</div>
<div class="grid3">
  <div class="card"><div class="card-h"><h3>任务完成趋势</h3><span class="hint">按天 · 近 7 天</span></div><div style="padding:10px 12px 4px">{trend}</div></div>
  <div class="card"><div class="card-h"><h3>工具调用分布</h3><span class="hint">tool.result 口径</span></div><div style="padding:12px 12px 4px">{donut}</div></div>
  <div class="card"><div class="card-h"><h3>失败原因 Top 5</h3><span class="hint">tasks.error.code</span></div><div style="padding:16px 16px 6px">{fail_html}</div></div>
</div>
<div class="card">
  <div class="tbar"><div class="srch">{ICONS['search']}搜索任务目标 / 编号</div>
    <div class="sel" style="height:30px"><span class="lb">状态</span>全部<span class="caret"></span></div>
    <div style="flex:1"></div>
    <span class="mut">共 128 条</span><button class="btn sm">列设置</button><button class="btn sm">紧凑</button></div>
  <table class="k"><thead><tr><th style="width:150px">任务编号</th><th>任务目标</th><th style="width:100px">状态</th><th style="width:80px">创建人</th><th style="width:110px">创建时间</th><th style="width:80px">耗时</th><th style="width:74px" class="right">Token</th><th style="width:170px">操作</th></tr></thead>
  <tbody>{tr_html}</tbody></table>
  <div class="pager"><span>共 128 条 · 每页 20 条</span>
    <div class="pg"><span>‹</span><span class="on">1</span><span>2</span><span>3</span><span>4</span><span>›</span></div></div>
</div>"""

# ---------------------------------------------------------------- P03 提交
p03_body = f"""
<div class="page-head"><div><h1>新建任务</h1><div class="sub">用一句业务目标 + 业务时间范围发起一次多智能体编排</div></div>
<div class="btns"><button class="btn">返回列表</button></div></div>
<div class="cols">
  <div class="card">
    <div style="padding:18px 16px 0">
      <div class="field"><div class="lb"><span class="req">*</span>任务目标</div>
        <div class="ta">分析 2026 年 9 月女装类目的 GMV 环比下滑原因，按品类与渠道拆分，给出 Top3 归因与可执行改进建议，并生成趋势对比图。
        <span class="cnt">86 / 500</span></div>
        <div style="font-size:11px;color:#86909C;margin-top:6px;line-height:18px">建议包含：分析对象 · 指标 · 时间 · 期望产出。目标越具体，Planner 规划越稳定（≤500 字）。</div>
      </div>
      <div class="field"><div class="lb"><span class="req">*</span>业务时间范围</div>
        <div class="range"><span>2026-09-01</span><span class="sep">→</span><span>2026-09-30</span></div>
        <div style="font-size:11px;color:#86909C;margin-top:6px">与自然月对齐；跨度建议 ≤ 90 天，过大会触发大范围扫描审批。</div>
      </div>
    </div>
    <div style="border-top:1px solid #F2F3F5">
      <div class="adv">
        <div style="display:flex;align-items:center;gap:8px;font-size:13px;color:#1D2129;padding:12px 0 4px"><span style="color:#86909C">▾</span>高级选项<span class="mut">（可选）</span></div>
        <div class="row-l"><div>同步等待执行<div style="font-size:11px;color:#86909C;margin-top:2px">开启后请求会阻塞到任务终态或下次挂起，页面可能等待较久</div></div><div class="sw"><i></i></div></div>
        <div class="row-l"><div class="ro">上下文压缩<span class="tag t-off">待接入</span></div><span class="mut">由服务端策略控制</span></div>
        <div class="row-l"><div class="ro">分层记忆注入<span class="tag t-off">待接入</span></div><span class="mut">默认读取长期记忆 ≤3 条</span></div>
        <div class="row-l"><div class="ro">标记为评测样本<span class="tag t-off">待接入</span></div><span class="mut">评测集由看板统一管理</span></div>
        <div style="font-size:11px;color:#86909C;line-height:18px;margin-top:10px;background:#F7F8FA;padding:8px 10px;border-radius:2px">提交接口当前仅接受 goal / time_range / sync；其余开关为只读展示，待后端开放后再启用。</div>
      </div>
    </div>
    <div class="form-foot"><button class="btn pri">提交并开始执行</button><button class="btn">重置</button><span class="mut" style="margin-left:auto;align-self:center">提交后立即进入详情页，可实时观察执行</span></div>
  </div>
  <div style="display:flex;flex-direction:column;gap:12px">
    <div class="card"><div class="card-h"><h3>提交后会发生什么</h3></div>
      <div style="padding:6px 16px 14px">
        <div class="step"><b>1</b><div><span style="color:#1D2129">Planner 规划</span><br>生成 DAG 子任务计划并做无环校验</div></div>
        <div class="step"><b>2</b><div><span style="color:#1D2129">Executor 执行</span><br>按依赖顺序调用 sql-query 等 MCP 工具</div></div>
        <div class="step"><b>3</b><div><span style="color:#1D2129">人工审批</span><br>高危工具 / 大范围扫描会挂起等待审核</div></div>
        <div class="step"><b>4</b><div><span style="color:#1D2129">Reviewer 评审</span><br>5 维打分（有据性 / 忠实度 / 自洽性…）</div></div>
        <div class="step"><b>5</b><div><span style="color:#1D2129">Summarizer 汇总</span><br>生成 Markdown 报告并引用图表与知识库</div></div>
        <div class="step"><b>6</b><div><span style="color:#1D2129">Notifier 通知</span><br>按需推送到运营群</div></div>
      </div></div>
    <div class="card" style="background:#FFFDF9;border-color:#FFCF8B"><div style="padding:14px 16px;font-size:12px;color:#874A00;line-height:20px">
      <b style="font-weight:500">审批提示</b><br>目标是「全量 / 全站 / 扫描」或涉及 notify 推送时，大概率触发人工闸门，任务会停在「待审批」，需审核人处理后才继续。</div></div>
  </div>
</div>"""

# ---------------------------------------------------------------- 共用详情头部
def detail_head(state_tag, awaiting=False):
    banner = ""
    if awaiting:
        banner = """<div class="banner warn"><span style="font-size:14px">⚠</span>
<div><b style="font-weight:500">等待人工审批</b> · 类型 HIGH_RISK_TOOL · 子任务 #5 推送结论到运营群 命中高危工具 notify</div>
<div style="margin-left:auto;font-variant-numeric:tabular-nums">剩余 21:14:03</div>
<button class="btn sm" style="background:#FF7D00;border-color:#FF7D00;color:#fff">查看审批</button></div>"""
    return f"""
<div class="dh">
  <div style="display:flex;align-items:center;gap:14px;padding-top:2px">
    <div class="dh-top"><h1 class="mono" style="font-size:17px">AF20261021-0007</h1>{state_tag}
      <span class="tag t-hi" style="margin-left:2px">高危子任务 ×1</span></div>
    <div style="flex:1"></div>
    <div class="btns"><button class="btn dgr">取消任务</button><button class="btn">以此目标新建</button>
    <button class="btn">导出完整 Trace</button><button class="btn">刷新</button></div>
  </div>
  <div class="dh-meta">
    <div class="mi">任务 ID<b class="mono">1024</b></div>
    <div class="mi">trace_id<b class="mono">7f3a9c2e1b04</b><span style="color:#165DFF;font-size:11px">复制</span></div>
    <div class="mi">创建人<b>张三</b></div>
    <div class="mi">创建时间<b class="num">2026-10-21 15:02:41</b></div>
    <div class="mi">总 Token<b class="num">8,214</b></div>
    <div class="mi">总耗时<b class="num">{'—' if awaiting else '▶ 03:12'}</b></div>
    <div class="mi">业务时间范围<b class="num">2026-09-01 → 09-30</b></div>
    <div class="mi">重规划<b>0 / 2 次</b></div>
  </div>
  <div class="goal" style="margin-bottom:14px"><div class="k2">目标</div>
  <div>分析 2026 年 9 月女装类目的 GMV 环比下滑原因，按品类与渠道拆分，给出 Top3 归因与可执行改进建议，并生成趋势对比图。</div></div>
  {banner}
</div>"""


def dag_card(awaiting=False, svg_h=290):
    return f"""
<div class="card" style="margin-top:12px">
  <div class="dagbar">
    <div style="font-size:13px;font-weight:500">DAG 编排视图</div>
    <span class="tag t-off">只读</span>
    <div style="flex:1"></div>
    <div class="zoom"><span>−</span><span>100%</span><span>+</span></div>
    <button class="btn sm">适应画布</button><button class="btn sm">全屏</button>
    <div class="legend"><span><i style="background:#00B42A"></i>完成</span><span><i style="background:#165DFF"></i>运行中</span>
    <span><i style="background:#FF7D00"></i>待审批</span><span><i style="background:#C9CDD4"></i>未开始</span><span><i style="background:#F53F3F"></i>失败</span></div>
  </div>
  <div style="padding:6px 8px 0;background:#FCFCFD;border-radius:0 0 4px 4px">{dag(awaiting)}</div>
</div>"""


TABS_A = f"""<div class="tabs"><div class="tab on">子任务与工具调用 <span class="c">5</span></div>
<div class="tab">执行轨迹 Trace <span class="c">18</span></div><div class="tab">产物与报告</div><div class="tab">记忆命中 <span class="c">3</span></div></div>"""
TABS_B = f"""<div class="tabs"><div class="tab">子任务与工具调用 <span class="c">5</span></div>
<div class="tab on">执行轨迹 Trace <span class="c">18</span></div><div class="tab">产物与报告</div><div class="tab">记忆命中 <span class="c">3</span></div></div>"""

# ---------------------------------------------------------------- P04a 子任务 Tab
SUB = [
    ("1", "拉取 9 月女装 GMV 明细", "sql-query", "high", tag("t-ok", "完成", False), "0"),
    ("2", "计算环比与同比", "sql-query", "normal", tag("t-run", "执行中"), "0"),
    ("3", "生成趋势对比图", "chart-render", "normal", tag("t-off", "待执行"), "0"),
    ("4", "检索历史复盘结论", "kb-search", "normal", tag("t-off", "待执行"), "0"),
    ("5", "推送结论到运营群", "notify", "high", tag("t-off", "待执行"), "0"),
]
sub_rows = ""
for seq, name, tool, risk, st, retry in SUB:
    rk = '<span class="chip hi">高危</span>' if risk == "high" else '<span class="mut">—</span>'
    indent = "" if seq in ("1", "4") else "margin-left:14px;"
    sub_rows += f"""<tr><td style="width:34px"><span class="mono mut">#{seq}</span></td>
<td><div style="{indent}display:flex;align-items:center;gap:6px">{'<span class="mut" style="font-size:11px">↳</span>' if indent else ''}{name}</div></td>
<td><span class="chip mono" style="font-size:11px">{tool}</span></td><td>{rk}</td><td>{st}</td><td class="mut num">{retry}</td></tr>"""

JSON_CALL = """<div class="json"><span class="p">{</span>
  <span class="k">"sql"</span>: <span class="s">"SELECT category, SUM(gmv) FROM dwd_order</span>
<span class="s">          WHERE dt BETWEEN '2026-09-01' AND '2026-09-30'</span>
<span class="s">          AND category LIKE '%女装%' GROUP BY 1"</span>,
  <span class="k">"db"</span>: <span class="s">"dwd"</span>,
  <span class="k">"estimated_rows"</span>: <span class="n">2104300</span>
<span class="p">}</span></div>"""

JSON_RES = """<div class="json"><span class="p">{</span>
  <span class="k">"ok"</span>: <span class="n">true</span>,
  <span class="k">"rows"</span>: <span class="n">1284</span>,
  <span class="k">"latency_ms"</span>: <span class="n">1810</span>,
  <span class="k">"result_digest"</span>: <span class="s">"女装 GMV 9 月 4,812 万，环比 -12.4%"</span>
<span class="p">}</span></div>"""

p04a_body = detail_head(tag("t-run", "执行中")) + dag_card() + f"""
<div class="card" style="margin-top:12px">
  {TABS_A}
  <div class="split">
    <div>
      <div class="tbar" style="padding:10px 16px"><div style="font-size:12px;color:#86909C">共 5 个子任务 · 依赖按 depends_on 缩进</div>
        <div style="flex:1"></div><span class="mut">视图</span><button class="btn sm">表格</button><button class="btn sm gho">树形</button></div>
      <table class="k"><thead><tr><th style="width:34px">#</th><th>子任务名称</th><th style="width:126px">工具</th><th style="width:74px">风险</th><th style="width:96px">状态</th><th style="width:60px">重试</th></tr></thead>
      <tbody>{sub_rows}</tbody></table>
      <div style="padding:10px 16px;font-size:11px;color:#86909C;border-top:1px solid #F2F3F5">子任务耗时未在接口出参，节点耗时取自 Trace 中同节点 span（近似值）</div>
      <div style="padding:4px 16px 14px;border-top:1px solid #F2F3F5">
        <div class="h4s" style="margin:10px 0 6px"><span>子任务产物引用（artifact_ids）</span><span>共 3 个</span></div>
        <div class="callrow" style="border-bottom:1px dashed #F2F3F5"><span class="mono mut">#1</span><span>dwd_gmv_202609_monthly</span><span class="chip" style="font-size:11px">table</span><div style="flex:1"></div><span class="mut">1,284 行</span></div>
        <div class="callrow" style="border-bottom:1px dashed #F2F3F5"><span class="mono mut">#3</span><span>gmv_mom_trend_202609</span><span class="chip" style="font-size:11px">chart</span><div style="flex:1"></div><span class="mut">png · 待生成</span></div>
        <div class="callrow" style="border-bottom:0"><span class="mono mut">#4</span><span>历史复盘片段 ×3</span><span class="chip" style="font-size:11px">kb</span><div style="flex:1"></div><span class="mut">待检索</span></div>
        <div style="font-size:11px;color:#86909C;margin-top:8px">依赖关系：#2 依赖 #1 · #3 依赖 #2 · #4 独立 · #5 依赖 #3 / #4</div>
      </div>
    </div>
    <div class="pane">
      <div class="h4s"><span>子任务 #2 · 计算环比与同比</span><span style="display:flex;gap:6px">{tag('t-run','执行中')}</span></div>
      <div style="display:flex;gap:6px;margin-bottom:10px"><span class="chip mono" style="font-size:11px">sql-query</span><span class="chip hi">高危</span><span class="chip">重试 0 次</span></div>
      <div class="h4s"><span>描述</span></div>
      <div style="font-size:12.5px;color:#4E5969;line-height:20px">基于 #1 的月度明细，计算 9 月相对 8 月的环比、相对去年 9 月的同比，并标注异常品类。</div>
      <div class="h4s" style="margin-top:14px"><span>结果摘要</span><span class="mut">执行中</span></div>
      <div style="font-size:12.5px;color:#86909C;background:#F7F8FA;border-radius:2px;padding:9px 11px">等待工具返回，暂未生成结果摘要</div>
      <div class="h4s" style="margin-top:14px"><span>工具调用（2）</span><span class="mut">按时间窗近似归属</span></div>
      <div class="callrow"><span class="tag t-info">call</span><span class="mono">sql-query</span><span class="mut num">15:04:12</span>
        <div style="flex:1"></div><span class="mut">展开入参 ▾</span></div>
      <div style="margin:6px 0 4px">{JSON_CALL}</div>
      <div class="callrow"><span class="tag t-ok" style="background:#E8FFEA;color:#00B42A;border-color:#7BE188">result</span><span class="mono">sql-query</span><span class="mut num">15:04:14</span>
        <div style="flex:1"></div><span class="num" style="font-size:11px">1.8s</span><span class="tag t-ok" style="height:18px">ok</span></div>
      <div style="margin-top:6px">{JSON_RES}</div>
      <div style="margin-top:14px;display:flex;align-items:center;gap:8px;font-size:11px;color:#86909C;background:#F7F8FA;padding:8px 10px;border-radius:2px">
        工具调用按节点 + 时间窗近似归属（Trace 接口未返回 subtask_id），精确关联待后端补字段。</div>
    </div>
  </div>
</div>"""

# ---------------------------------------------------------------- P04b Trace Tab
TRACE = [
    ("12", "planner", "enter", "开始规划", "0.3s", "10%", "120/0", "15:02:03", ""),
    ("13", "planner", "exit", "产出 5 个子任务 · 无环校验通过", "1.2s", "22%", "820/210", "15:02:04", ""),
    ("14", "executor", "tool.call", "sql-query · date_range 2026-09-01~30", "38ms", "4%", "0/0", "15:02:06", ""),
    ("15", "executor", "tool.result", "sql-query · 1284 行 · 女装 GMV 环比 -12.4%", "1.8s", "30%", "0/0", "15:02:08", "g"),
    ("16", "human_gate", "enter", "命中高危工具 → 挂起等待审批", "0.1s", "2%", "0/0", "15:02:10", "w"),
    ("17", "human_gate", "exit", "审批 #128 已批准 · 审核人 李四", "21m04s", "100%", "0/0", "15:23:14", "g"),
    ("18", "executor", "enter", "子任务 #2 计算环比与同比", "0.2s", "3%", "310/40", "15:23:15", ""),
    ("19", "executor", "tool.call", "sql-query · category='女装' AND dt IN (8月,9月)", "26ms", "3%", "0/0", "15:23:16", ""),
    ("20", "executor", "error", "TOOL_TIMEOUT · 超时重试第 1 次（最多 2 次）", "2.4s", "38%", "0/0", "15:23:19", "r"),
]
tr_rows = ""
for sid, node, ev, desc, lat, pct, tok, ts, k in TRACE:
    ntag = {"planner": "t-summ", "executor": "t-info", "human_gate": "t-wait"}.get(node, "t-off")
    ecls = "t-err" if ev == "error" else ("t-off" if ev in ("enter", "exit") else "t-info")
    bcls = {"g": "g", "r": "r", "w": ""}.get(k, "")
    tr_rows += f"""<div class="trow {('err' if k == 'r' else '')}">
<span class="mono mut">#{sid}</span><span><span class="tag {ntag}" style="height:19px;font-size:11px">{node}</span></span>
<span><span class="tag {ecls}" style="height:19px;font-size:11px;border-color:transparent">{ev}</span></span>
<span style="color:#4E5969;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">{desc}</span>
<span style="display:flex;align-items:center;gap:6px"><div class="bar {bcls}" style="flex:1"><i style="width:{pct}"></i></div><span class="mut num" style="width:40px;text-align:right">{lat}</span></span>
<span class="mono mut" style="text-align:right">{tok}</span><span class="mono mut">{ts}</span></div>"""

JSON_ERR = """<div class="json"><span class="p">{</span>
  <span class="k">"tool"</span>: <span class="s">"sql-query"</span>,
  <span class="k">"ok"</span>: <span class="n">false</span>,
  <span class="k">"error_code"</span>: <span class="s">"TOOL_TIMEOUT"</span>,
  <span class="k">"error_message"</span>: <span class="s">"query exceeded 2400ms budget"</span>,
  <span class="k">"attempts"</span>: <span class="n">1</span>,
  <span class="k">"degraded"</span>: <span class="n">false</span>
<span class="p">}</span></div>"""

JSON_MSG = """<div class="json" style="max-height:86px;overflow:hidden"><span class="p">[{</span><span class="k">"role"</span>: <span class="s">"system"</span>, <span class="k">"content"</span>: <span class="s">"你是电商经营分析助手…"</span><span class="p">},</span>
<span class="p"> {</span><span class="k">"role"</span>: <span class="s">"user"</span>, <span class="k">"content"</span>: <span class="s">"计算 9 月女装 GMV 环比…"</span><span class="p">}]</span></div>"""

JSON_NOTIFY = """<div class="json"><span class="p">{</span>
  <span class="k">"tool"</span>: <span class="s">"notify"</span>,
  <span class="k">"channel"</span>: <span class="s">"wecom-group/女装运营群"</span>,
  <span class="k">"message_digest"</span>: <span class="s">"9月女装GMV 4,812万，环比 -12.4%，Top1 归因：退货率上升 3.2pt…"</span>,
  <span class="k">"attachments"</span>: <span class="n">2</span>
<span class="p">}</span></div>"""

CHK = """<div style="display:flex;align-items:center;gap:10px;padding:8px 16px;border-bottom:1px solid #F2F3F5;background:#FCFCFD;font-size:11px;color:#86909C">
<span>checkpoint 轨道</span>
<div style="flex:1;height:3px;background:#F2F3F5;position:relative">
<span style="position:absolute;left:8%;top:-3px;width:9px;height:9px;background:#fff;border:1.5px solid #165DFF;border-radius:50%"></span>
<span style="position:absolute;left:34%;top:-3px;width:9px;height:9px;background:#fff;border:1.5px solid #FF7D00;border-radius:50%"></span>
<span style="position:absolute;left:62%;top:-3px;width:9px;height:9px;background:#165DFF;border:1.5px solid #165DFF;border-radius:50%"></span>
</div><span>step 4 · interrupt · plan_size 5 / done 2</span></div>"""

p04b_body = detail_head(tag("t-run", "执行中")) + dag_card() + f"""
<div class="card" style="margin-top:12px">
  {TABS_B}
  <div class="trace-ctl">
    <div class="play"><svg width="10" height="10" viewBox="0 0 10 10"><path d="M2 1l7 4-7 4z" fill="#fff"/></svg></div>
    <div class="speeds"><span>0.5x</span><span class="on">1x</span><span>2x</span><span>4x</span></div>
    <div class="track"><i></i><b></b></div>
    <span class="mut num" style="font-size:12px">9 / 18</span>
    <div style="display:flex;align-items:center;gap:5px;font-size:12px;color:#4E5969"><span style="width:12px;height:12px;border:1px solid #E5E6EB;border-radius:2px;display:inline-block"></span>仅看异常</div>
    <div class="sel" style="height:28px;font-size:12px"><span class="lb">节点</span>全部<span class="caret"></span></div>
    <span class="tag t-run" style="height:22px"><i></i>实时</span>
  </div>
  {CHK}
  <div class="split">
    <div>
      <div class="trow hd"><span>步骤</span><span>节点</span><span>事件</span><span>对象 / 摘要</span><span>耗时</span><span>Token</span><span>时间</span></div>
      {tr_rows}
      <div style="padding:8px 16px;font-size:11px;color:#86909C;border-top:1px solid #F2F3F5;display:flex;justify-content:space-between">
        <span>新事件实时追加，不阻塞界面 · 手动上滚后停止自动跟随</span><span style="color:#165DFF">回到最新 ↓</span></div>
    </div>
    <div class="pane">
      <div class="h4s"><span>步骤 #20 · executor / error</span><span class="tag t-err" style="height:19px">异常</span></div>
      <div style="font-size:12px;color:#4E5969;line-height:20px;margin-bottom:10px">2026-10-21 15:23:19.402 · 耗时 2.4s · token 0/0</div>
      <div class="h4s"><span>payload</span><span class="mut">复制 JSON</span></div>
      {JSON_ERR}
      <div style="background:#FFECE8;border:1px solid #FBACA3;border-radius:2px;padding:9px 11px;font-size:12px;color:#8B1F1F;line-height:19px;margin-top:10px">
        工具超时，Executor 将按策略重试；重试 2 次仍失败会触发 replan（当前 replan_count 0 / 2）。</div>
      <div class="h4s" style="margin-top:14px"><span>查看原始 LLM 输入 / 输出</span><span class="mut">来自 snapshot.values.messages</span></div>
      {JSON_MSG}
      <div class="h4s" style="margin-top:12px"><span>上下文记账</span></div>
      <div style="display:flex;gap:16px;font-size:12px;color:#4E5969">
        <span>压缩前 <b class="num" style="font-weight:500">5,120</b></span><span>压缩后 <b class="num" style="font-weight:500">2,860</b></span><span class="tag t-ok" style="height:19px">已压缩</span></div>
    </div>
  </div>
</div>"""

# ---------------------------------------------------------------- P04c 审批抽屉
p04c_body = detail_head(tag("t-wait", "待审批"), awaiting=True) + dag_card(awaiting=True) + f"""
<div class="card" style="margin-top:12px">
  <div class="tabs"><div class="tab on">子任务与工具调用 <span class="c">5</span></div>
  <div class="tab">执行轨迹 Trace <span class="c">12</span></div><div class="tab">产物与报告</div><div class="tab">记忆命中 <span class="c">3</span></div></div>
  <div style="padding:20px;color:#86909C;font-size:12px;display:flex;align-items:center;gap:8px">
    <span class="tag t-wait"><i></i>任务挂起中</span>审批完成后将从此断点继续执行，届时面板自动刷新</div>
</div>
<div class="scrim"></div>
<div class="drawer">
  <div class="dr-h">
    <div style="display:flex;align-items:center;gap:8px"><span style="font-size:15px;font-weight:500">审批 · #128</span>
      <span class="tag t-hi" style="height:20px;font-size:11px">HIGH_RISK_TOOL</span>
      <div style="flex:1"></div><span class="mut" style="font-size:16px">✕</span></div>
    <div style="display:flex;align-items:center;gap:8px;margin-top:8px;font-size:12px;color:#86909C">
      任务 AF20261021-0007 · 子任务 #5 推送结论到运营群
      <div style="margin-left:auto;color:#FF7D00;font-variant-numeric:tabular-nums">剩余 21:14:03</div></div>
  </div>
  <div class="dr-b">
    <div class="sec-t">风险原因</div>
    <div class="riskbox">子任务 #5 将调用 <b style="font-weight:500">notify</b> 工具向「女装运营群」推送消息，命中高危工具策略（外部可见动作），需人工确认内容与范围后放行。</div>
    <div class="sec-t">高危子任务（2）</div>
    <div style="border:1px solid #E5E6EB;border-radius:2px">
      <div style="display:flex;align-items:center;gap:8px;padding:9px 11px;border-bottom:1px solid #F2F3F5;font-size:12.5px">
        <span class="mono mut">#1</span><span>拉取 9 月女装 GMV 明细</span><span class="chip mono" style="font-size:11px">sql-query</span>
        <div style="flex:1"></div><span class="chip hi">高危</span><span class="tag t-ok" style="height:19px">已完成</span></div>
      <div style="display:flex;align-items:center;gap:8px;padding:9px 11px;font-size:12.5px">
        <span class="mono mut">#5</span><span>推送结论到运营群</span><span class="chip mono" style="font-size:11px">notify</span>
        <div style="flex:1"></div><span class="chip hi">高危</span><span class="tag t-off" style="height:19px">待执行</span></div>
    </div>
    <div class="sec-t">产物 / 参数预览（已脱敏）</div>
    {JSON_NOTIFY}
    <div class="sec-t">批准后将继续执行</div>
    <div style="display:flex;align-items:center;gap:8px;font-size:12px;flex-wrap:wrap">
      <span class="chip">notifier</span><span class="mut">→</span><span class="chip">reviewer</span><span class="mut">→</span>
      <span class="chip">summarizer</span><span class="mut">→</span><span class="chip">notifier</span><span class="mut">→</span><span class="chip">END</span></div>
    <div class="sec-t">驳回意见 <span class="req">*</span> <span class="mut">驳回时必填，将作为重规划的硬约束</span></div>
    <div class="ta" style="min-height:74px;color:#C9CDD4">例如：推送范围过大，仅保留 Top3 归因，去掉明细附件后重新生成</div>
  </div>
  <div class="dr-f"><span class="mut" style="margin-right:auto;font-size:11px">审批结果将写入审批单并恢复图执行</span>
    <button class="btn dgr">驳 回</button><button class="btn pri">批 准</button></div>
</div>"""

# ---------------------------------------------------------------- P05 收件箱
APV = [
    ("AF20261021-0006", "生成本周库存周转异常 SKU 清单并推送运营群", "HIGH_RISK_TOOL", "notify 推送外部群 · 内容未审核", "1", "李四", "10-21 14:47", "21:14:03", "warn"),
    ("AF20261021-0007", "分析 2026 年 9 月女装类目 GMV 环比下滑原因", "HIGH_RISK_TOOL", "sql-query 全表扫描 210 万行", "2", "张三", "10-21 15:02", "22:58:41", ""),
    ("AF20261020-0009", "扫描全站 SKU 价格带并输出竞品对标", "LARGE_SCAN", "扫描区间 366 天，超出阈值 90 天", "1", "王五", "10-20 18:20", "01:52:10", "red"),
]
ap_rows = ""
for no, goal, typ, reason, n, who, ts, left, cls in APV:
    tcls = "t-hi" if typ == "HIGH_RISK_TOOL" else "t-mid"
    color = {"warn": "#FF7D00", "red": "#F53F3F", "": "#4E5969"}[cls]
    ap_rows += f"""<tr><td style="width:40px"><span style="display:inline-block;width:14px;height:14px;border:1px solid #C9CDD4;border-radius:2px"></span></td>
<td style="white-space:nowrap"><div class="mono" style="font-size:12px">{no}</div></td>
<td style="max-width:300px"><div style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:300px">{goal}</div></td>
<td><span class="tag {tcls}" style="height:20px;font-size:11px">{typ}</span></td>
<td class="mut" style="max-width:180px"><div style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:180px">{reason}</div></td>
<td class="mut">{who}</td><td class="mut num" style="white-space:nowrap">{ts}</td>
<td class="num" style="color:{color};white-space:nowrap">{left}</td>
<td style="white-space:nowrap"><span style="color:#165DFF;font-size:12px;margin-right:12px">查看</span><span style="color:#00B42A;font-size:12px;margin-right:12px">批准</span><span style="color:#F53F3F;font-size:12px">驳回</span></td></tr>"""

p05_body = f"""
<div class="page-head"><div><h1>审批收件箱</h1><div class="sub">所有卡在人工闸门的任务 · 默认按过期时间升序（最紧急优先）</div></div>
<div class="btns"><button class="btn">刷新</button><span class="mut" style="font-size:12px">⟳ 30s 自动刷新</span></div></div>
<div class="card">
  <div class="tabs" style="gap:26px"><div class="tab on">待审批 <span class="c">3</span></div><div class="tab">已处理 <span class="c" style="background:#F2F3F5">128</span></div></div>
  <div class="tbar"><div class="sel" style="height:30px"><span class="lb">审批类型</span>全部<span class="caret"></span></div>
    <div class="sel" style="height:30px"><span class="lb">创建人</span>全部<span class="caret"></span></div>
    <span class="chip hi">2 条 2 小时内到期</span><div style="flex:1"></div><span class="mut">共 3 条待审批</span></div>
  <table class="k"><thead><tr><th style="width:40px"></th><th style="width:148px">任务编号</th><th>任务目标</th><th style="width:132px">审批类型</th>
    <th style="width:182px">触发风险原因</th><th style="width:66px">创建人</th><th style="width:96px">创建时间</th><th style="width:94px">倒计时</th><th style="width:156px">操作</th></tr></thead>
  <tbody>{ap_rows}</tbody></table>
</div>
<div class="card" style="margin-top:12px;display:flex;align-items:center;gap:14px;padding:12px 16px;border-color:#94BFFF;background:#F7FAFF">
  <span style="font-size:13px">已选 <b style="color:#165DFF;font-weight:500">1</b> 项</span>
  <span class="mut">批量操作逐条调用审批接口，非原子操作，失败项会单独列出</span>
  <div style="flex:1"></div>
  <button class="btn sm">取消勾选</button><button class="btn sm dgr">批量驳回</button><button class="btn sm pri">批量批准</button>
</div>"""

# ---------------------------------------------------------------- P06 评测
def cmp_card(title, sub, cls, win, metrics):
    rows = ""
    for label, val, is_win in metrics:
        mark = ' <span style="color:#165DFF;font-size:11px">★</span>' if is_win else ""
        rows += f'<div class="m"><span>{label}</span><b>{val}{mark}</b></div>'
    return f"""<div class="cmp {cls}"><div class="hd"><div>{title}<div class="mut" style="font-weight:400;font-size:11px;margin-top:3px">{sub}</div></div>
{win}</div><div style="margin-top:8px">{rows}</div></div>"""

ab_chart = """<svg width="100%" viewBox="0 0 1080 236" font-family="inherit">
<g stroke="#F2F3F5"><line x1="52" y1="20" x2="1060" y2="20"/><line x1="52" y1="70" x2="1060" y2="70"/><line x1="52" y1="120" x2="1060" y2="120"/><line x1="52" y1="170" x2="1060" y2="170"/></g>
<g fill="#86909C" font-size="10"><text x="16" y="24">100%</text><text x="22" y="74">75%</text><text x="22" y="124">50%</text><text x="22" y="174">25%</text></g>
<g>
<rect x="90" y="76" width="46" height="94" fill="#165DFF"/><rect x="142" y="85" width="46" height="85" fill="#94BFFF"/>
<text x="113" y="68" font-size="11" fill="#165DFF" text-anchor="middle">96.7</text><text x="165" y="77" font-size="11" fill="#4E5969" text-anchor="middle">93.3</text>
<text x="139" y="190" font-size="11" fill="#4E5969" text-anchor="middle">任务完成率</text>

<rect x="330" y="79" width="46" height="91" fill="#165DFF"/><rect x="382" y="78" width="46" height="92" fill="#94BFFF"/>
<text x="353" y="71" font-size="11" fill="#165DFF" text-anchor="middle">91.2</text><text x="405" y="70" font-size="11" fill="#4E5969" text-anchor="middle">92.0</text>
<text x="379" y="190" font-size="11" fill="#4E5969" text-anchor="middle">工具选择准确率</text>

<rect x="570" y="80" width="46" height="90" fill="#00B42A"/><rect x="622" y="95" width="46" height="75" fill="#7BE188"/>
<text x="593" y="72" font-size="11" fill="#00B42A" text-anchor="middle">412</text><text x="645" y="87" font-size="11" fill="#4E5969" text-anchor="middle">468</text>
<text x="619" y="190" font-size="11" fill="#4E5969" text-anchor="middle">P50 时延 (ms)</text>

<rect x="810" y="70" width="46" height="100" fill="#00B42A"/><rect x="862" y="60" width="46" height="110" fill="#7BE188"/>
<text x="833" y="62" font-size="11" fill="#00B42A" text-anchor="middle">721</text><text x="885" y="52" font-size="11" fill="#4E5969" text-anchor="middle">845</text>
<text x="859" y="190" font-size="11" fill="#4E5969" text-anchor="middle">P95 时延 (ms)</text>
</g>
<g font-size="11"><rect x="470" y="212" width="9" height="9" fill="#165DFF"/><text x="484" y="220" fill="#4E5969">compress_on</text>
<rect x="590" y="212" width="9" height="9" fill="#94BFFF"/><text x="604" y="220" fill="#4E5969">compress_off</text></g>
</svg>"""

RUNS = [
    ("EV-20261021-04", "ab_test", "compress_on", "agentflow-30", tag("t-run", "运行中"), "—", "—", "—", "—", "10-21 16:40"),
    ("EV-20261021-03", "ab_test", "compress_off", "agentflow-30", tag("t-ok", "完成", False), "93.3%", "92.0%", "845ms", "3,298", "10-21 11:26"),
    ("EV-20261021-02", "regression", "—", "agentflow-30", tag("t-ok", "完成", False), "100%", "—", "721ms", "2,840", "10-21 11:20"),
    ("EV-20261020-01", "reviewer_gate", "—", "agentflow-30", tag("t-err", "失败"), "—", "—", "—", "—", "10-20 20:02"),
]
run_rows = ""
for no, src, var, ds, st, comp, tool, lat, tok, ts in RUNS:
    scls = {"ab_test": "t-summ", "regression": "t-info", "reviewer_gate": "t-off"}[src]
    run_rows += f"""<tr><td class="mono" style="font-size:12px">{no}</td><td><span class="tag {scls}" style="height:20px;font-size:11px">{src}</span></td>
<td class="mono mut" style="font-size:11px">{var}</td><td class="mono mut" style="font-size:11px">{ds}</td><td>{st}</td>
<td class="num">{comp}</td><td class="num">{tool}</td><td class="num">{lat}</td><td class="num right">{tok}</td><td class="mut num">{ts}</td>
<td><span style="color:#165DFF;font-size:12px">查看</span></td></tr>"""

p06_body = f"""
<div class="page-head"><div><h1>评测与实验看板</h1><div class="sub">用指标判断策略是否值得开启 · 指标口径来自 eval_runs.metrics</div></div>
<div class="btns"><button class="btn">离线评测报告</button><button class="btn pri">新建运行</button></div></div>
<div class="card"><div class="tabs"><div class="tab on">A/B 实验对比</div><div class="tab">评测运行列表 <span class="c">4</span></div></div>
  <div class="filter"><div class="sel"><span class="lb">数据集</span>agentflow-30<span class="caret"></span></div>
  <div class="sel"><span class="lb">来源</span>ab_test<span class="caret"></span></div>
  <div class="sel"><span class="lb">时间</span>近 7 天<span class="caret"></span></div><div style="flex:1"></div>
  <span class="mut">对比基线：最近两条 DONE 运行 · 差异 &gt;5% 标记 ★</span></div>
</div>
<div class="compare" style="margin-top:12px">
  {cmp_card("上下文压缩 ON", "compress_on · EV-20261021-02", "win", '<span class="tag t-ok" style="height:20px">综合胜出</span>', [("任务完成率", "96.7%", True), ("工具选择准确率", "91.2%", False), ("P50 / P95 时延", "412 / 721ms", True), ("Token 成本", "2,840", True)])}
  {cmp_card("上下文压缩 OFF", "compress_off · EV-20261021-03", "", '<span class="mut" style="font-size:12px">基线</span>', [("任务完成率", "93.3%", False), ("工具选择准确率", "92.0%", True), ("P50 / P95 时延", "468 / 845ms", False), ("Token 成本", "3,298", False)])}
</div>
<div class="card" style="margin-bottom:12px"><div class="card-h"><h3>开启 / 关闭上下文压缩 · 指标对比</h3><span class="hint">同一评测集 30 条 · 时延与成本按组内最大值归一化</span></div>
  <div style="padding:12px 16px 4px">{ab_chart}</div></div>
<div class="card"><div class="card-h"><h3>评测运行</h3><span class="hint">status：PENDING / RUNNING / DONE / FAILED</span></div>
  <table class="k"><thead><tr><th style="width:140px">运行编号</th><th style="width:112px">来源</th><th style="width:112px">variant</th><th style="width:118px">数据集</th>
  <th style="width:92px">状态</th><th style="width:88px">完成率</th><th style="width:100px">工具准确率</th><th style="width:78px">P95</th><th style="width:80px" class="right">Token</th><th style="width:100px">创建时间</th><th style="width:56px">操作</th></tr></thead>
  <tbody>{run_rows}</tbody></table></div>"""

# ---------------------------------------------------------------- P07 记忆
MEM = [
    ("12", "女装类目 GMV 下滑优先看退货率与客单价，其次看流量结构；9 月平台活动节奏偏移是常见外因。", "long", 4, 12, "#1024", "10-20 09:12", "永久"),
    ("11", "大促期间同比口径需剔除预售定金，否则会低估当期 GMV 约 8%。", "long", 5, 31, "#0987", "10-01 11:40", "永久"),
    ("10", "库存周转异常清单默认按「周转天数 &gt; 45 且 环比恶化」筛选，推送前需人工确认范围。", "mid", 4, 6, "#1024", "10-18 15:22", "剩 27 天"),
    ("9", "John 偏好先给结论再给证据，报告首段不超过 3 行。", "mid", 3, 2, "—", "10-15 10:05", "剩 24 天"),
    ("8", "竞品价格带扫描容易被判为大范围扫描，超过 90 天区间会触发审批。", "long", 3, 9, "#0755", "09-28 16:48", "永久"),
]
mem_rows = ""
for mid, content, scope, imp, hit, task, ts, exp in MEM:
    scls = "t-summ" if scope == "long" else "t-info"
    stars = "★" * imp + f'<span class="off">{"★" * (5 - imp)}</span>'
    mem_rows += f"""<tr><td class="mono mut">{mid}</td>
<td style="max-width:520px"><div style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:520px">{content}</div></td>
<td><span class="tag {scls}" style="height:20px;font-size:11px">{scope}</span></td>
<td><span class="star">{stars}</span></td><td class="num">{hit}</td><td class="mono mut">{task}</td>
<td class="mut num" style="white-space:nowrap">{ts}</td><td class="mut" style="white-space:nowrap">{exp}</td>
<td><div style="display:flex;align-items:center;gap:12px"><span style="color:#165DFF;font-size:12px">详情</span><span style="color:#F53F3F;font-size:12px">删除</span></div></td></tr>"""

HITS = [("11", "大促期间同比口径需剔除预售定金…", "0.86", "long"), ("12", "女装类目 GMV 下滑优先看退货率…", "0.81", "long"), ("10", "库存周转异常清单默认按周转天数…", "0.46", "mid")]
hit_html = ""
for mid, txt, sc, scope in HITS:
    hit_html += f"""<div style="border:1px solid #E5E6EB;border-radius:2px;padding:9px 11px;margin-bottom:8px">
<div style="display:flex;align-items:center;gap:6px;font-size:11px;color:#86909C"><span class="mono">#{mid}</span><span class="tag t-info" style="height:18px;font-size:10px">{scope}</span>
<div style="flex:1"></div><span class="num" style="color:#165DFF">{sc}</span></div>
<div style="font-size:12px;color:#4E5969;margin:6px 0 7px;line-height:18px">{txt}</div>
<div style="height:4px;background:#F2F3F5;border-radius:2px"><div style="height:4px;width:{int(float(sc)*100)}%;background:#165DFF;border-radius:2px"></div></div></div>"""

p07_body = f"""
<div class="page-head"><div><h1>记忆管理</h1><div class="sub">分层记忆：短期由会话自动维护，中期 30 天滚动，长期永久保留</div></div>
<div class="btns"><button class="btn">相似度预览</button><button class="btn pri">+ 新增记忆</button></div></div>
<div class="cols" style="grid-template-columns:1fr 300px">
  <div style="display:flex;flex-direction:column;gap:12px">
  <div class="card">
    <div style="padding:12px 16px;border-bottom:1px solid #F2F3F5;display:flex;align-items:center;gap:12px">
      <div class="seg"><span>短期 0</span><span class="on">中期 42</span><span>长期 18</span></div>
      <div class="srch" style="width:200px;height:30px;border:1px solid #E5E6EB;border-radius:2px;display:flex;align-items:center;padding:0 10px;font-size:12px;color:#C9CDD4;gap:6px">{ICONS['search']}当前页内搜索</div>
      <div class="sel" style="height:30px;border:1px solid #E5E6EB;border-radius:2px;padding:0 10px;font-size:12px;display:flex;align-items:center;gap:8px;color:#4E5969"><span style="color:#86909C">task_id</span>全部</div>
      <div style="flex:1"></div><span class="mut">共 42 条</span></div>
    <table class="k"><thead><tr><th style="width:40px">ID</th><th>记忆内容</th><th style="width:62px">层级</th><th style="width:74px">重要度</th><th style="width:46px">命中</th><th style="width:62px">任务</th><th style="width:94px">创建时间</th><th style="width:54px">过期</th><th style="width:104px">操作</th></tr></thead>
    <tbody>{mem_rows}</tbody></table>
    <div class="pager"><span>共 42 条 · 每页 20 条</span><div class="pg"><span>‹</span><span class="on">1</span><span>2</span><span>3</span><span>›</span></div></div>
    <div style="padding:10px 16px;border-top:1px solid #F2F3F5;font-size:11px;color:#86909C;display:flex;align-items:center;gap:8px">
      <span class="tag t-off">短期层</span>短期记忆存放在 Redis，属于任务会话运行时缓存，管理接口不返回、不提供人工编辑入口。</div>
  </div>
  <div class="card">
    <div class="card-h"><h3>本次注入 Planner 的上下文</h3><span class="hint">memory_hits 上限 3 条 · 各 ≤200 token</span></div>
    <div style="padding:12px 16px;display:flex;gap:12px">
      <div style="flex:1;background:#F7F8FA;border:1px solid #F2F3F5;border-radius:2px;padding:9px 11px;font-size:12px;color:#4E5969;line-height:19px">
        命中 3 条 · 合计 412 字符 · 顺序按 score 降序，超限时按 importance 截断<br>
        <span class="mut">注入内容直接进入 PlannerInput.memory_hits，可在任务详情的 Trace 中核对实际输入</span></div>
      <div style="width:150px;text-align:center;border-left:1px dashed #F2F3F5;padding-left:12px">
        <div class="num" style="font-size:20px;font-weight:500">3</div><div class="mut" style="font-size:11px">条命中</div></div>
    </div>
  </div>
  </div>
  <div class="card" style="position:sticky;top:72px">
    <div class="card-h"><h3>相似度预览</h3><span class="hint">top_k ≤ 3</span></div>
    <div style="padding:12px 16px">
      <div style="height:32px;border:1px solid #E5E6EB;border-radius:2px;display:flex;align-items:center;padding:0 10px;font-size:12px;color:#1D2129">分析 9 月女装 GMV 环比下滑原因</div>
      <button class="btn sm pri" style="margin-top:8px;width:100%;justify-content:center">召回</button>
      <div class="h4s" style="margin-top:14px"><span>命中 3 条</span><span class="mut">store: chroma</span></div>
      {hit_html}

    </div>
  </div>
</div>"""

# ---------------------------------------------------------------- P08 角色权限视角（v1.0 对齐 PRD：无用户管理页）
# 三角色侧栏差异：每项 (label, admin可见, approver可见, viewer可见)
NAV_MATRIX = [
    ("工作台", True, True, True),
    ("新建任务", True, False, False),
    ("审批收件箱", True, True, False),
    ("评测与实验", True, True, True),
    ("记忆管理", True, False, False),
    ("系统设置", True, False, False),
]
ROLE_SIDE = [("运营管理员", "admin_user", "#165DFF", 1), ("审核人", "audit_lisi", "#FF7D00", 2), ("访客", "view_wangwu", "#86909C", 3)]
side_html = ""
for rname, ruser, rcolor, idx in ROLE_SIDE:
    items = ""
    for label, a, r, v in NAV_MATRIX:
        visible = (a, r, v)[idx - 1]
        if visible:
            items += f'<div class="rm-item">{label}</div>'
        else:
            items += f'<div class="rm-item rm-hide">{label}<span>无权限</span></div>'
    side_html += f"""<div class="card" style="padding:0;overflow:hidden">
  <div style="padding:12px 16px;border-bottom:1px solid #F2F3F5;display:flex;align-items:center;gap:8px">
    <span style="width:8px;height:8px;border-radius:50%;background:{rcolor}"></span>
    <b style="font-weight:500;font-size:13px">{rname}</b><span class="mono mut" style="font-size:11px">{ruser}</span>
  </div>
  <div style="background:#1D2129;padding:10px">{items}</div>
  <div style="padding:10px 16px;font-size:11px;color:#86909C;border-top:1px solid #F2F3F5">侧栏按角色裁剪 · 越权访问路由会被守卫拦回工作台</div>
</div>"""

# 功能点 × 角色矩阵：(功能, 层级, admin, approver, viewer, 控制方式)
PERM_ROWS = [
    ("登录 / 查看工作台", "页面", tag("t-ok", "✓", False), tag("t-ok", "✓", False), tag("t-ok", "✓", False), "登录即可"),
    ("新建任务", "按钮", tag("t-ok", "✓", False), tag("t-off", "隐藏", False), tag("t-off", "隐藏", False), "非 admin 隐藏菜单 + 禁用按钮"),
    ("任务详情", "页面", tag("t-ok", "✓", False), tag("t-ok", "✓", False), tag("t-mid", "限 COMPLETED", False), "viewer 越权读由后端 403 兜底"),
    ("取消任务", "按钮", tag("t-ok", "✓ 本人", False), tag("t-ok", "✓", False), tag("t-off", "隐藏", False), "按钮显隐 + require_role"),
    ("审批决策（抽屉）", "按钮", tag("t-ok", "✓", False), tag("t-ok", "✓", False), tag("t-off", "隐藏", False), "canDecide 计算属性控制"),
    ("审批收件箱", "页面", tag("t-ok", "✓", False), tag("t-ok", "✓", False), tag("t-off", "隐藏", False), "路由 meta.roles"),
    ("评测与实验", "页面", tag("t-ok", "✓ 完整", False), tag("t-ok", "✓ 完整", False), tag("t-mid", "汇总", False), "PRD 旅程三：访客只读汇总"),
    ("记忆管理", "页面", tag("t-ok", "✓", False), tag("t-off", "隐藏", False), tag("t-off", "隐藏", False), "路由 meta.roles + require_role"),
    ("系统设置", "页面", tag("t-ok", "✓", False), tag("t-off", "隐藏", False), tag("t-off", "隐藏", False), "路由 meta.roles"),
]
perm_rows = ""
for fn, layer, a, r, v, ctrl in PERM_ROWS:
    perm_rows += f'<tr><td>{fn}</td><td class="mut" style="width:60px">{layer}</td><td>{a}</td><td>{r}</td><td>{v}</td><td class="mut" style="font-size:11.5px">{ctrl}</td></tr>'

p08_body = f"""
<div class="page-head"><div><h1>全局权限控制 · 角色视角</h1>
<div class="sub">v1.0 对齐 PRD：不提供用户管理页面（账号由管理员预置数据库），权限能力通过 <span class="mono">路由守卫 + 按钮显隐 + 后端 require_role</span> 三层落地</div></div>
<div class="btns"><span class="tag t-info"><i></i>角色来自 GET /auth/me</span></div></div>
<div style="display:grid;grid-template-columns:repeat(3,1fr);gap:14px">{side_html}</div>
<div class="card" style="margin-top:14px"><div class="card-h"><h3>功能点 × 角色权限矩阵</h3><span class="hint">前端显隐只是体验优化，每个写接口后端仍做 require_role 二次校验</span></div>
<table class="k"><thead><tr><th style="width:170px">功能点</th><th style="width:60px">层级</th><th>运营管理员</th><th>审核人</th><th>访客</th><th style="width:260px">控制方式</th></tr></thead>
<tbody>{perm_rows}</tbody></table></div>
<div class="card" style="margin-top:14px;padding:14px 16px"><div style="font-size:12px;color:#4E5969;line-height:22px">
<b style="font-weight:500">落地机制</b>　
① 账号预置：管理员直接在数据库 / 种子脚本建号，页面无 CRUD 入口；
② 全局右上角用户下拉展示当前账号 + 角色名 + 退出登录；
③ 页面访问：vue-router <span class="mono">meta.roles</span> + 全局守卫，越权跳工作台并提示所需角色；
④ 按钮显隐：页面内 <span class="mono">canDecide / isAdmin</span> 等计算属性；
⑤ 安全边界：前端显隐不作为安全依赖，后端 <span class="mono">require_role()</span> 是唯一权威。</div></div>"""

# ---------------------------------------------------------------- P09 设置
TOOLS = [("sql-query", "在只读数仓执行 SQL，返回结果集与摘要", "mysql-warehouse", 12),
         ("chart-render", "根据数据渲染图表，输出 PNG 与引用 ID", "chart-worker", 4),
         ("kb-search", "检索经营知识库与历史复盘片段", "chroma-kb", 7),
         ("notify", "向企业微信群推送消息（高危，需审批）", "wecom-bridge", 2)]
tool_rows = ""
for name, desc, srv, calls in TOOLS:
    tool_rows += f"""<tr><td class="mono">{name}</td><td class="mut" style="max-width:300px">{desc}</td><td class="mono mut" style="font-size:11px">{srv}</td>
<td class="num right">{calls}</td><td><span style="color:#165DFF;font-size:12px">查看 Schema</span></td></tr>"""

p09_body = f"""
<div class="page-head"><div><h1>系统设置</h1><div class="sub">运行时配置以只读方式呈现 · 修改需运维调整环境变量后重启</div></div>
<div class="btns"><button class="btn">重新探测</button><span class="mut" style="font-size:12px">每 30s 轮询</span></div></div>
<div class="probe">
  <div class="pcard"><div class="t"><span class="okdot"></span>存活探针 /healthz</div><div class="rows"><div><b>状态</b>ok</div><div><b>版本</b>1.0.0</div><div><b>服务</b>agentflow-api</div></div></div>
  <div class="pcard"><div class="t"><span class="okdot"></span>就绪探针 /readyz</div><div class="rows"><div><b>MySQL</b><span class="ck">✓ 正常</span></div><div><b>Redis</b><span class="ck">✓ 正常</span></div><div><b>检查时间</b>16:52:31</div></div></div>
  <div class="pcard" style="border-color:#FFCF8B"><div class="t"><span style="width:8px;height:8px;border-radius:50%;background:#FF7D00;display:inline-block"></span>运行态摘要 /api/v1/health</div>
    <div class="rows"><div><b>环境</b>production</div><div><b>Mock</b>RAG 开 · 评测关</div><div><b>Token</b>access 2h / refresh 7d</div></div></div>
</div>
<div class="card"><div class="tabs"><div class="tab on">MCP 工具</div><div class="tab">开关配置</div><div class="tab">LLM 参数</div><div class="tab">日志</div></div>
  <div class="tbar"><div style="font-size:12px;color:#86909C">tools/list 结果 · 网关 <span class="mono">POST /mcp/v1</span> · 需 API Key</div>
  <div style="flex:1"></div><span class="tag t-ok" style="height:20px">网关已授权</span><button class="btn sm">刷新工具清单</button></div>
  <table class="k"><thead><tr><th style="width:150px">工具名</th><th>描述</th><th style="width:160px">server</th><th style="width:100px" class="right">近 7 天调用</th><th style="width:120px">操作</th></tr></thead>
  <tbody>{tool_rows}</tbody></table>
</div>
<div class="cols" style="grid-template-columns:1fr 1fr;margin-top:12px">
  <div class="card"><div class="card-h"><h3>开关配置</h3><span class="hint">只读 · 来源 /memories/meta 与运行时</span></div>
    <div style="padding:4px 16px 12px">
      <div class="row-l"><div>分层记忆 <span class="tag t-ok" style="height:19px">已启用</span></div><span class="mut">ENABLE_LAYERED_MEMORY = true</span></div>
      <div class="row-l"><div>短期层 TTL</div><span class="mono mut">redis · 3600s</span></div>
      <div class="row-l"><div>中期层 TTL</div><span class="mono mut">mysql.memories · 30 天</span></div>
      <div class="row-l"><div>长期层晋级</div><span class="mono mut">review score ≥ 4.0</span></div>
      <div class="row-l"><div>SSE 进度流</div><span class="tag t-ok" style="height:19px">已启用</span></div>
      <div class="row-l"><div>审批挂起截止</div><span class="mono mut">24h · 超时自动过期</span></div>
    </div></div>
  <div class="card"><div class="card-h"><h3>请求日志（最近）</h3><span class="hint">服务端结构化日志，仅展示样例</span></div>
    <div style="padding:12px 16px"><div class="logbox">
<div style="display:grid;grid-template-columns:58px 44px 1fr;gap:8px"><span style="color:#86909C">16:52:08</span><span style="color:#7BE188">INFO</span><span>task_stream_started  task_id=1024  trace=7f3a9c2e</span></div>
<div style="display:grid;grid-template-columns:58px 44px 1fr;gap:8px"><span style="color:#86909C">16:52:10</span><span style="color:#7BE188">INFO</span><span>tool_call  tool=sql-query  node=executor</span></div>
<div style="display:grid;grid-template-columns:58px 44px 1fr;gap:8px"><span style="color:#86909C">16:52:14</span><span style="color:#7BE188">INFO</span><span>human_gate  decision=approved  approval=128</span></div>
<div style="display:grid;grid-template-columns:58px 44px 1fr;gap:8px"><span style="color:#86909C">16:53:19</span><span style="color:#FFCF8B">WARN</span><span>tool_timeout  tool=sql-query  attempt=1</span></div>
<div style="display:grid;grid-template-columns:58px 44px 1fr;gap:8px"><span style="color:#86909C">16:53:21</span><span style="color:#7BE188">INFO</span><span>context_compressed  5120→2860  node=executor</span></div>
<div style="display:grid;grid-template-columns:58px 44px 1fr;gap:8px"><span style="color:#86909C">16:53:24</span><span style="color:#7BE188">INFO</span><span>sse_publish  event=state_update  cursor=1782</span></div>
</div>
<div style="font-size:11px;color:#86909C;margin-top:8px">日志查询接口未开放，排障请复制任务 trace_id 到日志平台检索。</div></div></div>
</div>"""

PAGES = [
    ("p01-login.html", p01, 900),
    ("p01b-login-states.html", p01b, 1000),
    ("p02-dashboard.html", shell("p02", "工作台", p02_body), 1080),
    ("p03-submit.html", shell("p02", "任务管理 / 新建任务", p03_body), 880),
    ("p04a-detail.html", shell("p02", "任务详情", p04a_body), 1170),
    ("p04b-trace.html", shell("p02", "任务详情 / Trace 回放", p04b_body), 1240),
    ("p04c-approval.html", shell("p05", "任务详情 / 审批", p04c_body, badge="3"), 960),
    ("p05-approvals.html", shell("p05", "审批收件箱", p05_body), 470),
    ("p06-eval.html", shell("p06", "评测看板", p06_body), 945),
    ("p07-memory.html", shell("p07", "记忆管理", p07_body), 730),
    ("p08-role-matrix.html", shell("p08", "全局权限控制 · 角色视角", p08_body, role="运营管理员"), 1180),
    ("p09-settings.html", shell("p09", "系统设置", p09_body), 900),
]

if __name__ == "__main__":
    for name, html, h in PAGES:
        with open(os.path.join(OUT, name), "w", encoding="utf-8") as f:
            f.write(html)
        print("wrote", name, h)
