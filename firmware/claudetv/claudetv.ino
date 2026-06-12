/*
 * ClaudeTV — GeekMagic SmallTV-Ultra (ESP8266 / ESP-12F, ST7789V 240x240)
 * Shows Claude session(5h)/week(7d) usage % + reset times, local weather, and a clock.
 * Full web control panel at "/" (brightness, night mode, rotation, refresh, reboot, OTA).
 * Silent rendering: TFT_eSPI with ONE held-open SPI transaction (CS low continuously).
 * Settings persist in EEPROM. WiFi creds + collector URL in config.h (gitignored).
 */
#include <ESP8266WiFi.h>
#include <ESP8266WebServer.h>
#include <ESP8266HTTPUpdateServer.h>
#include <ESP8266HTTPClient.h>
#include <WiFiClient.h>
#include <WiFiManager.h>
#include <TFT_eSPI.h>
#include <ArduinoJson.h>
#include <EEPROM.h>
#include "config.h"

#define FW_NAME "ClaudeTV"
#define FW_VER  "3.0"
#define TZ_STR  "AEST-10AEDT,M10.1.0,M4.1.0/3"
#define TFT_BL  5

TFT_eSPI tft = TFT_eSPI();
ESP8266WebServer        server(80);
ESP8266HTTPUpdateServer httpUpdater;

uint16_t C_BG, C_CORAL, C_WHITE, C_GRAY, C_TRACK, C_GREEN, C_AMBER, C_RED, C_SKY;
unsigned long lastFetch = 0, lastClock = 0;
int connOK = -1; bool haveData = false, nightActive = false;
struct { int s=0, w=0, wt=-999; String sr, wr, wc, city; } U;
int pS=-99, pW=-99, pWt=-999, pConn=-1; String pSR="\x01", pWR="\x01", pWC="\x01", pDate="\x01";

struct Settings { uint8_t magic, bri, nEn, nStart, nEnd, nBri, rot; uint16_t refresh; } S;
const uint8_t MAGIC = 0xC2;

const int SESS_CY=46, WEEK_CY=72, DIV1=88, WX_CY=110, DIV2=140, TIME_CY=168, DATE_CY=210;

void saveSettings() { EEPROM.put(0, S); EEPROM.commit(); }
void defaults() { S = {MAGIC, 60, 0, 21, 7, 8, 0, 20}; }

uint16_t lvl(int p){ if(p<0)return C_GRAY; if(p>=85)return C_RED; if(p>=50)return C_AMBER; return C_GREEN; }
void applyBacklight(){ int b = nightActive ? S.nBri : S.bri; b = constrain(b,0,100); analogWrite(TFT_BL,(100-b)*255/100); }
bool isNight(int hr){ if(!S.nEn||S.nStart==S.nEnd) return false; return S.nStart<S.nEnd ? (hr>=S.nStart&&hr<S.nEnd) : (hr>=S.nStart||hr<S.nEnd); }
void str(const char* s,int x,int y,const GFXfont* f,uint16_t col,uint8_t d){ tft.setFreeFont(f); tft.setTextDatum(d); tft.setTextColor(col,C_BG); tft.drawString(s,x,y); }

void drawChrome(){
  tft.fillScreen(C_BG); tft.fillRect(0,0,240,6,C_CORAL);
  str("CLAUDE USAGE",120,18,&FreeSansBold12pt7b,C_WHITE,MC_DATUM);
  tft.drawFastHLine(12,DIV1,216,C_TRACK); tft.drawFastHLine(12,DIV2,216,C_TRACK);
}
void drawStat(int cy,const char* label,int pct,const String& reset){
  tft.fillRect(10,cy-12,224,24,C_BG);
  str(label,14,cy,&FreeSans9pt7b,C_GRAY,ML_DATUM);
  char b[8]; if(pct<0)strcpy(b,"--"); else snprintf(b,sizeof b,"%d%%",pct);
  str(b,92,cy,&FreeSansBold12pt7b,lvl(pct),ML_DATUM);
  str((reset.length()?reset:String("idle")).c_str(),228,cy,&FreeSans9pt7b,C_GRAY,MR_DATUM);
}
void drawWeather(){
  tft.fillRect(10,DIV1+2,224,DIV2-DIV1-4,C_BG);
  str(U.city.length()?U.city.c_str():"weather",14,WX_CY-8,&FreeSans9pt7b,C_GRAY,ML_DATUM);
  str(U.wc.length()?U.wc.c_str():"--",14,WX_CY+12,&FreeSans9pt7b,C_SKY,ML_DATUM);
  char t[10]; if(U.wt>-999)snprintf(t,sizeof t,"%dC",U.wt); else strcpy(t,"--");
  str(t,228,WX_CY,&FreeSansBold18pt7b,C_WHITE,MR_DATUM);
}
void drawDot(){ tft.fillCircle(16,DATE_CY,3,connOK==1?C_GREEN:C_RED); }
void drawClock(){
  time_t now=time(nullptr); char hms[12],dat[18];
  if(now<100000){strcpy(hms,"--:--:--");strcpy(dat,"syncing");}
  else{struct tm* t=localtime(&now); strftime(hms,sizeof hms,"%H:%M:%S",t); strftime(dat,sizeof dat,"%a %d %b",t);
       bool n=isNight(t->tm_hour); if(n!=nightActive){nightActive=n;applyBacklight();}}
  str(hms,120,TIME_CY,&FreeSansBold18pt7b,C_WHITE,MC_DATUM);
  if(String(dat)!=pDate){tft.fillRect(28,DATE_CY-9,196,18,C_BG); str(dat,124,DATE_CY,&FreeSans9pt7b,C_GRAY,MC_DATUM); pDate=dat;}
}
void render(bool force){
  if(force||U.s!=pS||U.sr!=pSR){drawStat(SESS_CY,"SESSION",haveData?U.s:-1,U.sr);pS=U.s;pSR=U.sr;}
  if(force||U.w!=pW||U.wr!=pWR){drawStat(WEEK_CY,"WEEK",haveData?U.w:-1,U.wr);pW=U.w;pWR=U.wr;}
  if(force||U.wt!=pWt||U.wc!=pWC){drawWeather();pWt=U.wt;pWC=U.wc;}
  if(force||connOK!=pConn){drawDot();pConn=connOK;}
}
void fullRedraw(){ drawChrome(); pS=pW=-99; pWt=-999; pConn=-1; pSR=pWR=pWC=pDate="\x01"; render(true); drawClock(); }

void fetchUsage(){
  WiFiClient client; HTTPClient http; http.setTimeout(6000);
  if(!http.begin(client,USAGE_URL)){connOK=0;return;}
  int code=http.GET();
  if(code==200){
    JsonDocument doc;
    if(deserializeJson(doc,http.getString())==DeserializationError::Ok){
      connOK=1;
      if((doc["ok"]|0)==1){haveData=true; U.s=doc["s"]|0; U.w=doc["w"]|0; U.sr=String((const char*)(doc["sr"]|"")); U.wr=String((const char*)(doc["wr"]|""));}
      if(doc["wt"].is<int>()){U.wt=doc["wt"]|-999; U.wc=String((const char*)(doc["wc"]|"")); U.city=String((const char*)(doc["city"]|""));}
    } else connOK=0;
  } else connOK=0;
  http.end();
}

const char PANEL[] PROGMEM = R"HTML(<!DOCTYPE html><html><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'><title>ClaudeTV</title><style>
body{font-family:system-ui,sans-serif;background:#0b0e14;color:#e6e9ef;margin:0;padding:16px;max-width:480px;margin:auto}
h1{font-size:22px;color:#fff;border-bottom:3px solid #D97757;padding-bottom:8px}.coral{color:#D97757}
.card{background:#151a23;border-radius:12px;padding:14px;margin:12px 0}
.row{display:flex;justify-content:space-between;align-items:center;margin:6px 0}.big{font-size:26px;font-weight:700}
.muted{color:#8a97a8;font-size:13px}label{font-size:13px;color:#b0c4de}input[type=range]{width:100%}
button,select,input[type=number]{background:#232a36;color:#e6e9ef;border:1px solid #2c3442;border-radius:8px;padding:8px;font-size:14px}
button{cursor:pointer;width:100%}.grid{display:grid;grid-template-columns:1fr 1fr;gap:8px}
</style></head><body>
<h1>Claude<span class=coral>TV</span> <span class=muted id=ver></span></h1>
<div class=card>
<div class=row><span>Session (5h)</span><span class=big id=sess>--</span></div><div class=muted id=sessr></div>
<div class=row><span>Week (7d)</span><span class=big id=week>--</span></div><div class=muted id=weekr></div>
<div class=row><span class=muted id=wx></span><span class=muted id=clock></span></div></div>
<div class=card><label>Brightness <span id=bril></span></label><input type=range min=0 max=100 id=bri oninput="set('bri',this.value)"></div>
<div class=card><div class=row><label>Night mode</label><input type=checkbox id=nEn onchange="set('ne',this.checked?1:0)"></div>
<div class=grid><div><label>Start hr</label><input type=number min=0 max=23 id=nStart onchange="set('ns',this.value)"></div>
<div><label>End hr</label><input type=number min=0 max=23 id=nEnd onchange="set('nf',this.value)"></div></div>
<label>Night brightness <span id=nbril></span></label><input type=range min=0 max=100 id=nBri oninput="set('nb',this.value)"></div>
<div class=card><div class=row><label>Flip display 180&deg;</label><button style="width:auto" onclick="set('rot',-1)">Rotate</button></div>
<div class=row><label>Refresh (s)</label><input type=number min=5 max=120 id=refresh onchange="set('refresh',this.value)"></div></div>
<div class=card><div class=grid><button onclick="if(confirm('Reboot device?'))set('reboot',1)">Reboot</button>
<button onclick="location.href='/update'">Firmware OTA</button></div></div>
<script>
function set(k,v){fetch('/set?'+k+'='+v).then(load)}
function load(){fetch('/state').then(r=>r.json()).then(s=>{
ver.textContent='v'+s.ver;sess.textContent=s.haveData?s.s+'%':'--';sessr.textContent=s.sr?('resets '+s.sr):'idle';
week.textContent=s.haveData?s.w+'%':'--';weekr.textContent=s.wr?('resets '+s.wr):'';
wx.textContent=s.city+' '+s.wt+'°C '+s.wc;clock.textContent=s.time;
bri.value=s.bri;bril.textContent=s.bri+'%';nEn.checked=s.ne;nStart.value=s.ns;nEnd.value=s.nf;
nBri.value=s.nb;nbril.textContent=s.nb+'%';refresh.value=s.refresh;}).catch(()=>{})}
load();setInterval(load,2000);
</script></body></html>)HTML";

void handleRoot(){ server.send_P(200,"text/html",PANEL); }
void handleState(){
  time_t now=time(nullptr); char hms[12]="--:--:--"; if(now>=100000){struct tm* t=localtime(&now);strftime(hms,sizeof hms,"%H:%M:%S",t);}
  JsonDocument d;
  d["ver"]=FW_VER; d["haveData"]=haveData; d["conn"]=connOK; d["s"]=U.s; d["w"]=U.w; d["sr"]=U.sr; d["wr"]=U.wr;
  d["city"]=U.city; d["wt"]=U.wt; d["wc"]=U.wc; d["time"]=hms;
  d["bri"]=S.bri; d["ne"]=S.nEn; d["ns"]=S.nStart; d["nf"]=S.nEnd; d["nb"]=S.nBri; d["rot"]=S.rot; d["refresh"]=S.refresh;
  String out; serializeJson(d,out); server.send(200,"application/json",out);
}
void handleSet(){
  bool reboot=false, redraw=false;
  if(server.hasArg("bri")){ S.bri=constrain(server.arg("bri").toInt(),0,100); applyBacklight(); }
  if(server.hasArg("nb")){ S.nBri=constrain(server.arg("nb").toInt(),0,100); applyBacklight(); }
  if(server.hasArg("ne")){ S.nEn=server.arg("ne").toInt()?1:0; nightActive=false; applyBacklight(); }
  if(server.hasArg("ns")){ S.nStart=constrain(server.arg("ns").toInt(),0,23); }
  if(server.hasArg("nf")){ S.nEnd=constrain(server.arg("nf").toInt(),0,23); }
  if(server.hasArg("refresh")){ S.refresh=constrain(server.arg("refresh").toInt(),5,120); }
  if(server.hasArg("rot")){ S.rot = S.rot ? 0 : 2; redraw=true; }
  if(server.hasArg("reboot")) reboot=true;
  saveSettings();
  if(redraw){ tft.setRotation(S.rot); fullRedraw(); }
  server.send(200,"text/plain","ok");
  if(reboot){ delay(200); ESP.restart(); }
}

void setup(){
  Serial.begin(115200); Serial.println(F("\n[" FW_NAME " v" FW_VER "] boot"));
  EEPROM.begin(64); EEPROM.get(0,S); if(S.magic!=MAGIC) defaults();

  analogWriteRange(255); analogWriteFreq(22000);
  pinMode(TFT_BL,OUTPUT); applyBacklight();

  tft.init(); tft.setRotation(S.rot); tft.startWrite();
  C_BG=TFT_BLACK; C_CORAL=tft.color565(0xD9,0x77,0x57); C_WHITE=TFT_WHITE;
  C_GRAY=tft.color565(0x8a,0x97,0xa8); C_TRACK=tft.color565(0x26,0x2c,0x38);
  C_GREEN=tft.color565(0x4c,0xc0,0x66); C_AMBER=tft.color565(0xe0,0xa0,0x2a); C_RED=tft.color565(0xe0,0x3b,0x55); C_SKY=tft.color565(0x6c,0xb8,0xe8);
  drawChrome(); str("connecting WiFi",120,120,&FreeSans9pt7b,C_GRAY,MC_DATUM);

  WiFi.mode(WIFI_STA); WiFi.begin(WIFI_SSID,WIFI_PASS);
  unsigned long t0=millis(); while(WiFi.status()!=WL_CONNECTED && millis()-t0<25000){delay(250);yield();}
  if(WiFi.status()!=WL_CONNECTED){ WiFiManager wm; wm.setConfigPortalTimeout(180); if(!wm.autoConnect("ClaudeTV-Setup")){delay(1000);ESP.restart();} }
  Serial.print(F("IP: ")); Serial.println(WiFi.localIP());
  configTime(TZ_STR,"pool.ntp.org","time.nist.gov");

  httpUpdater.setup(&server,"/update");
  server.on("/",handleRoot); server.on("/state",handleState); server.on("/set",handleSet);
  server.begin();

  fullRedraw(); fetchUsage(); render(false); lastFetch=millis();
}

void loop(){
  server.handleClient();
  if(millis()-lastClock>=1000){ lastClock=millis(); drawClock(); }
  if(millis()-lastFetch>= (unsigned long)S.refresh*1000){ lastFetch=millis(); fetchUsage(); render(false); }
}
