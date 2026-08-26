// Device web control panel (HTML/JS), kept in a .h so the Arduino .ino prototype
// generator never scans its JavaScript — putting JS `function`/`let` in a raw string
// inside the .ino makes arduino-cli emit bogus C prototypes ("function does not name a type").
#pragma once
const char PANEL[] PROGMEM = R"HTML(<!DOCTYPE html><html lang=en><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'><title>ClaudeTV</title><style>
body{font-family:system-ui,sans-serif;background:#000;color:#e6e9ef;margin:0;padding:16px;max-width:480px;margin:auto}
h1{font-size:22px;color:#fff;border-bottom:3px solid #ff7a55;padding-bottom:8px}.coral{color:#ff7a55}
.card{background:#171f2e;border-radius:12px;padding:14px;margin:12px 0}.row{display:flex;justify-content:space-between;align-items:center;margin:6px 0}
.big{font-size:26px;font-weight:700}.muted{color:#a4b0c2;font-size:13px}label{font-size:13px;color:#b0c4de}input[type=range]{width:100%}
button,select,input[type=number]{background:#232a36;color:#e6e9ef;border:1px solid #2c374a;border-radius:8px;padding:8px;font-size:14px}
button{cursor:pointer;width:100%}.grid{display:grid;grid-template-columns:1fr 1fr;gap:8px}.foot{text-align:center;font-size:12px;margin-top:14px}
button.ghost{background:#1c2331;border:1px solid #2c374a}code{background:#0d1119;border:1px solid #2c374a;border-radius:6px;padding:2px 6px;font-size:12px}
</style></head><body>
<h1>Claude<span class=coral>TV</span> <span class=muted id=ver></span></h1>
<div class=card><div class=row><span class=muted id=acclbl></span><span class=muted id=accn></span></div>
<div class=row><span>Session (5h)</span><span class=big id=sess>--</span></div><div class=muted id=sessr></div>
<div class=row><span>Week (7d)</span><span class=big id=week>--</span></div><div class=muted id=weekr></div>
<div class=row id=fablerow style="display:none"><span id=fablelbl>Fable (7d)</span><span class=big id=fable>--</span></div>
<div class=row><span class=muted id=wx></span><span class=muted id=clock></span></div></div>
<div class=card id=acccard style="display:none"><span class=muted>Accounts on the display</span><div id=acclist class=muted></div>
<div class=row style="margin-top:8px"><label for=acyc>Seconds per account</label><input type=number min=3 max=60 id=acyc onchange="set('acyc',this.value)"></div>
<button class=ghost style="margin-top:8px" onclick="set('accnext',1)">Show next account now</button>
<div class=muted style="margin-top:8px">Accounts come from <b>claude-swap</b> on the collector host.
Add one there with <code>cswap add --alias &lt;name&gt;</code>, then use <b>Re-read accounts</b> in the Master Terminal.</div></div>
<div class=card><label for=bri>Brightness <span id=bril></span></label><input type=range min=0 max=100 id=bri oninput="set('bri',this.value)"></div>
<div class=card><div class=row><label for=nEn>Night mode (auto-dim)</label><input type=checkbox id=nEn onchange="set('ne',this.checked?1:0)"></div>
<div class=grid><div><label for=nStart>Start hr</label><input type=number min=0 max=23 id=nStart onchange="set('ns',this.value)"></div>
<div><label for=nEnd>End hr</label><input type=number min=0 max=23 id=nEnd onchange="set('nf',this.value)"></div></div>
<label for=nBri>Night brightness <span id=nbril></span></label><input type=range min=0 max=100 id=nBri oninput="set('nb',this.value)"></div>
<div class=card><div class=row><label>Flip display 180&deg;</label><button style="width:auto" onclick="set('rot',-1)">Rotate</button></div>
<div class=row><label for=refresh>Refresh (s)</label><input type=number min=5 max=120 id=refresh onchange="set('refresh',this.value)"></div></div>
<div class=card><label for=usage>Collector URL (host service)</label><input id=usage placeholder="http://host:8088/usage">
<div class=row style="margin-top:8px"><span id=cstat class=muted>&mdash;</span><button style="width:auto" onclick="applyUsage()">Apply</button></div></div>
<div class=card><button style="background:#39c3cd;color:#06222a;font-weight:700" onclick="fetch('/state').then(r=>r.json()).then(s=>window.open(s.terminal||'/','_blank'))">Master Terminal &#8599;</button></div>
<div class=card><div class=grid><button onclick="if(confirm('Reboot device?'))set('reboot',1)">Reboot</button><button onclick="location.href='/update'">Firmware OTA</button></div></div>
<div class=foot><a href="https://latticelabs.au" target=_blank style="color:#3fd2dd;text-decoration:none">lattice labs</a></div>
<script>
function set(k,v){fetch('/set?'+k+'='+v).then(load)}
function applyUsage(){cstat.textContent='Saving & testing…';fetch('/set?usage='+encodeURIComponent(usage.value)).then(()=>setTimeout(load,400))}
function load(){fetch('/state').then(r=>r.json()).then(s=>{ver.textContent='v'+s.ver;
// the card mirrors whichever account is on screen right now; the list below shows them all
acclbl.textContent=s.label||'';accn.textContent=(s.nacc>1)?((s.acci+1)+'/'+s.nacc):'';
acccard.style.display=(s.nacc>1)?'':'none';acyc.value=s.acyc;
acclist.innerHTML=(s.acc||[]).map((a,i)=>'<div class=row style=margin:2px:0><span>'+((i==s.acci)?'&#9679; ':'&#9675; ')
 +(a.l||('acct'+(i+1)))+(a.auth==2?' <span style=color:#ff4d68>expired</span>':'')+'</span><span>'
 +a.s+'% &middot; '+a.w+'%'+(a.f>=0?(' &middot; '+a.f+'%'):'')+'</span></div>').join('');
sess.textContent=s.haveData?s.s+'%':'--';sessr.textContent=s.sr?('resets '+s.sr):'idle';
week.textContent=s.haveData?s.w+'%':'--';weekr.textContent=s.wr?('resets '+s.wr):'';
if(s.f>=0){fablerow.style.display='';fablelbl.textContent=(s.fl||'Fable')+' (7d)';fable.textContent=s.haveData?s.f+'%':'--';}else fablerow.style.display='none';
wx.textContent=s.city+' '+s.wt+'°C '+s.wc;clock.textContent=s.time;
bri.value=s.bri;bril.textContent=s.bri+'%';nEn.checked=s.ne;nStart.value=s.ns;nEnd.value=s.nf;nBri.value=s.nb;nbril.textContent=s.nb+'%';refresh.value=s.refresh;
if(document.activeElement!==usage)usage.value=s.usage||'';
cstat.innerHTML=s.conn==1?'<span style=color:#54d36e>&#10003; connected</span>':(s.conn==0?'<span style=color:#ff4d68>&#10007; unreachable</span>':'&hellip;');}).catch(()=>{})}
load();setInterval(load,2000);
</script></body></html>)HTML";
