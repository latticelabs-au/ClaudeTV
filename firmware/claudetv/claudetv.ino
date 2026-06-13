/*
 * ClaudeTV — GeekMagic SmallTV-Ultra (ESP8266 / ESP-12F, ST7789V 240x240)
 * Dashboard: Claude session(5h)/week(7d) usage % + reset times (two-column hero cards),
 * cycling weather turntable, big clock, Lattice Labs logo. Web control panel + OTA.
 * Silent: TFT_eSPI with ONE held-open SPI transaction (CS low). Settings persist in EEPROM.
 * Full rounded-card redraws (no partial-clear seams). Secrets in config.h (gitignored).
 *   http://claudetv.local/
 */
#include <ESP8266WiFi.h>
#include <ESP8266WebServer.h>
#include <ESP8266HTTPUpdateServer.h>
#include <ESP8266HTTPClient.h>
#include <ESP8266mDNS.h>
#include <WiFiClient.h>
#include <WiFiManager.h>
#include <TFT_eSPI.h>
#include <ArduinoJson.h>
#include <EEPROM.h>
#include "config.h"
#include "logo.h"

#define FW_NAME "ClaudeTV"
#define FW_VER  "4.1"
#define TZ_STR  "AEST-10AEDT,M10.1.0,M4.1.0/3"
#define TFT_BL  5
#define WX_CYCLE_MS 4000

TFT_eSPI tft = TFT_eSPI();
ESP8266WebServer        server(80);
ESP8266HTTPUpdateServer httpUpdater;

uint16_t C_BG, C_PANEL, C_LINE, C_CORAL, C_CYAN, C_WHITE, C_GRAY, C_DIM, C_GREEN, C_AMBER, C_RED, C_SKY;
unsigned long lastFetch=0, lastClock=0, lastWx=0;
int connOK=-1, wxIdx=0; bool haveData=false, nightActive=false;
struct { int s=0, w=0, wt=-999, wfl=-999, whi=-999, wlo=-999, wrain=-999, whum=-999; String sr, wr, wc, city; } U;
int pS=-99, pW=-99, pConn=-1; String pSR="\x01", pWR="\x01", pDate="\x01";

struct Settings { uint8_t magic, bri, nEn, nStart, nEnd, nBri, rot; uint16_t refresh; } S;
const uint8_t MAGIC = 0xC4;

// geometry (1:1 with emulator)
const int UCX=8, UCY=33, UCW=224, UCH=79, LCX=64, RCX=176;
const int WCX=8, WCY=118, WCW=224, WCH=54;
const int TIME_Y=196, DATE_Y=222;

void saveSettings(){ EEPROM.put(0,S); EEPROM.commit(); }
void defaults(){ S = {MAGIC, 60, 1, 21, 7, 30, 0, 20}; }
uint16_t lvl(int p){ if(p<0)return C_GRAY; if(p>=85)return C_RED; if(p>=50)return C_AMBER; return C_GREEN; }
void applyBacklight(){ int b=nightActive?S.nBri:S.bri; b=constrain(b,0,100); analogWrite(TFT_BL,(100-b)*255/100); }
bool isNight(int hr){ if(!S.nEn||S.nStart==S.nEnd)return false; return S.nStart<S.nEnd?(hr>=S.nStart&&hr<S.nEnd):(hr>=S.nStart||hr<S.nEnd); }
// transparent text (bg arg ignored) — every card/region is fully cleared before drawing,
// so transparent glyphs never leave the panel-colored bounding boxes that overflow card edges.
void str(const char* s,int x,int y,const GFXfont* f,uint16_t col,uint8_t d,uint16_t bg){ (void)bg; tft.setFreeFont(f); tft.setTextDatum(d); tft.setTextColor(col); tft.drawString(s,x,y); }

void wxMetric(int i,const char*& lbl,int& val,bool& temp){
  switch(i){ case 0:lbl="NOW";val=U.wt;temp=true;break; case 1:lbl="FEELS";val=U.wfl;temp=true;break;
    case 2:lbl="HIGH";val=U.whi;temp=true;break; case 3:lbl="LOW";val=U.wlo;temp=true;break;
    case 4:lbl="RAIN";val=U.wrain;temp=false;break; default:lbl="HUM";val=U.whum;temp=false;break; }
}
void drawLogo(){ tft.setSwapBytes(true); tft.pushImage(186,184,LOGO_W,LOGO_H,LOGO); tft.setSwapBytes(false); }

void drawUsageCard(){
  tft.fillRoundRect(UCX,UCY,UCW,UCH,10,C_PANEL);
  tft.drawFastVLine(120,UCY+14,UCH-28,C_LINE);
  // session (left col): reset centered under the column
  str("SESSION",LCX,UCY+15,&FreeSans9pt7b,C_GRAY,MC_DATUM,C_PANEL);
  char b[8]; if(haveData)snprintf(b,sizeof b,"%d%%",U.s); else strcpy(b,"--");
  str(b,LCX,UCY+43,&FreeSansBold18pt7b,lvl(haveData?U.s:-1),MC_DATUM,C_PANEL);
  str(U.sr.length()?U.sr.c_str():"idle",UCX+12,UCY+67,&FreeSansBold9pt7b,C_DIM,ML_DATUM,C_PANEL);
  // week (right col): reset right-aligned so the wide date+time never crosses the card edge
  str("WEEK",RCX,UCY+15,&FreeSans9pt7b,C_GRAY,MC_DATUM,C_PANEL);
  if(haveData)snprintf(b,sizeof b,"%d%%",U.w); else strcpy(b,"--");
  str(b,RCX,UCY+43,&FreeSansBold18pt7b,lvl(haveData?U.w:-1),MC_DATUM,C_PANEL);
  str(U.wr.length()?U.wr.c_str():"--",UCX+UCW-12,UCY+67,&FreeSansBold9pt7b,C_DIM,MR_DATUM,C_PANEL);
}
void drawWeatherCard(){
  tft.fillRoundRect(WCX,WCY,WCW,WCH,10,C_PANEL);
  str(U.city.length()?U.city.c_str():"weather",WCX+12,WCY+19,&FreeSans9pt7b,C_WHITE,ML_DATUM,C_PANEL);
  str(U.wc.length()?U.wc.c_str():"--",WCX+12,WCY+38,&FreeSans9pt7b,C_SKY,ML_DATUM,C_PANEL);
  const char* lbl; int val; bool temp; wxMetric(wxIdx,lbl,val,temp);
  str(lbl,WCX+WCW-14,WCY+16,&FreeSans9pt7b,C_GRAY,MR_DATUM,C_PANEL);
  char b[8];
  if(val<=-999){ str("--",WCX+WCW-14,WCY+38,&FreeSansBold12pt7b,C_GRAY,MR_DATUM,C_PANEL); return; }
  if(temp){ snprintf(b,sizeof b,"%d",val); str(b,WCX+WCW-20,WCY+38,&FreeSansBold12pt7b,C_WHITE,MR_DATUM,C_PANEL);
            tft.drawCircle(WCX+WCW-14,WCY+31,2,C_WHITE); }
  else    { snprintf(b,sizeof b,"%d%%",val); str(b,WCX+WCW-14,WCY+38,&FreeSansBold12pt7b,C_WHITE,MR_DATUM,C_PANEL); }
}
void drawDot(){ tft.fillCircle(18,DATE_Y,3,connOK==1?C_GREEN:C_RED); }
void drawClock(){
  time_t now=time(nullptr); char hms[12],dat[18];
  if(now<100000){strcpy(hms,"--:--:--");strcpy(dat,"syncing");}
  else{struct tm* t=localtime(&now); strftime(hms,sizeof hms,"%H:%M:%S",t); strftime(dat,sizeof dat,"%a %d %b",t);
       bool n=isNight(t->tm_hour); if(n!=nightActive){nightActive=n;applyBacklight();}}
  tft.fillRect(12,TIME_Y-15,158,30,C_BG);
  str(hms,16,TIME_Y,&FreeSansBold18pt7b,C_WHITE,ML_DATUM,C_BG);
  // repaint the date every second with a generous clear -> no leftover smooth-font residue
  tft.fillRect(26,DATE_Y-13,164,26,C_BG);
  str(dat,28,DATE_Y,&FreeSans9pt7b,C_GRAY,ML_DATUM,C_BG);
}
void header(){
  tft.fillRoundRect(12,12,9,9,2,C_CORAL);
  str("CLAUDE USAGE",26,9,&FreeSansBold12pt7b,C_WHITE,TL_DATUM,C_BG);
}
void render(bool force){
  if(force||U.s!=pS||U.w!=pW||U.sr!=pSR||U.wr!=pWR){drawUsageCard();pS=U.s;pW=U.w;pSR=U.sr;pWR=U.wr;}
  if(force||connOK!=pConn){drawDot();pConn=connOK;}
}
void fullRedraw(){
  tft.fillScreen(C_BG); header(); drawLogo();
  pS=pW=-99; pConn=-1; pSR=pWR=pDate="\x01";
  render(true); drawWeatherCard(); drawClock();
}

void splash(){
  tft.fillScreen(C_BG); tft.setSwapBytes(true); tft.pushImage(120-LOGO_W/2,66,LOGO_W,LOGO_H,LOGO); tft.setSwapBytes(false);
  str("ClaudeTV",120,150,&FreeSansBold18pt7b,C_CORAL,MC_DATUM,C_BG);
  str("lattice labs",120,182,&FreeSans9pt7b,C_CYAN,MC_DATUM,C_BG);
}

void fetchUsage(){
  WiFiClient client; HTTPClient http; http.setTimeout(6000);
  if(!http.begin(client,USAGE_URL)){connOK=0;return;}
  int code=http.GET();
  if(code==200){
    JsonDocument doc;
    if(deserializeJson(doc,http.getString())==DeserializationError::Ok){
      connOK=1;
      if((doc["ok"]|0)==1){haveData=true; U.s=doc["s"]|0; U.w=doc["w"]|0; U.sr=String((const char*)(doc["sr"]|"")); U.wr=String((const char*)(doc["wr"]|""));}
      if(doc["wt"].is<int>()){ U.wt=doc["wt"]|-999; U.wfl=doc["wfl"]|-999; U.whi=doc["whi"]|-999; U.wlo=doc["wlo"]|-999;
        U.wrain=doc["wrain"]|-999; U.whum=doc["whum"]|-999; U.wc=String((const char*)(doc["wc"]|"")); U.city=String((const char*)(doc["city"]|"")); drawWeatherCard(); }
    } else connOK=0;
  } else connOK=0;
  http.end();
}

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
<div class=card><button style="background:#39c3cd;color:#06222a;font-weight:700" onclick="fetch('/state').then(r=>r.json()).then(s=>window.open(s.terminal||'/','_blank'))">Master Terminal &#8599;</button></div>
<div class=card><div class=grid><button onclick="if(confirm('Reboot device?'))set('reboot',1)">Reboot</button><button onclick="location.href='/update'">Firmware OTA</button></div></div>
<div class=foot><a href="https://latticelabs.au" target=_blank style="color:#3fd2dd;text-decoration:none">lattice labs</a></div>
<script>
function set(k,v){fetch('/set?'+k+'='+v).then(load)}
function load(){fetch('/state').then(r=>r.json()).then(s=>{ver.textContent='v'+s.ver;
sess.textContent=s.haveData?s.s+'%':'--';sessr.textContent=s.sr?('resets '+s.sr):'idle';
week.textContent=s.haveData?s.w+'%':'--';weekr.textContent=s.wr?('resets '+s.wr):'';
wx.textContent=s.city+' '+s.wt+'°C '+s.wc;clock.textContent=s.time;
bri.value=s.bri;bril.textContent=s.bri+'%';nEn.checked=s.ne;nStart.value=s.ns;nEnd.value=s.nf;nBri.value=s.nb;nbril.textContent=s.nb+'%';refresh.value=s.refresh;}).catch(()=>{})}
load();setInterval(load,2000);
</script></body></html>)HTML";

void handleRoot(){ server.send_P(200,"text/html",PANEL); }
void handleState(){
  time_t now=time(nullptr); char hms[12]="--:--:--"; if(now>=100000){struct tm* t=localtime(&now);strftime(hms,sizeof hms,"%H:%M:%S",t);}
  JsonDocument d;
  d["ver"]=FW_VER; d["haveData"]=haveData; d["conn"]=connOK; d["s"]=U.s; d["w"]=U.w; d["sr"]=U.sr; d["wr"]=U.wr;
  d["city"]=U.city; d["wt"]=U.wt; d["wc"]=U.wc; d["time"]=hms;
  d["bri"]=S.bri; d["ne"]=S.nEn; d["ns"]=S.nStart; d["nf"]=S.nEnd; d["nb"]=S.nBri; d["rot"]=S.rot; d["refresh"]=S.refresh;
  String tu=USAGE_URL; int i=tu.indexOf("/usage"); d["terminal"] = (i>0) ? tu.substring(0,i)+"/" : tu;
  String out; serializeJson(d,out); server.send(200,"application/json",out);
}
void handleSet(){
  bool reboot=false,redraw=false;
  if(server.hasArg("bri")){S.bri=constrain(server.arg("bri").toInt(),0,100);applyBacklight();}
  if(server.hasArg("nb")){S.nBri=constrain(server.arg("nb").toInt(),0,100);applyBacklight();}
  if(server.hasArg("ne")){S.nEn=server.arg("ne").toInt()?1:0;nightActive=false;applyBacklight();}
  if(server.hasArg("ns"))S.nStart=constrain(server.arg("ns").toInt(),0,23);
  if(server.hasArg("nf"))S.nEnd=constrain(server.arg("nf").toInt(),0,23);
  if(server.hasArg("refresh"))S.refresh=constrain(server.arg("refresh").toInt(),5,120);
  if(server.hasArg("rot")){S.rot=S.rot?0:2;redraw=true;}
  if(server.hasArg("reboot"))reboot=true;
  saveSettings();
  if(redraw){tft.setRotation(S.rot);fullRedraw();}
  server.send(200,"text/plain","ok");
  if(reboot){delay(200);ESP.restart();}
}

void setup(){
  Serial.begin(115200); Serial.println(F("\n[" FW_NAME " v" FW_VER "] boot"));
  EEPROM.begin(64); EEPROM.get(0,S); if(S.magic!=MAGIC) defaults();
  analogWriteRange(255); analogWriteFreq(22000); pinMode(TFT_BL,OUTPUT); applyBacklight();

  tft.init(); tft.setRotation(S.rot); tft.startWrite();
  C_BG=TFT_BLACK; C_PANEL=tft.color565(0x17,0x1f,0x2e); C_LINE=tft.color565(0x2c,0x37,0x4a);
  C_CORAL=tft.color565(0xff,0x7a,0x55); C_CYAN=tft.color565(0x3f,0xd2,0xdd); C_WHITE=TFT_WHITE;
  C_GRAY=tft.color565(0xa4,0xb0,0xc2); C_DIM=tft.color565(0x74,0x85,0x9b);
  C_GREEN=tft.color565(0x54,0xd3,0x6e); C_AMBER=tft.color565(0xf0,0xad,0x36); C_RED=tft.color565(0xff,0x4d,0x68); C_SKY=tft.color565(0x84,0xcd,0xf2);
  splash();

  WiFi.mode(WIFI_STA); WiFi.begin(WIFI_SSID,WIFI_PASS);
  unsigned long t0=millis(); while(WiFi.status()!=WL_CONNECTED && millis()-t0<25000){delay(250);yield();}
  if(WiFi.status()!=WL_CONNECTED){ WiFiManager wm; wm.setConfigPortalTimeout(180); if(!wm.autoConnect("ClaudeTV-Setup")){delay(1000);ESP.restart();} }
  Serial.print(F("IP: ")); Serial.println(WiFi.localIP());
  configTime(TZ_STR,"pool.ntp.org","time.nist.gov");
  if(MDNS.begin("claudetv")) MDNS.addService("http","tcp",80);

  httpUpdater.setup(&server,"/update");
  server.on("/",handleRoot); server.on("/state",handleState); server.on("/set",handleSet); server.begin();

  fullRedraw(); fetchUsage(); render(false); lastFetch=lastWx=millis();
}

void loop(){
  server.handleClient(); MDNS.update();
  if(millis()-lastClock>=1000){ lastClock=millis(); drawClock(); }
  if(millis()-lastWx>=WX_CYCLE_MS){ lastWx=millis(); wxIdx=(wxIdx+1)%6; drawWeatherCard(); }
  if(millis()-lastFetch>=(unsigned long)S.refresh*1000){ lastFetch=millis(); fetchUsage(); render(false); }
}
