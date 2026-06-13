// Device web control panel (HTML/JS), kept in a .h so the Arduino .ino prototype
// generator never scans its JavaScript — putting JS `function`/`let` in a raw string
// inside the .ino makes arduino-cli emit bogus C prototypes ("function does not name a type").
#pragma once
const char PANEL[] PROGMEM = R"HTML(<!DOCTYPE html><html><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'><title>ClaudeTV</title><style>
body{font-family:system-ui,sans-serif;background:#000;color:#e6e9ef;margin:0;padding:16px;max-width:480px;margin:auto}
h1{font-size:22px;color:#fff;border-bottom:3px solid #ff7a55;padding-bottom:8px}.coral{color:#ff7a55}
.card{background:#171f2e;border-radius:12px;padding:14px;margin:12px 0}.row{display:flex;justify-content:space-between;align-items:center;margin:6px 0}
.big{font-size:26px;font-weight:700}.muted{color:#a4b0c2;font-size:13px}label{font-size:13px;color:#b0c4de}input[type=range]{width:100%}
button,select,input[type=number]{background:#232a36;color:#e6e9ef;border:1px solid #2c374a;border-radius:8px;padding:8px;font-size:14px}
button{cursor:pointer;width:100%}.grid{display:grid;grid-template-columns:1fr 1fr;gap:8px}.foot{text-align:center;font-size:12px;margin-top:14px}
</style></head><body>
<h1>Claude<span class=coral>TV</span> <span class=muted id=ver></span></h1>
<div class=card><div class=row><span>Session (5h)</span><span class=big id=sess>--</span></div><div class=muted id=sessr></div>
<div class=row><span>Week (7d)</span><span class=big id=week>--</span></div><div class=muted id=weekr></div>
<div class=row><span class=muted id=wx></span><span class=muted id=clock></span></div></div>
<div class=card><label>Brightness <span id=bril></span></label><input type=range min=0 max=100 id=bri oninput="set('bri',this.value)"></div>
<div class=card><div class=row><label>Night mode (auto-dim)</label><input type=checkbox id=nEn onchange="set('ne',this.checked?1:0)"></div>
<div class=grid><div><label>Start hr</label><input type=number min=0 max=23 id=nStart onchange="set('ns',this.value)"></div>
<div><label>End hr</label><input type=number min=0 max=23 id=nEnd onchange="set('nf',this.value)"></div></div>
<label>Night brightness <span id=nbril></span></label><input type=range min=0 max=100 id=nBri oninput="set('nb',this.value)"></div>
<div class=card><div class=row><label>Flip display 180&deg;</label><button style="width:auto" onclick="set('rot',-1)">Rotate</button></div>
<div class=row><label>Refresh (s)</label><input type=number min=5 max=120 id=refresh onchange="set('refresh',this.value)"></div></div>
<div class=card><label>Collector URL (host service)</label><input id=usage placeholder="http://host:8088/usage">
<div class=row style="margin-top:8px"><span id=cstat class=muted>&mdash;</span><button style="width:auto" onclick="applyUsage()">Apply</button></div></div>
<div class=card><button style="background:#39c3cd;color:#06222a;font-weight:700" onclick="fetch('/state').then(r=>r.json()).then(s=>window.open(s.terminal||'/','_blank'))">Master Terminal &#8599;</button></div>
<div class=card><div class=grid><button onclick="if(confirm('Reboot device?'))set('reboot',1)">Reboot</button><button onclick="location.href='/update'">Firmware OTA</button></div></div>
<div class=foot><a href="https://latticelabs.au" target=_blank style="color:#3fd2dd;text-decoration:none">lattice labs</a></div>
<script>
function set(k,v){fetch('/set?'+k+'='+v).then(load)}
function applyUsage(){cstat.textContent='Saving & testing…';fetch('/set?usage='+encodeURIComponent(usage.value)).then(()=>setTimeout(load,400))}
function load(){fetch('/state').then(r=>r.json()).then(s=>{ver.textContent='v'+s.ver;
sess.textContent=s.haveData?s.s+'%':'--';sessr.textContent=s.sr?('resets '+s.sr):'idle';
week.textContent=s.haveData?s.w+'%':'--';weekr.textContent=s.wr?('resets '+s.wr):'';
wx.textContent=s.city+' '+s.wt+'°C '+s.wc;clock.textContent=s.time;
bri.value=s.bri;bril.textContent=s.bri+'%';nEn.checked=s.ne;nStart.value=s.ns;nEnd.value=s.nf;nBri.value=s.nb;nbril.textContent=s.nb+'%';refresh.value=s.refresh;
if(document.activeElement!==usage)usage.value=s.usage||'';
cstat.innerHTML=s.conn==1?'<span style=color:#54d36e>&#10003; connected</span>':(s.conn==0?'<span style=color:#ff4d68>&#10007; unreachable</span>':'&hellip;');}).catch(()=>{})}
load();setInterval(load,2000);
</script></body></html>)HTML";
