# -*- coding: utf-8 -*-
"""AgentFlow UI prototype builder: writes static high-fidelity HTML pages."""
import os

OUT = os.path.dirname(os.path.abspath(__file__))

CSS = """
*{box-sizing:border-box}
body{margin:0;background:#F7F8FA;font-family:"PingFang SC","Microsoft YaHei",Inter,system-ui,sans-serif;color:#1D2129;font-size:14px;-webkit-font-smoothing:antialiased}
.side{position:fixed;left:0;top:0;bottom:0;width:220px;background:#fff;border-right:1px solid #E5E6EB;display:flex;flex-direction:column;z-index:6}
.brand{height:56px;display:flex;align-items:center;gap:9px;padding:0 16px;border-bottom:1px solid #F2F3F5}
.brand .mark{width:26px;height:26px;border-radius:6px;background:#165DFF;color:#fff;display:flex;align-items:center;justify-content:center;font-size:13px;font-weight:500}
.brand .nm{font-size:15px;font-weight:500;letter-spacing:-.2px}
.brand .ver{font-size:11px;color:#86909C;margin-left:auto}
.nav{padding:8px 0;flex:1}
.grp{padding:12px 16px 6px;font-size:11px;color:#86909C}
.item{height:38px;margin:2px 8px;padding:0 10px;border-radius:4px;display:flex;align-items:center;gap:9px;color:#4E5969;font-size:13px}
.item svg{width:15px;height:15px;stroke:#86909C;fill:none;stroke-width:1.5;flex:none}
.item.on{background:#E8F0FF;color:#165DFF;font-weight:500}
.item.on svg{stroke:#165DFF}
.item .badge{margin-left:auto;background:#F53F3F;color:#fff;height:16px;min-width:16px;border-radius:8px;font-size:11px;display:flex;align-items:center;justify-content:center;padding:0 4px}
.side-foot{border-top:1px solid #F2F3F5;padding:10px 16px;font-size:11px;color:#86909C;line-height:1.7}
.main{margin-left:220px;min-height:100vh}
.topbar{height:56px;background:#fff;border-bottom:1px solid #E5E6EB;display:flex;align-items:center;gap:14px;padding:0 20px;position:sticky;top:0;z-index:5}
.crumb{font-size:13px;color:#86909C;display:flex;align-items:center;gap:6px}
.crumb b{color:#1D2129;font-weight:400}
.crumb .sl{color:#C9CDD4}
.sp{flex:1}
.search{width:210px;height:30px;border:1px solid #E5E6EB;border-radius:2px;display:flex;align-items:center;gap:7px;padding:0 10px;color:#C9CDD4;font-size:12px;background:#F7F8FA}
.bell{position:relative;width:30px;height:30px;display:flex;align-items:center;justify-content:center;border:1px solid #E5E6EB;border-radius:4px}
.dot-red{position:absolute;top:4px;right:4px;width:6px;height:6px;background:#F53F3F;border-radius:50%;border:1px solid #fff}
.who{display:flex;align-items:center;gap:8px;padding-left:12px;border-left:1px solid #F2F3F5}
.ava{width:26px;height:26px;border-radius:50%;background:#E8F0FF;color:#165DFF;font-size:12px;display:flex;align-items:center;justify-content:center}
.who .n{font-size:12px;line-height:1.3}
.who .r{font-size:11px;color:#86909C}
.content{padding:16px 20px 28px}
.page-head{display:flex;align-items:flex-start;justify-content:space-between;margin-bottom:14px}
h1{margin:0;font-size:20px;line-height:28px;font-weight:500}
.sub{margin-top:3px;font-size:12px;color:#86909C}
.card{background:#fff;border:1px solid #E5E6EB;border-radius:4px}
.btns{display:flex;gap:8px;align-items:center}
.btn{height:32px;padding:0 13px;border:1px solid #E5E6EB;background:#fff;border-radius:2px;font-size:13px;color:#4E5969;display:inline-flex;align-items:center;gap:6px;font-family:inherit}
.btn.pri{background:#165DFF;border-color:#165DFF;color:#fff}
.btn.dgr{color:#F53F3F;border-color:#FBACA3}
.btn.sm{height:26px;padding:0 9px;font-size:12px}
.btn.tx{border:0;background:none;color:#165DFF;padding:0 4px}
.btn.gho{background:#F7F8FA}
.btn.off{color:#C9CDD4;border-color:#E5E6EB;background:#F7F8FA}
.tag{display:inline-flex;align-items:center;gap:5px;height:22px;padding:0 7px;border-radius:2px;font-size:12px;border:1px solid transparent;white-space:nowrap}
.tag i{width:6px;height:6px;border-radius:50%;font-style:normal}
.t-plan{background:#E8F3FF;color:#1677FF;border-color:#94BFFF}.t-plan i{background:#1677FF}
.t-run{background:#E8F0FF;color:#165DFF;border-color:#94BFFF}.t-run i{background:#165DFF}
.t-summ{background:#F5E8FF;color:#722ED1;border-color:#D3ADF7}.t-summ i{background:#722ED1}
.t-wait{background:#FFF7E8;color:#FF7D00;border-color:#FFCF8B}.t-wait i{background:#FF7D00}
.t-ok{background:#E8FFEA;color:#00B42A;border-color:#7BE188}
.t-err{background:#FFECE8;color:#F53F3F;border-color:#FBACA3}
.t-off{background:#F2F3F5;color:#86909C;border-color:#E5E6EB}
.t-hi{background:#FFECE8;color:#F53F3F;border-color:#FBACA3}
.t-mid{background:#FFF7E8;color:#FF7D00;border-color:#FFCF8B}
.t-info{background:#E8F3FF;color:#1677FF;border-color:#94BFFF}
.chip{display:inline-flex;align-items:center;height:22px;padding:0 7px;border-radius:2px;background:#F2F3F5;color:#4E5969;font-size:12px}
.chip.hi{background:#FFECE8;color:#F53F3F}
table.k{width:100%;border-collapse:collapse;table-layout:fixed}
.k th{height:38px;background:#F7F8FA;border-bottom:1px solid #E5E6EB;font-size:12px;font-weight:400;color:#4E5969;text-align:left;padding:0 12px;white-space:nowrap}
.k td{height:46px;border-bottom:1px solid #F2F3F5;padding:0 12px;font-size:13px;vertical-align:middle}
.k tr:last-child td{border-bottom:0}
.mut{color:#86909C;font-size:12px}
.mono{font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-size:12px}
.num{font-variant-numeric:tabular-nums}
.right{text-align:right}
.cols>div{min-width:0}
.filter{display:flex;align-items:center;gap:12px;padding:12px 16px}
.sel{height:32px;border:1px solid #E5E6EB;border-radius:2px;padding:0 10px;font-size:13px;color:#1D2129;display:inline-flex;align-items:center;gap:8px;background:#fff}
.sel .lb{color:#86909C;font-size:12px}
.caret{width:0;height:0;border-left:4px solid transparent;border-right:4px solid transparent;border-top:5px solid #86909C}
.kpis{display:grid;grid-template-columns:repeat(5,1fr);gap:12px;margin:12px 0}
.kpi{background:#fff;border:1px solid #E5E6EB;border-radius:4px;padding:14px 16px;position:relative}
.kpi h4{margin:0;font-size:12px;font-weight:400;color:#4E5969}
.kpi .v{font-size:26px;font-weight:600;line-height:34px;margin-top:6px;letter-spacing:-.5px;font-variant-numeric:tabular-nums}
.kpi .v small{font-size:12px;font-weight:400;color:#86909C;margin-left:4px;letter-spacing:0}
.kpi .f{font-size:11px;color:#86909C;margin-top:4px;display:flex;align-items:center;gap:5px}
.up{color:#00B42A}.down{color:#F53F3F}
.kpi .ic{position:absolute;right:14px;top:14px;width:26px;height:26px;border-radius:6px;background:#F7F8FA;display:flex;align-items:center;justify-content:center;font-size:11px;color:#86909C}
.kpi.warn{border-color:#FFCF8B;background:#FFFDF9}
.grid3{display:grid;grid-template-columns:1.4fr 1fr 1fr;gap:12px;margin-bottom:12px}
.card-h{display:flex;align-items:center;justify-content:space-between;padding:12px 16px;border-bottom:1px solid #F2F3F5}
.card-h h3{margin:0;font-size:14px;font-weight:500}
.hint{font-size:11px;color:#86909C}
.tbar{display:flex;gap:10px;align-items:center;padding:12px 16px;border-bottom:1px solid #F2F3F5}
.tbar .srch{width:260px;height:30px;border:1px solid #E5E6EB;border-radius:2px;display:flex;align-items:center;padding:0 10px;font-size:12px;color:#C9CDD4;gap:6px}
.pager{display:flex;justify-content:space-between;align-items:center;padding:12px 16px;font-size:12px;color:#86909C}
.pg{display:flex;gap:4px}
.pg span{min-width:26px;height:26px;border:1px solid #E5E6EB;border-radius:2px;display:flex;align-items:center;justify-content:center;font-size:12px;color:#4E5969}
.pg span.on{background:#165DFF;border-color:#165DFF;color:#fff}
.cols{display:grid;grid-template-columns:1fr 320px;gap:12px;align-items:start}
.field{margin-bottom:16px}
.lb{font-size:13px;color:#1D2129;margin-bottom:6px;display:flex;gap:4px;align-items:center}
.req{color:#F53F3F}
.ta{border:1px solid #E5E6EB;border-radius:2px;padding:10px 12px;font-size:13px;line-height:22px;color:#1D2129;min-height:92px;position:relative;background:#fff}
.ta .cnt{position:absolute;right:10px;bottom:6px;font-size:11px;color:#86909C}
.range{display:inline-flex;align-items:center;border:1px solid #E5E6EB;border-radius:2px;height:34px;font-size:13px;background:#fff}
.range span{padding:0 12px}
.range .sep{color:#C9CDD4;padding:0}
.adv{padding:0 16px 14px}
.row-l{display:flex;align-items:center;justify-content:space-between;padding:11px 0;border-bottom:1px dashed #F2F3F5;font-size:13px}
.row-l:last-child{border-bottom:0}
.sw{width:34px;height:18px;border-radius:9px;background:#C9CDD4;position:relative;flex:none}
.sw i{position:absolute;width:14px;height:14px;background:#fff;border-radius:50%;top:2px;left:2px}
.sw.on{background:#165DFF}
.sw.on i{left:18px}
.form-foot{display:flex;gap:10px;padding:14px 16px;border-top:1px solid #F2F3F5}
.step{display:flex;gap:10px;padding:9px 0;font-size:12px;color:#4E5969;line-height:18px}
.step b{width:18px;height:18px;border-radius:50%;background:#E8F0FF;color:#165DFF;font-size:11px;font-weight:500;display:flex;align-items:center;justify-content:center;flex:none;margin-top:1px}
.dh{background:#fff;border:1px solid #E5E6EB;border-radius:4px;padding:14px 16px 0}
.dh-top{display:flex;align-items:center;gap:10px}
.dh-top h1{font-size:17px}
.dh-meta{display:flex;flex-wrap:wrap;gap:8px 26px;margin-top:11px;padding-bottom:12px}
.mi{font-size:12px;color:#86909C;display:flex;gap:6px;align-items:center}
.mi b{font-weight:400;color:#1D2129;font-size:12px}
.goal{margin-top:2px;background:#F7F8FA;border-radius:2px;padding:9px 12px;font-size:13px;color:#4E5969;line-height:20px;display:flex;gap:10px}
.goal .k2{color:#86909C;font-size:12px;flex:none;padding-top:1px}
.banner{margin-top:12px;display:flex;align-items:center;gap:10px;padding:9px 12px;border-radius:2px;font-size:12px}
.banner.warn{background:#FFF7E8;border:1px solid #FFCF8B;color:#874A00}
.banner.err{background:#FFECE8;border:1px solid #FBACA3;color:#8B1F1F}
.banner.info{background:#E8F3FF;border:1px solid #94BFFF;color:#0E42D2}
.dagbar{display:flex;align-items:center;gap:10px;padding:9px 14px;border-bottom:1px solid #F2F3F5}
.zoom{display:flex;border:1px solid #E5E6EB;border-radius:2px;overflow:hidden;font-size:12px;color:#4E5969}
.zoom span{padding:3px 9px;border-left:1px solid #E5E6EB}
.zoom span:first-child{border-left:0}
.legend{display:flex;gap:14px;font-size:11px;color:#86909C;margin-left:auto}
.legend i{display:inline-block;width:7px;height:7px;border-radius:50%;margin-right:4px}
.tabs{display:flex;gap:26px;padding:0 16px;border-bottom:1px solid #E5E6EB;background:#fff}
.tab{height:44px;display:flex;align-items:center;gap:6px;font-size:13px;color:#4E5969;position:relative}
.tab.on{color:#165DFF;font-weight:500}
.tab.on:after{content:"";position:absolute;left:0;right:0;bottom:-1px;height:2px;background:#165DFF}
.tab .c{background:#F2F3F5;color:#4E5969;font-size:11px;padding:0 5px;border-radius:8px;line-height:16px}
.split{display:grid;grid-template-columns:1fr 430px}
.pane{padding:12px 16px;border-left:1px solid #F2F3F5}
.h4s{font-size:12px;color:#86909C;margin:0 0 8px;display:flex;justify-content:space-between;align-items:center}
.json{background:#F7F8FA;border:1px solid #F2F3F5;border-radius:2px;padding:9px 11px;font-family:ui-monospace,Consolas,monospace;font-size:12px;line-height:19px;white-space:pre;overflow:hidden;color:#1D2129}
.json .k{color:#722ED1}.json .s{color:#00843D}.json .n{color:#165DFF}.json .p{color:#86909C}
.callrow{display:flex;align-items:center;gap:8px;height:36px;border-bottom:1px dashed #F2F3F5;font-size:12px}
.bar{height:6px;background:#F2F3F5;border-radius:3px;position:relative;overflow:hidden}
.bar i{position:absolute;left:0;top:0;bottom:0;background:#165DFF;border-radius:3px}
.bar.g i{background:#00B42A}.bar.r i{background:#F53F3F}
.trace-ctl{display:flex;align-items:center;gap:12px;padding:10px 14px;border-bottom:1px solid #F2F3F5}
.play{width:26px;height:26px;border-radius:50%;background:#165DFF;display:flex;align-items:center;justify-content:center}
.speeds{display:flex;border:1px solid #E5E6EB;border-radius:2px;font-size:12px;overflow:hidden}
.speeds span{padding:3px 8px;color:#4E5969;border-left:1px solid #E5E6EB}
.speeds span:first-child{border-left:0}
.speeds span.on{background:#165DFF;color:#fff;border-color:#165DFF}
.track{flex:1;height:4px;background:#F2F3F5;border-radius:2px;position:relative}
.track i{position:absolute;left:0;top:0;bottom:0;width:62%;background:#165DFF;border-radius:2px}
.track b{position:absolute;left:62%;top:-4px;width:12px;height:12px;background:#fff;border:2px solid #165DFF;border-radius:50%}
.trow{display:grid;grid-template-columns:42px 84px 86px 1fr 136px 70px 78px;align-items:center;gap:8px;height:33px;padding:0 14px;border-bottom:1px solid #F7F8FA;font-size:12px}
.trow.hd{height:28px;background:#F7F8FA;color:#4E5969;font-size:11px;border-bottom:1px solid #E5E6EB}
.trow.err{background:#FFF5F3}
.scrim{position:fixed;top:0;left:0;width:100vw;height:100vh;background:#1D2129;opacity:.42;z-index:8}
.drawer{position:fixed;top:0;right:0;bottom:0;width:540px;background:#fff;z-index:9;display:flex;flex-direction:column;box-shadow:-8px 0 24px rgba(0,0,0,.14)}
.dr-h{padding:15px 20px;border-bottom:1px solid #F2F3F5}
.dr-b{flex:1;padding:4px 20px 16px}
.dr-f{padding:13px 20px;border-top:1px solid #E5E6EB;display:flex;justify-content:flex-end;align-items:center;gap:10px}
.sec-t{font-size:12px;color:#86909C;margin:16px 0 8px}
.riskbox{background:#FFF7E8;border:1px solid #FFCF8B;border-radius:2px;padding:10px 12px;font-size:13px;color:#874A00;line-height:20px}
.kv{display:flex;justify-content:space-between;font-size:13px;padding:8px 0;border-bottom:1px dashed #F2F3F5}
.mat{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-bottom:12px}
.mcard{background:#fff;border:1px solid #E5E6EB;border-radius:4px;padding:14px 16px}
.mcard h3{margin:0 0 4px;font-size:14px;font-weight:500;display:flex;align-items:center;gap:8px}
.mcard p{margin:0 0 10px;font-size:12px;color:#86909C;line-height:1.7}
.mline{display:flex;justify-content:space-between;font-size:12px;color:#4E5969;padding:5px 0;border-top:1px dashed #F2F3F5}
.rm-item{display:flex;align-items:center;gap:8px;padding:7px 12px;margin:2px 0;border-radius:3px;font-size:12.5px;color:#C9CDD4}
.rm-hide{color:#5F6B7A;background:rgba(255,255,255,.03)}
.rm-hide span{margin-left:auto;font-size:10px;color:#FF7D00;border:1px solid rgba(255,125,0,.35);border-radius:2px;padding:0 4px;line-height:15px}
.ck{color:#00B42A}.no{color:#C9CDD4}
.seg{display:inline-flex;border:1px solid #E5E6EB;border-radius:2px;overflow:hidden;font-size:13px;background:#fff}
.seg span{padding:5px 14px;color:#4E5969;border-left:1px solid #E5E6EB}
.seg span:first-child{border-left:0}
.seg span.on{background:#E8F0FF;color:#165DFF}
.probe{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-bottom:12px}
.pcard{background:#fff;border:1px solid #E5E6EB;border-radius:4px;padding:14px 16px}
.pcard .t{display:flex;align-items:center;gap:8px;font-size:13px;font-weight:500}
.pcard .rows{margin-top:10px;font-size:12px;color:#4E5969;line-height:1.9}
.pcard .rows b{font-weight:400;color:#86909C;display:inline-block;width:78px}
.okdot{width:8px;height:8px;border-radius:50%;background:#00B42A}
.logbox{background:#1D2129;border-radius:4px;padding:14px 16px;font-family:ui-monospace,Consolas,monospace;font-size:12px;line-height:20px;color:#C9D1D9;overflow:hidden}
.logbox .t{color:#86909C}
.logbox .w{color:#FFCF8B}
.compare{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:12px}
.cmp{background:#fff;border:1px solid #E5E6EB;border-radius:4px;padding:14px 16px}
.cmp.win{border-color:#94BFFF;box-shadow:0 0 0 1px #E8F0FF inset}
.cmp .hd{display:flex;align-items:center;justify-content:space-between;font-size:13px;font-weight:500}
.cmp .m{display:flex;justify-content:space-between;align-items:baseline;padding:8px 0;border-bottom:1px dashed #F2F3F5;font-size:13px}
.cmp .m span{color:#4E5969;font-size:12px}
.cmp .m b{font-weight:500;font-variant-numeric:tabular-nums}
.star{color:#FF7D00;letter-spacing:1px;font-size:12px}
.star .off{color:#E5E6EB}
"""

ICONS = {
    "dash": '<svg viewBox="0 0 16 16"><rect x="2" y="2" width="5" height="5" rx="1"/><rect x="9" y="2" width="5" height="5" rx="1"/><rect x="2" y="9" width="5" height="5" rx="1"/><rect x="9" y="9" width="5" height="5" rx="1"/></svg>',
    "task": '<svg viewBox="0 0 16 16"><path d="M3 4h10M3 8h10M3 12h6"/></svg>',
    "inbox": '<svg viewBox="0 0 16 16"><path d="M2 9l2-5h8l2 5v4H2z"/><path d="M2 9h4l1 2h2l1-2h4"/></svg>',
    "eval": '<svg viewBox="0 0 16 16"><path d="M3 13V7M8 13V3M13 13V9"/></svg>',
    "mem": '<svg viewBox="0 0 16 16"><path d="M8 2l6 3-6 3-6-3 6-3z"/><path d="M2 8l6 3 6-3"/><path d="M2 11l6 3 6-3"/></svg>',
    "user": '<svg viewBox="0 0 16 16"><circle cx="8" cy="5" r="2.6"/><path d="M3 14c0-2.6 2.2-4.2 5-4.2S13 11.4 13 14"/></svg>',
    "set": '<svg viewBox="0 0 16 16"><circle cx="8" cy="8" r="2.2"/><path d="M8 2v1.6M8 12.4V14M2 8h1.6M12.4 8H14M3.8 3.8l1.1 1.1M11.1 11.1l1.1 1.1M12.2 3.8l-1.1 1.1M4.9 11.1L3.8 12.2"/></svg>',
    "bell": '<svg width="15" height="15" viewBox="0 0 16 16" fill="none" stroke="#4E5969" stroke-width="1.4"><path d="M4.4 6.6a3.6 3.6 0 017.2 0c0 3 .9 3.9 1.3 4.4H3.1c.4-.5 1.3-1.4 1.3-4.4z"/><path d="M6.6 13a1.5 1.5 0 002.8 0"/></svg>',
    "search": '<svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="#C9CDD4" stroke-width="1.6"><circle cx="7" cy="7" r="4.2"/><path d="M10.2 10.2L14 14"/></svg>',
}

NAV_BIZ = [
    ("dash", "工作台", "p02"),
    ("task", "任务管理", None),
    ("inbox", "审批收件箱", "p05"),
    ("eval", "评测看板", "p06"),
    ("mem", "记忆管理", "p07"),
]
# v1.0 对齐 PRD：移除"用户与权限"页面（账号预置数据库，无页面 CRUD）；
# 权限控制效果见 p08-role-matrix.html 角色视角矩阵页。
NAV_SYS = [("set", "系统设置", "p09")]


def shell(active, crumb, content, user="张三", role="运营管理员", badge="3"):
    biz = ""
    for key, label, pid in NAV_BIZ:
        on = " on" if pid == active else ""
        b = f'<span class="badge">{badge}</span>' if key == "inbox" else ""
        biz += f'<div class="item{on}">{ICONS[key]}<span>{label}</span>{b}</div>'
    sys = ""
    for key, label, pid in NAV_SYS:
        on = " on" if pid == active else ""
        sys += f'<div class="item{on}">{ICONS[key]}<span>{label}</span></div>'
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>AgentFlow · {crumb}</title><style>{CSS}</style></head>
<body>
<div class="side">
  <div class="brand"><div class="mark">AF</div><div class="nm">AgentFlow</div><div class="ver">v1.0.0</div></div>
  <div class="nav">
    <div class="grp">业务</div>{biz}
    <div class="grp">系统</div>{sys}
  </div>
  <div class="side-foot">电商经营分析 Agent 平台<br>环境：生产 · 负责人 张三</div>
</div>
<div class="main">
  <div class="topbar">
    <div class="crumb">AgentFlow<span class="sl">/</span><b>{crumb}</b></div>
    <div class="sp"></div>
    <div class="search">{ICONS['search']}搜索任务编号 / trace_id</div>
    <div class="bell">{ICONS['bell']}<span class="dot-red"></span></div>
    <div class="who"><div class="ava">{user[0]}</div><div><div class="n">{user}</div><div class="r">{role}</div></div></div>
  </div>
  <div class="content">{content}</div>
</div>
</body></html>"""


def tag(cls, text, dot=True):
    d = '<i></i>' if dot else ''
    return f'<span class="tag {cls}">{d}{text}</span>'


def bar(pct, cls="", w=90):
    return f'<div class="bar {cls}" style="width:{w}px"><i style="width:{pct}%"></i></div>'


def dnode(x, y, kind, title, sub, status="ok", w=160, h=64):
    style = {
        "ok": ("#FFFFFF", "#E5E6EB", "#1D2129", "#86909C"),
        "run": ("#F7FAFF", "#165DFF", "#1D2129", "#165DFF"),
        "wait": ("#FFF7E8", "#FF7D00", "#1D2129", "#FF7D00"),
        "idle": ("#FFFFFF", "#E5E6EB", "#4E5969", "#86909C"),
        "err": ("#FFF5F3", "#F53F3F", "#1D2129", "#F53F3F"),
    }[status]
    fill, stroke, tc, sc = style
    letter = {"planner": "P", "executor": "E", "human": "H", "reviewer": "R", "summarizer": "S", "notifier": "N"}[kind]
    icbg = {"planner": "#722ED1", "executor": "#165DFF", "human": "#FF7D00", "reviewer": "#0F6E56", "summarizer": "#1677FF", "notifier": "#86909C"}[kind]
    sw = "2" if status in ("run", "wait", "err") else "1"
    dash = ' stroke-dasharray="5 4"' if kind == "human" else ""
    badge = ""
    gl, gbg, gfg = {
        "ok": ("✓", "#E8FFEA", "#00B42A"),
        "run": ("▶", "#165DFF", "#FFFFFF"),
        "wait": ("Ⅱ", "#FF7D00", "#FFFFFF"),
        "idle": ("○", "#F7F8FA", "#C9CDD4"),
        "err": ("✕", "#FFECE8", "#F53F3F"),
    }[status]
    return f"""<g>
<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="6" fill="{fill}" stroke="{stroke}" stroke-width="{sw}"{dash}/>
<rect x="{x+12}" y="{y+19}" width="26" height="26" rx="6" fill="{icbg}" opacity="{0.12 if status in ('ok','idle') else 0.16}"/>
<text x="{x+25}" y="{y+36}" font-size="12" fill="{icbg}" text-anchor="middle">{letter}</text>
<text x="{x+48}" y="{y+27}" font-size="12.5" fill="{tc}">{title}</text>
<text x="{x+48}" y="{y+45}" font-size="11" fill="{sc}">{sub}</text>
<circle cx="{x+w-17}" cy="{y+16}" r="7.5" fill="{gbg}"/>
<text x="{x+w-17}" y="{y+19.5}" font-size="9" fill="{gfg}" text-anchor="middle">{gl}</text>{badge}
</g>"""


def edge(d, color="#C9CDD4", dash="", w=1.5, marker=True):
    m = ' marker-end="url(#ar)"' if marker else ""
    ds = f' stroke-dasharray="{dash}"' if dash else ""
    return f'<path d="{d}" fill="none" stroke="{color}" stroke-width="{w}"{ds}{m}/>'


def dag(awaiting=False):
    xs = [24, 214, 404, 594, 784, 974]
    y1, y2 = 46, 196
    g_st = "wait" if awaiting else "ok"
    g_sub = "待审批" if awaiting else "已批准"
    n4 = "idle" if awaiting else "run"
    n4sub = "待执行" if awaiting else "运行中"
    nodes = [
        dnode(xs[0], y1, "planner", "Planner", "1.2s"),
        dnode(xs[1], y1, "executor", "子任务 #1", "sql-query"),
        dnode(xs[2], y1, "human", "人工审批", g_sub, g_st),
        dnode(xs[3], y1, "executor", "子任务 #2", n4sub, n4),
        dnode(xs[4], y1, "executor", "子任务 #3", "待执行", "idle"),
        dnode(xs[5], y1, "reviewer", "Reviewer", "待执行", "idle"),
        dnode(784, y2, "summarizer", "Summarizer", "待执行", "idle"),
        dnode(974, y2, "notifier", "Notifier", "待执行", "idle"),
    ]
    e = ""
    for i in range(5):
        e += edge(f"M{xs[i]+160} {y1+32} H{xs[i+1]-2}")
    e += edge(f"M1054 {y1+64} V150 H864 V192")
    e += edge("M1054 46 V18 H294 V42", color="#C9CDD4", dash="5 4", w=1)
    e += '<text x="676" y="14" font-size="11" fill="#86909C">replan 回边（本任务未触发 ×0）</text>'
    grid = "".join(f'<line x1="{i*24}" y1="0" x2="{i*24}" y2="290"/>' for i in range(48))
    return f"""<svg width="1150" height="290" viewBox="0 0 1150 290" font-family="PingFang SC, Microsoft YaHei, sans-serif">
<defs><marker id="ar" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
<path d="M2 1L8 5L2 9" fill="none" stroke="#9AA0A6" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></marker></defs>
<rect x="0" y="0" width="1150" height="290" fill="#FCFCFD"/>
<g stroke="#F7F8FA" stroke-width="1">{grid}</g>
{e}
{"".join(nodes)}
</svg>"""
