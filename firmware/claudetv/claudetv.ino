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
#include "panel.h"

#define FW_NAME "ClaudeTV"
#define FW_VER  "4.3"
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

struct Settings { uint8_t magic, bri, nEn, nStart, nEnd, nBri, rot; uint16_t refresh; char usageUrl[100]; } S;
const uint8_t MAGIC = 0xC5;

// geometry (1:1 with emulator)
const int UCX=8, UCY=33, UCW=224, UCH=79, LCX=64, RCX=176;
const int WCX=8, WCY=118, WCW=224, WCH=54;
const int TIME_Y=196, DATE_Y=222;

void saveSettings(){ EEPROM.put(0,S); EEPROM.commit(); }
void defaults(){ memset(&S,0,sizeof S); S.magic=MAGIC; S.bri=60; S.nEn=1; S.nStart=21; S.nEnd=7; S.nBri=30; S.rot=0; S.refresh=20; strncpy(S.usageUrl,USAGE_URL,sizeof(S.usageUrl)-1); }
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
  if(!http.begin(client,S.usageUrl)){connOK=0;return;}
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

// PANEL HTML lives in panel.h (keeps JS out of the .ino prototype generator)

void handleRoot(){ server.send_P(200,"text/html",PANEL); }
void handleState(){
  time_t now=time(nullptr); char hms[12]="--:--:--"; if(now>=100000){struct tm* t=localtime(&now);strftime(hms,sizeof hms,"%H:%M:%S",t);}
  JsonDocument d;
  d["ver"]=FW_VER; d["haveData"]=haveData; d["conn"]=connOK; d["s"]=U.s; d["w"]=U.w; d["sr"]=U.sr; d["wr"]=U.wr;
  d["city"]=U.city; d["wt"]=U.wt; d["wc"]=U.wc; d["time"]=hms;
  d["bri"]=S.bri; d["ne"]=S.nEn; d["ns"]=S.nStart; d["nf"]=S.nEnd; d["nb"]=S.nBri; d["rot"]=S.rot; d["refresh"]=S.refresh;
  d["usage"]=S.usageUrl;
  String tu=S.usageUrl; int i=tu.indexOf("/usage"); d["terminal"] = (i>0) ? tu.substring(0,i)+"/" : tu;
  String out; serializeJson(d,out); server.send(200,"application/json",out);
}
void handleSet(){
  bool reboot=false,redraw=false,usageChanged=false;
  if(server.hasArg("bri")){S.bri=constrain(server.arg("bri").toInt(),0,100);applyBacklight();}
  if(server.hasArg("nb")){S.nBri=constrain(server.arg("nb").toInt(),0,100);applyBacklight();}
  if(server.hasArg("ne")){S.nEn=server.arg("ne").toInt()?1:0;nightActive=false;applyBacklight();}
  if(server.hasArg("ns"))S.nStart=constrain(server.arg("ns").toInt(),0,23);
  if(server.hasArg("nf"))S.nEnd=constrain(server.arg("nf").toInt(),0,23);
  if(server.hasArg("refresh"))S.refresh=constrain(server.arg("refresh").toInt(),5,120);
  if(server.hasArg("usage")){ String u=server.arg("usage"); if(u.length()>6){ strncpy(S.usageUrl,u.c_str(),sizeof(S.usageUrl)-1); S.usageUrl[sizeof(S.usageUrl)-1]=0; usageChanged=true; } }
  if(server.hasArg("rot")){S.rot=S.rot?0:2;redraw=true;}
  if(server.hasArg("reboot"))reboot=true;
  saveSettings();
  if(redraw){tft.setRotation(S.rot);fullRedraw();}
  if(usageChanged){ fetchUsage(); render(false); }   // immediately re-test the new collector
  server.send(200,"text/plain","ok");
  if(reboot){delay(200);ESP.restart();}
}

void setup(){
  Serial.begin(115200); Serial.println(F("\n[" FW_NAME " v" FW_VER "] boot"));
  EEPROM.begin(256); EEPROM.get(0,S); if(S.magic!=MAGIC) defaults();
  if(S.usageUrl[0]==0){ strncpy(S.usageUrl,USAGE_URL,sizeof(S.usageUrl)-1); }
  analogWriteRange(255); analogWriteFreq(22000); pinMode(TFT_BL,OUTPUT); applyBacklight();

  tft.init(); tft.setRotation(S.rot); tft.startWrite();
  C_BG=TFT_BLACK; C_PANEL=tft.color565(0x17,0x1f,0x2e); C_LINE=tft.color565(0x2c,0x37,0x4a);
  C_CORAL=tft.color565(0xff,0x7a,0x55); C_CYAN=tft.color565(0x3f,0xd2,0xdd); C_WHITE=TFT_WHITE;
  C_GRAY=tft.color565(0xa4,0xb0,0xc2); C_DIM=tft.color565(0x74,0x85,0x9b);
  C_GREEN=tft.color565(0x54,0xd3,0x6e); C_AMBER=tft.color565(0xf0,0xad,0x36); C_RED=tft.color565(0xff,0x4d,0x68); C_SKY=tft.color565(0x84,0xcd,0xf2);
  splash();

  WiFi.mode(WIFI_STA);
  if(strlen(WIFI_SSID)>0){                      // baked creds (personal build) -> direct connect
    WiFi.begin(WIFI_SSID,WIFI_PASS);
    unsigned long t0=millis(); while(WiFi.status()!=WL_CONNECTED && millis()-t0<25000){delay(250);yield();}
  }
  if(WiFi.status()!=WL_CONNECTED){               // generic build / bad creds -> captive portal
    WiFiManager wm; wm.setConfigPortalTimeout(180);
    WiFiManagerParameter pUrl("usage","Collector URL (http://host:8088/usage)",S.usageUrl,sizeof(S.usageUrl)-1);
    wm.addParameter(&pUrl);
    if(!wm.autoConnect("ClaudeTV-Setup")){delay(1000);ESP.restart();}
    if(strlen(pUrl.getValue())>6){ strncpy(S.usageUrl,pUrl.getValue(),sizeof(S.usageUrl)-1); S.usageUrl[sizeof(S.usageUrl)-1]=0; saveSettings(); }
  }
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
