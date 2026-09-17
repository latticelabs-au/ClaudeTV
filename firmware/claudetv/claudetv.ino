/*
 * ClaudeTV — GeekMagic SmallTV-Ultra (ESP8266 / ESP-12F, ST7789V 240x240)
 * Dashboard: Claude session(5h) | week(7d) + the model-scoped weekly limit (e.g. Fable) as
 * usage % + reset times — week and the scoped limit share one 7d reset line; falls back to the
 * classic two-column card when the account has no scoped limit.
 * Cycling weather turntable, big clock, Lattice Labs logo. Web control panel + OTA.
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
#define FW_VER  "5.2"
// However many accounts cswap manages, the cycle shows up to this many. Raise freely: the cost
// is 4 Strings + 4 ints of heap each, and the header switches from page dots to an "i/N"
// counter past DOTS_MAX so the indicator never grows into the label.
#define MAXACC   8
#define DOTS_MAX 6
#define TZ_STR  "AEST-10AEDT,M10.1.0,M4.1.0/3"
#define TFT_BL  5
#define WX_CYCLE_MS 4000

TFT_eSPI tft = TFT_eSPI();
ESP8266WebServer        server(80);
ESP8266HTTPUpdateServer httpUpdater;

uint16_t C_BG, C_PANEL, C_LINE, C_CORAL, C_CYAN, C_WHITE, C_GRAY, C_DIM, C_GREEN, C_AMBER, C_RED, C_SKY;
// Provider tint. Claude accounts keep the coral header; Codex accounts get violet, the one hue
// nothing else on this screen uses (numbers are green/amber/red, the update mark is cyan, the
// weather is sky), so a glance at the header tells you whose numbers these are.
uint16_t C_CODEX, C_CODEX_DIM;
unsigned long lastFetch=0, lastClock=0, lastWx=0, lastAcc=0;
int connOK=-1, wxIdx=0; bool haveData=false, nightActive=false, updAvail=false, pUpd=false;
int dataAge=-1, pAuth=-1;
// One account's usage. The card renders ONE account at a time and the display cycles through
// them (the same turntable the weather metrics use), so every account keeps the full three
// Bold18 heroes instead of six numbers fighting over one 79px card.
struct Acct { int s=0, w=0, f=-1, auth=0; bool codex=false; String sr, wr, fl, label; };   // auth: 0 ok, 1 pending, 2 dead; codex: acc[].p=="x"
Acct AC[MAXACC]; int nAcc=1, accIdx=0;
struct { int wt=-999, wfl=-999, whi=-999, wlo=-999, wrain=-999, whum=-999; String wc, city; } U;
int pS=-99, pW=-99, pF=-99, pConn=-1, pAcc=-1, pNAcc=-1; bool pCodex=false; String pSR="\x01", pWR="\x01", pFL="\x01", pLBL="\x01", pDate="\x01";
Acct& A(){ return AC[(accIdx < nAcc && accIdx < MAXACC) ? accIdx : 0]; }   // the account on screen

// accCycle was appended AFTER usageUrl, so a 4.7 EEPROM image reads a stale byte there. MAGIC is
// deliberately NOT bumped (that would wipe the saved collector URL on generic builds); instead the
// value is range-checked on load, which re-defaults both a legacy byte and a corrupt one.
struct Settings { uint8_t magic, bri, nEn, nStart, nEnd, nBri, rot; uint16_t refresh; char usageUrl[100]; uint8_t accCycle; } S;
const uint8_t MAGIC = 0xC5;
const uint8_t ACC_CYCLE_MIN = 3, ACC_CYCLE_MAX = 60, ACC_CYCLE_DEF = 5;

// geometry (1:1 with emulator)
const int UCX=8, UCY=33, UCW=224, UCH=79, LCX=64, RCX=176;
// 3-metric geometry (account has a model-scoped weekly limit, e.g. Fable). Single-letter
// labels (S/W/F) free the label row so ALL THREE numbers run at Bold18. Sized from the fonts'
// REAL xAdvance tables: "88%"@18=69px per ~74px column; 100 renders WITHOUT '%' ("100%"@18 is
// 88px and would not fit). Worst cases: sr "12:39pm"=71px -> 9.5..80.5; week 84.5..153.5;
// fable 160.5..229.5; shared reset "Dec 30 12:59pm"=134px -> 90..224 (NO "resets" prefix —
// with it the line is 174px and clips the card). All inside the card (8..232).
const int FDIV=82, FSCX=45, FWCX=119, FFCX=195, FRSTX=157;
const int WCX=8, WCY=118, WCW=224, WCH=54;
const int TIME_Y=196, DATE_Y=222;

void saveSettings(){ EEPROM.put(0,S); EEPROM.commit(); }
void defaults(){ memset(&S,0,sizeof S); S.magic=MAGIC; S.bri=60; S.nEn=1; S.nStart=21; S.nEnd=7; S.nBri=30; S.rot=0; S.refresh=20; S.accCycle=ACC_CYCLE_DEF; strncpy(S.usageUrl,USAGE_URL,sizeof(S.usageUrl)-1); }
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

void drawMetric(int cx,const char* label,int pct,const GFXfont* f){
  str(label,cx,UCY+15,&FreeSans9pt7b,C_GRAY,MC_DATUM,C_PANEL);
  char b[8];
  // pct<0 means the collector had no reading for this window. It must render as "--": the
  // collector deliberately sends -1 rather than inventing a 0, and "-1%" would be a lie.
  if(!haveData||pct<0) strcpy(b,"--");
  else if(pct==100) strcpy(b,"100");             // no '%': 88px at 18pt won't fit the column
  else snprintf(b,sizeof b,"%d%%",pct);
  str(b,cx,UCY+43,f,lvl(haveData?pct:-1),MC_DATUM,C_PANEL);
}
void drawUpdateMark(){
  // small cyan up-arrow in the card's top-right corner. That corner is deliberately empty in
  // both layouts (the F column is centred at 195, its number sits at y=76), so this marker
  // never collides with a metric no matter how wide the numbers get.
  if(!updAvail) return;
  int x=UCX+UCW-16, y=UCY+8;
  tft.fillTriangle(x,y, x-5,y+7, x+5,y+7, C_CYAN);
  tft.fillRect(x-2,y+7,5,4,C_CYAN);
}
void drawUsageCard(){
  Acct& a=A();
  tft.fillRoundRect(UCX,UCY,UCW,UCH,10,C_PANEL);
  if(a.codex&&a.s<0&&a.f<0){      // Codex plan with a weekly limit only (no 5h window, no model
    // limit): ONE hero, not a classic card with a dead "SESSION --" column beside the number that
    // matters. Same three rows as the other layouts, so cycling accounts does not jump around.
    // Worst case "resets Dec 30 12:59pm"@Bold9 = 192px centred -> 24..216, clear of the card edge,
    // and a lone "100%"@18 = 88px fits, so this layout keeps its percent sign.
    str("WEEK",120,UCY+15,&FreeSans9pt7b,C_GRAY,MC_DATUM,C_PANEL);
    char b[8]; if(haveData&&a.w>=0)snprintf(b,sizeof b,"%d%%",a.w); else strcpy(b,"--");
    str(b,120,UCY+43,&FreeSansBold18pt7b,lvl(haveData?a.w:-1),MC_DATUM,C_PANEL);
    char r[32]; if(a.wr.length())snprintf(r,sizeof r,"resets %s",a.wr.c_str()); else strcpy(r,"--");
    str(r,120,UCY+67,&FreeSansBold9pt7b,C_DIM,MC_DATUM,C_PANEL);
    drawUpdateMark();
    return;
  }
  if(a.f>=0){                     // S (5h) | W + F (7d) — one shared reset, three Bold18 heroes
    tft.drawFastVLine(FDIV,UCY+14,UCH-28,C_LINE);   // divider = the 5h | 7d window boundary
    drawMetric(FSCX,"S",a.s,&FreeSansBold18pt7b);
    str(a.sr.length()?a.sr.c_str():"idle",FSCX,UCY+67,&FreeSansBold9pt7b,C_DIM,MC_DATUM,C_PANEL);
    drawMetric(FWCX,"W",a.w,&FreeSansBold18pt7b);
    char fLbl[2]={a.fl.length()?a.fl[0]:'F',0};     // first letter of the scoped-model name
    drawMetric(FFCX,fLbl,a.f,&FreeSansBold18pt7b);
    // week + fable share the same 7d window (their resets land ~1s apart) -> ONE reset line
    if(a.wr.length()) str(a.wr.c_str(),FRSTX,UCY+67,&FreeSansBold9pt7b,C_DIM,MC_DATUM,C_PANEL);
    drawUpdateMark();
    return;
  }
  // classic 2-col — account has no model-scoped weekly limit
  tft.drawFastVLine(120,UCY+14,UCH-28,C_LINE);
  // session (left col): reset centered under the column
  str("SESSION",LCX,UCY+15,&FreeSans9pt7b,C_GRAY,MC_DATUM,C_PANEL);
  char b[8]; if(haveData&&a.s>=0)snprintf(b,sizeof b,"%d%%",a.s); else strcpy(b,"--");
  str(b,LCX,UCY+43,&FreeSansBold18pt7b,lvl(haveData?a.s:-1),MC_DATUM,C_PANEL);
  str(a.sr.length()?a.sr.c_str():"idle",UCX+12,UCY+67,&FreeSansBold9pt7b,C_DIM,ML_DATUM,C_PANEL);
  // week (right col): reset right-aligned so the wide date+time never crosses the card edge
  str("WEEK",RCX,UCY+15,&FreeSans9pt7b,C_GRAY,MC_DATUM,C_PANEL);
  if(haveData&&a.w>=0)snprintf(b,sizeof b,"%d%%",a.w); else strcpy(b,"--");
  str(b,RCX,UCY+43,&FreeSansBold18pt7b,lvl(haveData?a.w:-1),MC_DATUM,C_PANEL);
  // The two resets share one row: session from the left edge, week from the right. At their widest
  // ("12:39pm" 71px + "Dec 30 12:59pm" 134px) they meet in the middle of the 200px row. Claude resets
  // land on round hours so it was rare; Codex resets carry odd minutes, so it is not. When they would
  // touch, the WEEK reset drops its minutes ("Dec 30 12pm"): days away, the hour is what matters.
  String wr=a.wr;
  tft.setFreeFont(&FreeSansBold9pt7b);
  if(a.sr.length()&&wr.length()&&tft.textWidth(a.sr)+tft.textWidth(wr)>UCW-24-8){
    int c=wr.lastIndexOf(':'); if(c>0) wr.remove(c,3);
  }
  str(wr.length()?wr.c_str():"--",UCX+UCW-12,UCY+67,&FreeSansBold9pt7b,C_DIM,MR_DATUM,C_PANEL);
  drawUpdateMark();
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
void fmtAge(int s,char* out,size_t n){
  if(s<0){ out[0]=0; return; }
  if(s<3600) snprintf(out,n,"stale %dm",s/60); else snprintf(out,n,"stale %dh",s/3600);
}
void drawUsageError(){          // dead Claude auth -> takeover the hero card (weather/clock keep running)
  tft.fillRoundRect(UCX,UCY,UCW,UCH,10,C_PANEL);
  str("LOGIN EXPIRED",120,UCY+24,&FreeSansBold12pt7b,C_RED,MC_DATUM,C_PANEL);
  str("re-auth on host",120,UCY+46,&FreeSans9pt7b,C_GRAY,MC_DATUM,C_PANEL);
  char ag[16]; fmtAge(dataAge,ag,sizeof ag);
  if(ag[0]) str(ag,120,UCY+66,&FreeSans9pt7b,C_DIM,MC_DATUM,C_PANEL);
  drawUpdateMark();
}
void drawDot(){ int au=A().auth; uint16_t c=(connOK!=1||au==2)?C_RED:(au==1?C_AMBER:C_GREEN); tft.fillCircle(18,DATE_Y,3,c); }
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
// Header: a single account keeps the original "CLAUDE USAGE" title; two or more replace it with
// the account label (the title is decoration once the card is per-account) plus page dots, so you
// always know which account the numbers belong to. "PERSONAL"@Bold12 = 132px from x=26 -> 158,
// clear of the dots, which are right-aligned to end at x=232.
void header(){
  // Clear the ENTIRE band above the usage card (rows 0..UCY-1). The label changes width AND
  // Bold12 glyphs at TL_DATUM y=9 reach ~y=32, so a tight clear leaves the previous label's
  // bottom rows stranded in the 26..32 gap that neither the header nor the card repaints.
  tft.fillRect(0,0,240,UCY,C_BG);
  Acct& a=A(); uint16_t tint=a.codex?C_CODEX:C_CORAL;
  tft.fillRoundRect(12,12,9,9,2,tint);
  if(nAcc<2){ str(a.codex?"CODEX USAGE":"CLAUDE USAGE",26,9,&FreeSansBold12pt7b,C_WHITE,TL_DATUM,C_BG); return; }
  String lb=a.label; if(!lb.length()) lb="ACCOUNT";
  str(lb.c_str(),26,9,&FreeSansBold12pt7b,tint,TL_DATUM,C_BG);
  if(nAcc<=DOTS_MAX){                              // page dots, right-aligned to end at x=232
    // Codex pages are dim violet, so a mixed fleet reads at a glance (which dots are Codex) and a
    // Claude-only display looks exactly as it always has.
    for(int i=0;i<nAcc;i++)
      tft.fillCircle(232-(nAcc-1-i)*10,17,3,(i==accIdx)?C_WHITE:(AC[i].codex?C_CODEX_DIM:C_LINE));
  }else{                                           // too many to dot -> compact "3/8" counter
    char c[8]; snprintf(c,sizeof c,"%d/%d",accIdx+1,nAcc);
    str(c,235,17,&FreeSansBold9pt7b,C_GRAY,MR_DATUM,C_BG);
  }
}
void render(bool force){
  Acct& a=A();
  bool cardChg=force||a.codex!=pCodex||a.auth!=pAuth||accIdx!=pAcc||updAvail!=pUpd||a.s!=pS||a.w!=pW||a.f!=pF||a.sr!=pSR||a.wr!=pWR||a.fl!=pFL;
  // nAcc matters on its own: dropping to one account must repaint the title and clear the dots
  bool hdrChg =force||accIdx!=pAcc||nAcc!=pNAcc||a.label!=pLBL||a.codex!=pCodex;
  bool dotChg =force||connOK!=pConn||a.auth!=pAuth;
  if(hdrChg){ header(); pLBL=a.label; pNAcc=nAcc; pCodex=a.codex; }
  if(cardChg){ if(a.auth==2)drawUsageError(); else drawUsageCard(); pS=a.s;pW=a.w;pF=a.f;pSR=a.sr;pWR=a.wr;pFL=a.fl;pUpd=updAvail; }
  if(dotChg){ drawDot(); pConn=connOK; }
  pAuth=a.auth; pAcc=accIdx;
}
void fullRedraw(){
  tft.fillScreen(C_BG); drawLogo();
  pS=pW=pF=-99; pConn=-1; pAuth=-1; pAcc=-1; pNAcc=-1; pSR=pWR=pFL=pLBL=pDate="\x01";
  render(true); drawWeatherCard(); drawClock();   // render(true) paints the header
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
      const char* a=doc["auth"]|"ok";
      int topAuth=(!strcmp(a,"dead"))?2:(!strcmp(a,"pending"))?1:0;
      dataAge=doc["age"]|-1;
      updAvail=(doc["up"]|0)==1;   // collector found a newer firmware release
      if((doc["ok"]|0)==1){
        haveData=true;
        // Multi-account collector: acc[] carries every account. An older single-account
        // collector has no acc[], so fall back to the flat keys as one account — that keeps
        // this firmware working against both.
        JsonArray ar=doc["acc"].as<JsonArray>();
        if(!ar.isNull()&&ar.size()>0){
          int n=0;
          for(JsonObject o:ar){
            if(n>=MAXACC) break;
            Acct& t=AC[n];
            t.s=o["s"]|0; t.w=o["w"]|0; t.f=o["f"]|-1;
            t.sr=String((const char*)(o["sr"]|"")); t.wr=String((const char*)(o["wr"]|""));
            t.fl=String((const char*)(o["fl"]|"")); t.label=String((const char*)(o["l"]|""));
            const char* oa=o["auth"]|"ok";
            t.auth=(!strcmp(oa,"dead"))?2:(!strcmp(oa,"pending"))?1:0;
            const char* op=o["p"]|"c";                 // provider: "c" Claude (or absent), "x" Codex
            t.codex=(op[0]=='x');
            n++;
          }
          nAcc=n;
        } else {
          Acct& t=AC[0];
          t.s=doc["s"]|0; t.w=doc["w"]|0; t.sr=String((const char*)(doc["sr"]|"")); t.wr=String((const char*)(doc["wr"]|""));
          t.f=doc["f"]|-1; t.fl=String((const char*)(doc["fl"]|""));   // model-scoped weekly (e.g. Fable); -1 = none
          t.label=""; t.auth=topAuth; t.codex=false; nAcc=1;
        }
        if(accIdx>=nAcc) accIdx=0;
      }
      else if(topAuth==2){haveData=false; nAcc=1; accIdx=0; AC[0].auth=2;}   // dead -> no phantom stale %
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
  Acct& a=A();
  d["ver"]=FW_VER; d["haveData"]=haveData; d["conn"]=connOK; d["auth"]=a.auth; d["age"]=dataAge;
  d["s"]=a.s; d["w"]=a.w; d["f"]=a.f; d["fl"]=a.fl; d["sr"]=a.sr; d["wr"]=a.wr;
  d["nacc"]=nAcc; d["acci"]=accIdx; d["label"]=a.label; d["up"]=updAvail;
  JsonArray ar=d["acc"].to<JsonArray>();
  for(int i=0;i<nAcc&&i<MAXACC;i++){ JsonObject o=ar.add<JsonObject>();
    o["l"]=AC[i].label; o["s"]=AC[i].s; o["w"]=AC[i].w; o["f"]=AC[i].f; o["auth"]=AC[i].auth; o["p"]=AC[i].codex?"x":"c"; }
  d["city"]=U.city; d["wt"]=U.wt; d["wc"]=U.wc; d["time"]=hms;
  d["bri"]=S.bri; d["ne"]=S.nEn; d["ns"]=S.nStart; d["nf"]=S.nEnd; d["nb"]=S.nBri; d["rot"]=S.rot; d["refresh"]=S.refresh; d["acyc"]=S.accCycle;
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
  if(server.hasArg("acyc")){S.accCycle=constrain(server.arg("acyc").toInt(),ACC_CYCLE_MIN,ACC_CYCLE_MAX);accIdx=0;redraw=true;}
  // step the turntable on demand from the panel (and restart the dwell so it doesn't jump again)
  if(server.hasArg("accnext")&&nAcc>1){accIdx=(accIdx+1)%nAcc;lastAcc=millis();render(false);}
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
  // a pre-5.0 EEPROM image has no accCycle byte -> whatever was at that offset; re-default it
  if(S.accCycle<ACC_CYCLE_MIN||S.accCycle>ACC_CYCLE_MAX) S.accCycle=ACC_CYCLE_DEF;
  analogWriteRange(255); analogWriteFreq(22000); pinMode(TFT_BL,OUTPUT); applyBacklight();

  tft.init(); tft.setRotation(S.rot); tft.startWrite();
  C_BG=TFT_BLACK; C_PANEL=tft.color565(0x17,0x1f,0x2e); C_LINE=tft.color565(0x2c,0x37,0x4a);
  C_CORAL=tft.color565(0xff,0x7a,0x55); C_CYAN=tft.color565(0x3f,0xd2,0xdd); C_WHITE=TFT_WHITE;
  C_GRAY=tft.color565(0xa4,0xb0,0xc2); C_DIM=tft.color565(0x74,0x85,0x9b);
  C_GREEN=tft.color565(0x54,0xd3,0x6e); C_AMBER=tft.color565(0xf0,0xad,0x36); C_RED=tft.color565(0xff,0x4d,0x68); C_SKY=tft.color565(0x84,0xcd,0xf2);
  C_CODEX=tft.color565(0xa9,0x8b,0xff); C_CODEX_DIM=tft.color565(0x4a,0x3d,0x80);
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
  // account turntable: S.accCycle seconds per account. To show ONE account instead, point the
  // device at the collector's /usage?acct=<label> — that serves a single account and the header
  // reverts to the classic title.
  if(nAcc>1 && millis()-lastAcc>=(unsigned long)S.accCycle*1000){
    lastAcc=millis(); accIdx=(accIdx+1)%nAcc; render(false);
  }
  if(millis()-lastFetch>=(unsigned long)S.refresh*1000){ lastFetch=millis(); fetchUsage(); render(false); }
}
