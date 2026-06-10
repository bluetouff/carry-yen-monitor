(function(){
  "use strict";
  var CONTRACT_YEN = 12500000;
  var state = { spot:null, fxSeries:[], cot:[], move4w:null, generated:null };

  var $ = function(id){ return document.getElementById(id); };
  function fmt(n,d){ if(n===null||n===undefined||isNaN(n)) return "—"; return Number(n).toLocaleString("fr-FR",{minimumFractionDigits:d,maximumFractionDigits:d}); }
  function signed(n,d){ var s = n>0?"+":""; return s+fmt(n,d); }

  // ---------- chargement du snapshot local (meme origine, aucun tiers) ----------
  function loadSnapshot(){
    return fetch("data.json", {cache:"no-store"}).then(function(r){ if(!r.ok) throw 0; return r.json(); }).then(function(j){
      state.fxSeries = (j.fx||[]).filter(function(p){ return p && p.v; });
      state.cot = (j.cot||[]).filter(function(p){ return p && p.d && p.net!==null && p.net!==undefined; });
      if(state.fxSeries.length){ state.spot = state.fxSeries[state.fxSeries.length-1].v; }
      if(j.spot){ state.spot = j.spot; }
      if(j.rates){
        if(j.rates.boj!==null && j.rates.boj!==undefined) $("iBoj").value = j.rates.boj;
        if(j.rates.fed!==null && j.rates.fed!==undefined) $("iFed").value = j.rates.fed;
      }
      state.generated = j.generated || null;
      return j;
    });
  }

  // ---------- graphiques SVG ----------
  function lineChart(el, pts, opts){
    opts = opts || {};
    var W=620, H=200, pad={l:46,r:12,t:14,b:24};
    var ys = pts.map(function(p){return p.y;});
    var minY = opts.min!==undefined?opts.min:Math.min.apply(null,ys);
    var maxY = opts.max!==undefined?opts.max:Math.max.apply(null,ys);
    if(minY===maxY){ maxY+=1; minY-=1; }
    var iw=W-pad.l-pad.r, ih=H-pad.t-pad.b;
    function px(i){ return pad.l + (pts.length<2?0:(i/(pts.length-1))*iw); }
    function py(v){ return pad.t + ih - ((v-minY)/(maxY-minY))*ih; }
    var d="", zero=null;
    pts.forEach(function(p,i){ d += (i?"L":"M")+px(i).toFixed(1)+" "+py(p.y).toFixed(1)+" "; });
    var area = d + "L"+px(pts.length-1).toFixed(1)+" "+(pad.t+ih)+" L"+pad.l+" "+(pad.t+ih)+" Z";
    if(minY<0 && maxY>0){ zero = py(0); }
    var grid="";
    for(var g=0; g<=4; g++){ var gy=pad.t+(g/4)*ih; var gv=maxY-(g/4)*(maxY-minY);
      grid += '<line x1="'+pad.l+'" y1="'+gy.toFixed(1)+'" x2="'+(W-pad.r)+'" y2="'+gy.toFixed(1)+'" stroke="#22403c" stroke-width="1"/>';
      grid += '<text x="'+(pad.l-6)+'" y="'+(gy+3).toFixed(1)+'" fill="#6f9b94" font-size="10" font-family="ui-monospace,monospace" text-anchor="end">'+ (opts.fmtY?opts.fmtY(gv):Math.round(gv)) +'</text>';
    }
    var col = opts.color||"#00f0d0";
    var last = pts[pts.length-1];
    var svg = '<svg viewBox="0 0 '+W+' '+H+'" preserveAspectRatio="xMidYMid meet" role="img">'
      + grid
      + (zero!==null?'<line x1="'+pad.l+'" y1="'+zero.toFixed(1)+'" x2="'+(W-pad.r)+'" y2="'+zero.toFixed(1)+'" stroke="#456b65" stroke-width="1" stroke-dasharray="3 3"/>':'')
      + '<defs><linearGradient id="grad_'+opts.id+'" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="'+col+'" stop-opacity="0.22"/><stop offset="1" stop-color="'+col+'" stop-opacity="0"/></linearGradient></defs>'
      + '<path d="'+area+'" fill="url(#grad_'+opts.id+')"/>'
      + '<path d="'+d+'" fill="none" stroke="'+col+'" stroke-width="1.8" stroke-linejoin="round"/>'
      + '<circle cx="'+px(pts.length-1).toFixed(1)+'" cy="'+py(last.y).toFixed(1)+'" r="3" fill="'+col+'"/>'
      + '</svg>';
    el.innerHTML = svg;
  }

  // ---------- rendu ----------
  function status(ok,msg){ $("statusDot").className = "dot"+(ok?" live":""); $("statusTxt").textContent = msg; }

  function render(){
    var boj = parseFloat($("iBoj").value)||0, fed = parseFloat($("iFed").value)||0;
    var diff = fed - boj;

    if(state.spot){
      $("kSpot").textContent = fmt(state.spot,2);
      var fx = state.fxSeries;
      if(fx.length>6){
        var wk = ((state.spot/fx[fx.length-6].v)-1)*100;
        var cls = wk>0.02?"up":(wk<-0.02?"down":"flat");
        $("kSpotSub").innerHTML = 'réf. BCE &middot; <span class="'+cls+'">'+signed(wk,2)+'% / 5j</span>';
      }
      if(fx.length>22){ state.move4w = ((state.spot/fx[fx.length-21].v)-1)*100; }
    } else { $("kSpot").textContent="—"; }

    $("kDiff").textContent = fmt(diff,2)+" pt";
    $("kDiffSub").textContent = "≈ "+fmt(diff*100,0)+" pb de portage brut";

    var cot = state.cot, latest = cot.length?cot[cot.length-1]:null;
    if(latest){
      var net = latest.net;
      $("kNet").textContent = signed(net,0);
      $("kNet").className = "val "+(net<0?"up":"down");
      $("kNetSub").textContent = "au "+latest.d+(net<0?" · short yen":" · long yen");
      $("netBig").textContent = signed(net,0);
      $("netTag").className = "gtag "+(net<0?"short":"long");
      $("netTag").textContent = net<0?"pari CONTRE le yen (carry actif)":"pari POUR le yen (couverture / débouclage)";

      var nets = cot.map(function(c){return c.net;});
      var maxShort = Math.min.apply(null,nets);
      var crowd = net<0 && maxShort<0 ? Math.max(0,Math.min(100,(net/maxShort)*100)) : 0;
      $("crowdPct").textContent = fmt(crowd,0)+"% de l'extrême";
      $("crowdFill").style.width = crowd+"%";
      $("c1").textContent = fmt(crowd,0)+"%";
      $("f1").style.width = crowd+"%";

      if(state.spot){
        var notB = Math.abs(net)*CONTRACT_YEN/state.spot/1e9;
        $("notional").textContent = "≈ "+fmt(notB,1)+" Md$ "+(net<0?"de short yen":"de long yen");
      }

      var pts = cot.map(function(c,i){return {x:i,y:c.net};});
      lineChart($("cotChart"), pts, {id:"cot", color:"#ff6b9d", fmtY:function(v){return (v/1000).toFixed(0)+"k";}});
      $("cotStart").textContent = cot[0].d;
      $("cotEnd").textContent = latest.d;
    } else {
      $("kNet").textContent="—"; $("netBig").textContent="—";
      $("cotChart").innerHTML='<div class="err">positionnement CFTC indisponible</div>';
    }

    if(state.fxSeries.length){
      var fxs = state.fxSeries;
      var fpts = fxs.map(function(p,i){return {x:i,y:p.v};});
      lineChart($("fxChart"), fpts, {id:"fx", color:"#00f0d0", fmtY:function(v){return v.toFixed(0);}});
      $("fxStart").textContent = fxs[0].d;
      $("fxEnd").textContent = fxs[fxs.length-1].d;
      var lo=Math.min.apply(null,fxs.map(function(p){return p.v;})), hi=Math.max.apply(null,fxs.map(function(p){return p.v;}));
      $("fxRange").textContent = "plage 12m "+fmt(lo,1)+" - "+fmt(hi,1);
    }

    computeRisk(diff);
    computeCalc(diff);
  }

  function computeRisk(diff){
    var cot = state.cot, latest = cot.length?cot[cot.length-1]:null;
    var crowd = 0;
    if(latest && latest.net<0){
      var maxShort = Math.min.apply(null, cot.map(function(c){return c.net;}));
      if(maxShort<0) crowd = Math.max(0,Math.min(100,(latest.net/maxShort)*100));
    }
    var appro = 0;
    if(state.move4w!==null){ appro = Math.max(0,Math.min(100, -state.move4w*22)); }
    var compress = Math.max(0,Math.min(100, (5.25-diff)/5.25*100));

    $("c2").textContent = (state.move4w!==null?signed(state.move4w,1)+"%":"—");
    $("f2").style.width = appro+"%";
    $("c3").textContent = fmt(diff,2)+" pt";
    $("f3").style.width = compress+"%";

    var risk = Math.round(crowd*0.45 + appro*0.35 + compress*0.20);
    var band = risk<30?["Faible","var(--ok)"]:risk<55?["Modéré","var(--gold)"]:risk<78?["Élevé","var(--yen)"]:["Critique","var(--yen)"];
    $("kRisk").textContent = risk;
    $("kRisk").style.color = band[1];
    $("kRiskSub").innerHTML = band[0]+" &middot; surcharge "+fmt(crowd,0)+"%";

    $("needle").style.left = Math.max(2,Math.min(98,risk))+"%";
    var v = risk<30
      ? "Le carry domine. Différentiel large, shorts non extrêmes, yen stable. Le portage se gagne, mais surveiller tout reflux des shorts."
      : risk<55
      ? "Équilibre fragile. Le carry reste payant mais le matelas se réduit. Une hausse BoJ ou un choc de risque peut amorcer un débouclage."
      : risk<78
      ? "Risque élevé. Shorts surchargés et/ou yen qui s'apprécie. Configuration propice à des rachats forcés et à de la volatilité."
      : "Risque critique. Positionnement extrême et momentum yen défavorable au carry. Mémoire d'août 2024.";
    $("verdict").textContent = v;
  }

  function computeCalc(diff){
    var lev = parseFloat($("iLev").value)||1;
    var move = parseFloat($("iMove").value)||0;
    var carry = diff*lev;
    var total = (diff + move)*lev;
    $("oCarry").textContent = signed(carry,2)+"%";
    $("oCarry").className = "ov "+(carry>=0?"down":"up");
    $("oTotal").textContent = signed(total,2)+"%";
    $("oTotal").className = "ov "+(total>=0?"down":"up");
    $("oBreak").textContent = "-"+fmt(diff,2)+"%";
    if(state.spot){ $("oLevel").textContent = fmt(state.spot*(1-diff/100),2); }
    else { $("oLevel").textContent="—"; }
  }

  ["iBoj","iFed","iLev","iMove"].forEach(function(id){
    $(id).addEventListener("input", function(){ render(); });
  });

  // ---------- init ----------
  $("ts").textContent = "Page chargée le " + new Date().toLocaleString("fr-FR");
  status(false,"chargement du snapshot...");

  loadSnapshot().then(function(j){
    render();
    var when = j.generated ? new Date(j.generated).toLocaleString("fr-FR") : "—";
    status(true, "snapshot du "+when);
  }).catch(function(){
    render();
    status(false, "snapshot indisponible, calculateur seul");
  });
})();
