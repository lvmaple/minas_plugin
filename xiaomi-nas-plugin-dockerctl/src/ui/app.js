/* Docker Console — Full-featured NAS plugin */
(function(){
"use strict";

// ---- API base (desktop Electron path-safe) ----
function apiUrl(ep){
  var p=window.location.pathname;
  var m=p.match(/(\/plugin\/[^\/]+\/[^\/]+)/);
  var base=m?m[1]:p.replace(/\/index\.html$/,"").replace(/\/$/,"");
  return base+"/api/"+ep;
}
function $(id){return document.getElementById(id);}
function esc(s){
  return String(s==null?"":s).replace(/&/g,"&amp;").replace(/</g,"&lt;")
    .replace(/>/g,"&gt;").replace(/"/g,"&quot;");
}
function toast(msg,ms){
  var el=$("toast");el.textContent=msg;el.classList.add("show");
  clearTimeout(el._t);el._t=setTimeout(function(){el.classList.remove("show");},ms||2200);
}
function fetchJSON(url,opts){
  return fetch(url,opts).then(function(r){
    if(!r.ok&&r.status>=500)throw new Error("HTTP "+r.status);
    return r.json();
  });
}

// ==================== TAB NAV ====================
document.querySelectorAll(".tab").forEach(function(btn){
  btn.addEventListener("click",function(){
    document.querySelectorAll(".tab").forEach(function(b){b.classList.remove("active");});
    btn.classList.add("active");
    var v=btn.getAttribute("data-view");
    document.querySelectorAll(".view").forEach(function(s){s.classList.remove("active");});
    var t=$("view-"+v);if(t)t.classList.add("active");
    if(v==="overview")loadOverview();
    if(v==="containers")loadContainers();
    if(v==="images")loadImages();
    if(v==="volumes")loadVolumes();
    if(v==="networks")loadNetworks();
    if(v==="logs")loadLogs();
    if(v==="deploy"){loadDeployImages();loadDeployNetworks();}
  });
});

// data-goto shortcuts
document.querySelectorAll("[data-goto]").forEach(function(el){
  el.addEventListener("click",function(){
    var v=el.getAttribute("data-goto");
    var tab=document.querySelector('.tab[data-view="'+v+'"]');
    if(tab)tab.click();
  });
});

// ==================== DRAWER ====================
function openDrawer(title,html){
  $("drawer-title").textContent=title;
  $("drawer-body").innerHTML=html;
  $("drawer").classList.add("open");
}
function closeDrawer(){$("drawer").classList.remove("open");}
$("drawer-x").addEventListener("click",closeDrawer);
$("drawer-close").addEventListener("click",closeDrawer);

// ==================== MODAL ====================
function openModal(title,bodyHTML,footButtons){
  $("modal-title").textContent=title;
  $("modal-body").innerHTML=bodyHTML;
  $("modal-foot").innerHTML=footButtons||'<button class="btn btn-ghost" onclick="document.getElementById(\'modal\').classList.remove(\'open\')">关闭</button>';
  $("modal").classList.add("open");
}
function closeModal(){$("modal").classList.remove("open");}
$("modal-x").addEventListener("click",closeModal);
$("modal-close").addEventListener("click",closeModal);

// ==================== OVERVIEW ====================
function loadOverview(){
  fetchJSON(apiUrl("overview")).then(function(d){
    $("stat-running").textContent=d.containers_running!=null?d.containers_running:"-";
    $("stat-total").textContent=d.containers_total!=null?d.containers_total:"-";
    $("stat-images").textContent=d.images!=null?d.images:"-";
    $("stat-volumes").textContent=d.volumes!=null?d.volumes:"-";

    var st=d.states||{};
    var total=Math.max(1,d.containers_total||1);
    var rows=[
      ["running","运行","bar-running",st.running||0],
      ["exited","停止","bar-exited",st.exited||0],
      ["paused","暂停","bar-paused",st.paused||0],
      ["other","其他","bar-other",(st.created||0)+(st.restarting||0)+(st.other||0)]
    ];
    $("state-bars").innerHTML=rows.map(function(r){
      var pct=Math.round(r[3]/total*100);
      return '<div class="state-row"><span class="lbl">'+r[1]+'</span>'+
        '<div class="bar-wrap"><div class="bar '+r[2]+'" style="width:'+pct+'%"></div></div>'+
        '<span class="cnt">'+r[3]+"</span></div>";
    }).join("");

    fetchJSON(apiUrl("containers")).then(function(c){
      var items=(c.items||[]).filter(function(x){
        return (x.State||"").toLowerCase()==="running";
      }).slice(0,8);
      var box=$("overview-running");
      if(!items.length){box.innerHTML='<div class="empty"><div class="icon">📦</div>暂无运行中的容器</div>';return;}
      box.innerHTML=items.map(function(x){
        return '<div class="mini-item" data-id="'+esc(x.ID)+'" data-name="'+esc(x.Names)+'">'+
          '<span class="dot dot-running"></span>'+
          '<span class="name">'+esc(x.Names)+"</span>"+
          '<span class="img">'+esc(x.Image)+"</span></div>";
      }).join("");
      box.querySelectorAll(".mini-item").forEach(function(el){
        el.addEventListener("click",function(){
          openContainerDetail(el.getAttribute("data-id"),el.getAttribute("data-name"));
        });
      });
    }).catch(function(){});
  }).catch(function(){toast("无法连接后端");});
}

// ==================== CONTAINERS ====================
var allContainers=[];
function loadContainers(){
  var showAll=$("chk-all").checked?"1":"0";
  fetchJSON(apiUrl("containers?all="+showAll)).then(function(d){
    allContainers=d.items||[];
    renderContainers();
  }).catch(function(){
    $("container-list").innerHTML='<div class="empty"><div class="icon">⚠️</div>加载失败</div>';
  });
}
function renderContainers(){
  var kw=($("search-box").value||"").toLowerCase();
  var items=allContainers.filter(function(x){
    if(!kw)return true;
    return(x.Names||"").toLowerCase().indexOf(kw)>=0||
      (x.Image||"").toLowerCase().indexOf(kw)>=0||
      (x.ID||"").toLowerCase().indexOf(kw)>=0;
  });
  var box=$("container-list");
  if(!items.length){box.innerHTML='<div class="empty"><div class="icon">📦</div>暂无容器</div>';return;}
  box.innerHTML=items.map(function(x){
    var state=(x.State||"").toLowerCase()||"other";
    var stCls="status-"+(["running","exited","paused"].indexOf(state)>=0?state:"other");
    var dotCls="dot-"+(["running","exited","paused"].indexOf(state)>=0?state:"other");
    return '<div class="c-card" data-id="'+esc(x.ID)+'" data-name="'+esc(x.Names)+'">'+
      '<div class="row1"><span class="'+dotCls+'"></span><span class="cname">'+esc(x.Names)+"</span></div>"+
      '<div class="cimg">'+esc(x.Image)+"</div>"+
      '<div class="meta">'+
      '<span class="chip '+stCls+'">'+esc(x.State||"?")+"</span>"+
      '<span class="chip">'+esc(x.Status||"")+"</span>"+
      (x.Ports?'<span class="chip">'+esc(x.Ports)+"</span>":"")+
      "</div>"+
      '<div class="actions">'+
      (state==="exited"||state==="created"?'<button class="btn btn-sm btn-success" data-act="start">启动</button>':"")+
      (state==="paused"?'<button class="btn btn-sm btn-success" data-act="unpause">恢复</button>':"")+
      (state==="running"?'<button class="btn btn-sm btn-warn" data-act="stop">停止</button>':"")+
      (state==="running"?'<button class="btn btn-sm btn-primary" data-act="restart">重启</button>':"")+
      (state==="running"?'<button class="btn btn-sm btn-warn" data-act="pause">暂停</button>':"")+
      '<button class="btn btn-sm btn-ghost" data-act="logs">日志</button>'+
      '<button class="btn btn-sm btn-danger" data-act="remove">删除</button>'+
      "</div></div>";
  }).join("");
  box.querySelectorAll(".c-card").forEach(function(card){
    card.addEventListener("click",function(e){
      if(e.target.tagName==="BUTTON")return;
      openContainerDetail(card.getAttribute("data-id"),card.getAttribute("data-name"));
    });
    card.querySelectorAll("button").forEach(function(btn){
      btn.addEventListener("click",function(e){
        e.stopPropagation();
        var act=btn.getAttribute("data-act");
        var id=card.getAttribute("data-id");
        var name=card.getAttribute("data-name");
        if(act==="logs"){showLogs(id,name);return;}
        // remove 二次确认统一放在 doAction（避免弹两次框）
        doAction(id,act);
      });
    });
  });
}
function doAction(id,act){
  if(act==="remove"){
    var card=document.querySelector('.c-card[data-id="'+id+'"]');
    var name=card?(card.getAttribute("data-name")||id):id;
    var state=card?(card.querySelector(".chip.status-running,.chip.status-exited,.chip.status-paused")||{}).textContent||"":"";
    var isRunning=state.trim()==="running";
    if(isRunning){
      if(!confirm("容器 "+name+" 正在运行。确定强制删除？（取消则先停止再删除）")){
        toast("正在停止…");
        fetchJSON(apiUrl("containers/"+encodeURIComponent(id)+"/stop"),{method:"POST"}).then(function(){
          return fetchJSON(apiUrl("containers/"+encodeURIComponent(id)+"/remove"),{
            method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({force:false})
          });
        }).then(function(d){
          if(d.ok){toast("已停止并删除");loadContainers();loadOverview();}
          else toast("删除失败: "+(d.error||""));
        }).catch(function(){toast("请求失败");});
        return;
      }
      fetchJSON(apiUrl("containers/"+encodeURIComponent(id)+"/remove"),{
        method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({force:true})
      }).then(function(d){
        if(d.ok){toast("已强制删除");loadContainers();loadOverview();}
        else toast("删除失败: "+(d.error||""));
      }).catch(function(){toast("请求失败");});
      return;
    }
    if(!confirm("确认删除容器 "+name+"？此操作不可恢复。"))return;
    fetchJSON(apiUrl("containers/"+encodeURIComponent(id)+"/remove"),{
      method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({force:false})
    }).then(function(d){
      if(d.ok){toast("已删除");loadContainers();loadOverview();}
      else toast("删除失败: "+(d.error||""));
    }).catch(function(){toast("请求失败");});
    return;
  }
  toast("执行中…");
  fetchJSON(apiUrl("containers/"+encodeURIComponent(id)+"/"+act),{method:"POST"}).then(function(d){
    if(d.ok){toast("操作成功");loadContainers();loadOverview();}
    else toast("失败: "+(d.error||"未知错误"));
  }).catch(function(){toast("请求失败");});
}

function openContainerDetail(id,name){
  openDrawer(name||id,'<div class="empty">加载中…</div>');
  fetchJSON(apiUrl("containers/"+encodeURIComponent(id))).then(function(d){
    var c=d.detail||{};
    var state=(c.State&&c.State.Status)||"";
    var cfg=c.Config||{};
    var livePorts=(c.NetworkSettings&&c.NetworkSettings.Ports)||{};
    var bindings=(c.HostConfig&&c.HostConfig.PortBindings)||{};
    var portKeys=Object.keys(livePorts);
    Object.keys(bindings).forEach(function(k){if(portKeys.indexOf(k)<0)portKeys.push(k);});
    var portLines=portKeys.map(function(k){
      var v=livePorts[k]||bindings[k];
      if(!v||!v.length)return esc(k)+" → (未映射)";
      return esc(k)+" → "+v.map(function(p){return (p.HostIp?esc(p.HostIp)+":":"")+esc(p.HostPort);}).join(", ");
    }).join("<br>")||"无端口映射";
    var mounts=(c.Mounts||[]).map(function(m){
      return esc(m.Source||"")+" → "+esc(m.Destination||"")+(m.RW?"":" (只读)");
    }).join("<br>")||"无挂载";

    $("drawer-body").innerHTML=
      '<div class="d-section"><h3>基本信息</h3>'+
      kv("名称",name||(c.Name||"").replace(/^\//,""))+
      kv("ID",(id||"").slice(0,12))+
      kv("镜像",cfg.Image||"")+
      kv("状态",state)+
      kv("创建",c.Created||"")+
      kv("启动命令",(cfg.Cmd||[]).join(" "))+
      kv("入口",(cfg.Entrypoint||[]).join(" "))+
      "</div>"+
      '<div class="d-section"><h3>端口映射</h3><div class="kv"><div class="v">'+portLines+"</div></div></div>"+
      '<div class="d-section"><h3>存储挂载</h3><div class="kv"><div class="v">'+mounts+"</div></div></div>"+
      '<div class="d-section"><h3>日志</h3>'+
      '<button class="btn btn-sm btn-primary" id="btn-load-logs">加载日志</button>'+
      '<div class="log-box" id="log-box" style="margin-top:8px">点击「加载日志」查看</div></div>';
    $("drawer-title").textContent=name||id;
    var lb=$("btn-load-logs");
    if(lb)lb.addEventListener("click",function(){showLogs(id,name);});
  }).catch(function(){
    $("drawer-body").innerHTML='<div class="empty">加载失败</div>';
  });
}

function showLogs(id,name){
  openDrawer("日志 · "+(name||id),'<div class="empty">加载中…</div>');
  fetchJSON(apiUrl("containers/"+encodeURIComponent(id)+"/logs?tail=300")).then(function(d){
    var text=d.logs||d.error||"(空)";
    $("drawer-body").innerHTML='<div class="log-box">'+esc(text)+"</div>";
  }).catch(function(){
    $("drawer-body").innerHTML='<div class="empty">加载失败</div>';
  });
}

function kv(k,v){
  return '<div class="kv"><span class="k">'+esc(k)+'</span><span class="v">'+esc(v)+"</span></div>";
}
function fmtSize(b){
  b=Number(b)||0;
  if(b>=1e9)return (b/1e9).toFixed(2)+" GB";
  if(b>=1e6)return (b/1e6).toFixed(1)+" MB";
  if(b>=1e3)return (b/1e3).toFixed(1)+" KB";
  return b+" B";
}

// ==================== IMAGE DETAIL (drawer) ====================
function openImageDetail(id,name){
  var title=(name&&name.indexOf("<none>")<0)?name:(id||"").replace("sha256:","").slice(0,16);
  openDrawer(title,'<div class="empty">加载中…</div>');
  fetchJSON(apiUrl("images/"+encodeURIComponent(id))).then(function(d){
    var c=d.detail||{};
    var cfg=c.Config||{};
    var rootfs=c.RootFS||{};
    var joinLines=function(arr){return (arr||[]).map(esc).join("<br>")||"—";};
    var labels=Object.keys(cfg.Labels||{}).map(function(k){return esc(k)+" = "+esc(cfg.Labels[k]);}).join("<br>")||"—";
    var layers=(rootfs.Layers||[]).map(function(l){return esc(String(l).replace("sha256:","").slice(0,16));}).join("<br>")||"—";
    $("drawer-body").innerHTML=
      '<div class="d-section"><h3>基本信息</h3>'+
      kv("镜像",title)+
      kv("ID",String(c.Id||id||"").replace("sha256:","").slice(0,16))+
      kv("创建",c.Created||"—")+
      kv("大小",(c.Size?fmtSize(c.Size):"—")+(c.VirtualSize?"（含基础层 "+fmtSize(c.VirtualSize)+"）":""))+
      kv("架构/系统",(c.Architecture||"—")+" / "+(c.Os||"—"))+
      kv("构建工具","Docker "+(c.DockerVersion||"未知"))+
      kv("作者",c.Author||"—")+
      "</div>"+
      '<div class="d-section"><h3>入口与配置</h3>'+
      kv("Entrypoint",(cfg.Entrypoint||[]).join(" ")||"—")+
      kv("Cmd",(cfg.Cmd||[]).join(" ")||"—")+
      kv("用户",cfg.User||"—")+
      kv("工作目录",cfg.WorkingDir||"—")+
      "</div>"+
      '<div class="d-section"><h3>暴露端口</h3><div class="kv"><div class="v">'+joinLines(Object.keys(cfg.ExposedPorts||{}))+"</div></div></div>"+
      '<div class="d-section"><h3>数据卷</h3><div class="kv"><div class="v">'+joinLines(Object.keys(cfg.Volumes||{}))+"</div></div></div>"+
      '<div class="d-section"><h3>环境变量</h3><div class="kv"><div class="v">'+joinLines(cfg.Env)+"</div></div></div>"+
      '<div class="d-section"><h3>标签</h3><div class="kv"><div class="v">'+labels+"</div></div></div>"+
      '<div class="d-section"><h3>层信息（'+(rootfs.Layers||[]).length+" 层）</h3><div class=\"kv\"><div class=\"v\">"+layers+"</div></div></div>"+
      '<div class="d-section"><h3>仓库摘要</h3><div class="kv"><div class="v">'+joinLines(c.RepoDigests)+"</div></div></div>";
    $("drawer-title").textContent=title;
  }).catch(function(){
    $("drawer-body").innerHTML='<div class="empty">加载失败</div>';
  });
}

// ==================== IMAGES ====================
var allImages=[];
function fmtPullTime(ts){
  var d=new Date(ts*1000);
  function p(n){return (n<10?"0":"")+n;}
  return d.getFullYear()+"-"+p(d.getMonth()+1)+"-"+p(d.getDate())+" "+p(d.getHours())+":"+p(d.getMinutes());
}
function loadImages(){
  fetchJSON(apiUrl("images")).then(function(d){
    allImages=d.items||[];
    renderImages();
  }).catch(function(){
    $("image-list").innerHTML='<div class="empty"><div class="icon">⚠️</div>加载失败</div>';
  });
}
function renderImages(){
  var kw=($("search-img").value||"").toLowerCase();
  var items=allImages.filter(function(x){
    if(!kw)return true;
    return(x.Repository||"").toLowerCase().indexOf(kw)>=0||
      (x.Tag||"").toLowerCase().indexOf(kw)>=0||
      (x.ID||"").toLowerCase().indexOf(kw)>=0;
  });
  var sort=$("image-sort").value;
  if(sort!=="created")items.sort(function(a,b){
    var at=Number(a.PulledAt)||0,bt=Number(b.PulledAt)||0;
    if(!at||!bt)return at? -1 : bt? 1 : 0;
    return sort==="pull-asc"?at-bt:bt-at;
  });
  var box=$("image-list");
  if(!items.length){box.innerHTML='<div class="empty"><div class="icon">💿</div>暂无镜像</div>';return;}
  box.innerHTML=items.map(function(x){
    var full=(x.Repository||"<none>")+":"+(x.Tag||"none");
    return '<div class="c-card" data-img="'+esc(full)+'" data-id="'+esc(x.ID)+'">'+
      '<div class="row1"><span class="cname">'+esc(x.Repository||"<none>")+"</span></div>"+
      '<div class="cimg">'+esc(x.Tag||"")+" · "+esc(x.ID||"").replace("sha256:","").slice(0,12)+"</div>"+
      '<div class="meta">'+
      '<span class="chip">'+esc(x.Size||"")+"</span>"+
      '<span class="chip">'+(x.PulledAt?"拉取 "+fmtPullTime(x.PulledAt):"拉取时间未知")+"</span>"+
      '<span class="chip">创建 '+esc(x.CreatedSince||"")+"</span>"+
      "</div>"+
      '<div class="actions">'+
      '<button class="btn btn-sm btn-primary" data-act="run">部署</button>'+
      '<button class="btn btn-sm btn-danger" data-act="rmi">删除</button>'+
      "</div></div>";
  }).join("");
  box.querySelectorAll(".c-card").forEach(function(card){
    card.addEventListener("click",function(e){
      if(e.target.tagName==="BUTTON")return;
      openImageDetail(card.getAttribute("data-id"),card.getAttribute("data-img"));
    });
    card.querySelectorAll("button").forEach(function(btn){
      btn.addEventListener("click",function(e){
        e.stopPropagation();
        var act=btn.getAttribute("data-act");
        var img=card.getAttribute("data-img");
        if(act==="run"){
          // jump to deploy with image prefilled
          document.querySelector('.tab[data-view="deploy"]').click();
          var sel=$("dep-image");
          if(sel){
            var has=[].some.call(sel.options,function(o){return o.value===img;});
            if(!has)addImageOption(sel,img);
            sel.value=img;
            syncImageCustom();
          }
          toast("已填入镜像: "+img);
          return;
        }
        if(img.indexOf("<none>")>=0){toast("无标签镜像请用 ID 删除");return;}
        if(!confirm("确认删除镜像 "+img+"？"))return;
        fetchJSON(apiUrl("images/"+encodeURIComponent(img)+"/remove"),{
          method:"POST",headers:{"Content-Type":"application/json"},body:"{}"
        }).then(function(d){
          if(d.ok){toast("已删除");loadImages();loadOverview();}
          else toast("删除失败: "+(d.error||""));
        }).catch(function(){toast("请求失败");});
      });
    });
  });
}

// ==================== OPS LOG ====================
var logExpanded={};
function fmtTime(ts){
  var d=new Date((ts||0)*1000);
  function p(n){return (n<10?"0":"")+n;}
  return p(d.getMonth()+1)+"-"+p(d.getDate())+" "+p(d.getHours())+":"+p(d.getMinutes())+":"+p(d.getSeconds());
}
function loadLogs(){
  fetchJSON(apiUrl("logs?tail=200")).then(function(d){
    var items=(d.items||[]).slice().reverse();
    var box=$("log-list");
    if(!box)return;
    if(!items.length){box.innerHTML='<div class="empty"><div class="icon">📜</div>暂无操作记录</div>';return;}
    box.innerHTML=items.map(function(x){
      var key=x.ts+"|"+x.kind+"|"+x.target;
      var open=!!logExpanded[key];
      return '<div class="log-item">'+
        '<div class="log-row" data-key="'+esc(key)+'">'+
          '<span class="log-st '+(x.ok?"log-st-ok":"log-st-err")+'">'+(x.ok?"成功":"失败")+"</span>"+
          '<span class="log-kind">'+esc(x.kind||"?")+"</span>"+
          '<span class="log-target">'+esc(x.target||"")+"</span>"+
          '<span class="log-time">'+fmtTime(x.ts)+"</span>"+
          (x.detail?'<span class="log-caret">'+(open?"▾":"▸")+"</span>":"")+
        "</div>"+
        (x.detail?'<div class="log-expand'+(open?" open":"")+'">'+esc(x.detail)+"</div>":"")+
      "</div>";
    }).join("");
  }).catch(function(){
    var box=$("log-list");
    if(box)box.innerHTML='<div class="empty"><div class="icon">⚠️</div>日志加载失败</div>';
  });
}
$("log-list").addEventListener("click",function(e){
  var el=e.target;
  while(el&&el!==this&&!el.classList.contains("log-row"))el=el.parentElement;
  if(!el||el===this)return;
  var exp=el.parentElement.querySelector(".log-expand");
  if(!exp)return;
  var key=el.getAttribute("data-key");
  var on=exp.classList.toggle("open");
  logExpanded[key]=on;
  var caret=el.querySelector(".log-caret");
  if(caret)caret.textContent=on?"▾":"▸";
});
$("btn-refresh-logs").addEventListener("click",loadLogs);
$("btn-clear-logs").addEventListener("click",function(){
  if(!confirm("确认清空操作日志？"))return;
  fetchJSON(apiUrl("logs/clear"),{method:"POST"}).then(function(){
    toast("已清空");loadLogs();
  }).catch(function(){toast("请求失败");});
});
// 日志页停留时自动刷新
setInterval(function(){
  var v=document.getElementById("view-logs");
  if(v&&v.classList.contains("active"))loadLogs();
},5000);

// ==================== PULL PROGRESS ====================
var pullTimer=null;
function stopPullWatch(){if(pullTimer){clearInterval(pullTimer);pullTimer=null;}}
function startPullWatch(){stopPullWatch();renderPullStatus();pullTimer=setInterval(renderPullStatus,1500);}
window.__cancelPull=function(){
  if(!confirm("确认取消当前拉取任务？"))return;
  fetchJSON(apiUrl("images/pull/cancel"),{method:"POST"}).then(function(d){
    if(d.ok){toast("已取消");renderPullStatus();}
    else toast(d.error||"取消失败");
  }).catch(function(){toast("请求失败");});
};
function renderPullStatus(){
  fetchJSON(apiUrl("images/pull/status")).then(function(d){
    var t=d.task,box=$("pull-status");
    if(!box)return;
    if(!t){box.style.display="none";return;}
    box.style.display="block";
    var now=t.status==="running"?Date.now()/1000:(t.ended||t.updated||t.started);
    var elapsed=Math.max(0,Math.round(now-(t.started||0)));
    if(t.status==="running"){
      var last=(t.frames||[]).slice(-3).map(esc).join("<br>");
      box.className="pull-status";
      box.innerHTML='<span class="spin"></span><b>正在拉取</b> '+esc(t.image)+(t.using_proxy?"（本次代理）":"")+
        '（已用 '+elapsed+'s）<button class="btn btn-ghost btn-sm" style="float:right" onclick="window.__cancelPull&&window.__cancelPull()">取消拉取</button>'+
        '<div class="frames">'+(last||"连接仓库中…")+"</div>";
    }else if(t.status==="success"){
      box.className="pull-status ok";
      box.innerHTML="✅ "+esc(t.image)+" 拉取完成（用时 "+elapsed+"s）";
      stopPullWatch();loadImages();loadOverview();
    }else{
      box.className="pull-status err";
      box.innerHTML="❌ 拉取失败 "+esc(t.image)+"："+esc((t.error||"").slice(0,200));
      stopPullWatch();
    }
  }).catch(function(){});
}

// pull image
var pullProxyStorageKey="dockerctl.pullProxy";
function rememberPullProxy(value){
  try{
    if(value)window.localStorage.setItem(pullProxyStorageKey,value);
    else window.localStorage.removeItem(pullProxyStorageKey);
  }catch(e){}
}
function savedPullProxy(){
  try{return window.localStorage.getItem(pullProxyStorageKey)||"";}
  catch(e){return "";}
}
$("btn-pull-img").addEventListener("click",function(){
  openModal("拉取镜像",
    '<label>镜像源<select id="pull-registry" onchange="document.getElementById(\'pull-custom-prefix\').style.display=this.value===\'__custom__\'?\'block\':\'none\'">'+
    '<option value="">直连 Docker Hub</option>'+
    '<option value="docker.m.daocloud.io">DaoCloud 加速</option>'+
    '<option value="docker.nju.edu.cn">南京大学镜像</option>'+
    '<option value="docker.1ms.run">1ms.run</option>'+
    '<option value="__custom__">自定义前缀…</option>'+
    "</select></label>"+
    '<input type="text" id="pull-custom-prefix" placeholder="自定义前缀，如 my.mirror.example.com" style="display:none;margin-bottom:10px">'+
    '<label>镜像名（含标签）<input type="text" id="pull-img-name" placeholder="如 nginx:latest"></label>'+
    '<label>拉取代理（可选，自动记住）<input type="text" id="pull-proxy" placeholder="http://192.168.1.2:7890" autocomplete="off"></label>'+
    '<div style="font-size:11px;color:#64748b;line-height:1.6;margin-bottom:10px">地址保存在当前浏览器；清空后直连。代理须能从 NAS 访问，支持无账号密码的 HTTP(S) 代理。</div>'+
    '<div style="font-size:11px;color:#64748b;line-height:1.6">走镜像站时自动补 <code>library/</code> 路径，拉取成功后自动打回短名标签（如 <code>nginx:latest</code>）。</div>',
    '<button class="btn btn-ghost" onclick="document.getElementById(\'modal\').classList.remove(\'open\')">取消</button>'+
    '<button class="btn btn-primary" id="btn-pull-go">拉取</button>'
  );
  var proxyInput=$("pull-proxy");
  proxyInput.value=savedPullProxy();
  proxyInput.addEventListener("input",function(){rememberPullProxy(proxyInput.value.trim());});
  setTimeout(function(){
    var go=$("btn-pull-go");
    if(go)go.addEventListener("click",function(){
      var raw=($("pull-img-name").value||"").trim();
      if(!raw){toast("请输入镜像名");return;}
      var prefix=($("pull-registry")||{}).value||"";
      if(prefix==="__custom__")prefix=($("pull-custom-prefix").value||"").trim();
      // 名字里已带仓库主机（首段含 . 或 :）则不再加前缀
      var first=raw.split("/")[0];
      var isFull=raw.indexOf("/")>0&&(first.indexOf(".")>=0||first.indexOf(":")>=0);
      var image=raw,tagAs="";
      if(prefix&&!isFull){
        image=prefix+"/"+(raw.indexOf("/")>=0?raw:"library/"+raw);
        tagAs=raw;
      }
      var proxy=($("pull-proxy").value||"").trim();
      toast("开始拉取…");
      fetchJSON(apiUrl("images/pull"),{
        method:"POST",headers:{"Content-Type":"application/json"},
        body:JSON.stringify({image:image,tag_as:tagAs,proxy:proxy})
      }).then(function(d){
        if(d.ok){
          closeModal();
          toast(d.already?"该镜像正在拉取中，已为你打开进度":"已开始拉取: "+image);
          startPullWatch();
        }else{
          toast((d.error||"拉取失败"));
          if((d.error||"").indexOf("已有拉取")>=0)startPullWatch();
        }
      }).catch(function(){toast("请求失败");});
    });
  },50);
});

// ==================== REGISTRY LOGIN ====================
function loadRegistryList(){
  fetchJSON(apiUrl("registry/auth")).then(function(d){
    var list=d.registries||[];
    var box=$("reg-list");
    if(!box)return;
    box.innerHTML=list.length?("已登录：<br>"+list.map(esc).join("<br>")):"尚未登录任何仓库";
  }).catch(function(){
    var box=$("reg-list");
    if(box)box.textContent="读取失败";
  });
}
$("btn-registry-login").addEventListener("click",function(){
  openModal("镜像仓库登录",
    '<label>仓库地址<input type="text" id="reg-server" placeholder="如 docker.io / harbor.example.com:5000"></label>'+
    '<label>用户名<input type="text" id="reg-user" placeholder="Token 登录可填用户名或 token"></label>'+
    '<label>密码 / Token<input type="password" id="reg-pass" placeholder="仅传给 Docker 引擎，插件不保存"></label>'+
    '<div class="empty" id="reg-list" style="padding:10px">读取中…</div>',
    '<button class="btn btn-ghost" onclick="document.getElementById(\'modal\').classList.remove(\'open\')">取消</button>'+
    '<button class="btn btn-ghost" id="btn-reg-logout">退出登录</button>'+
    '<button class="btn btn-primary" id="btn-reg-login">登录</button>'
  );
  setTimeout(function(){
    loadRegistryList();
    $("btn-reg-login").addEventListener("click",function(){
      var server=($("reg-server").value||"").trim();
      var user=($("reg-user").value||"").trim();
      var pass=$("reg-pass").value||"";
      if(!server){toast("请填写仓库地址");return;}
      if(!user||!pass){toast("请填写用户名和密码");return;}
      toast("登录中…",8000);
      fetchJSON(apiUrl("registry/login"),{
        method:"POST",headers:{"Content-Type":"application/json"},
        body:JSON.stringify({server:server,username:user,password:pass})
      }).then(function(d){
        if(d.ok){toast("登录成功");$("reg-pass").value="";loadRegistryList();}
        else toast("登录失败: "+(d.error||""));
      }).catch(function(){toast("请求失败");});
    });
    $("btn-reg-logout").addEventListener("click",function(){
      var server=($("reg-server").value||"").trim();
      if(!server){toast("请填写要退出的仓库地址");return;}
      fetchJSON(apiUrl("registry/logout"),{
        method:"POST",headers:{"Content-Type":"application/json"},
        body:JSON.stringify({server:server})
      }).then(function(d){
        if(d.ok){toast("已退出登录");loadRegistryList();}
        else toast("退出失败: "+(d.error||""));
      }).catch(function(){toast("请求失败");});
    });
  },50);
});

// ==================== DEPLOY ====================
function addImageOption(sel,val){
  var o=document.createElement("option");
  o.value=val;o.textContent=val;
  var custom=sel.querySelector('option[value="__custom__"]');
  sel.insertBefore(o,custom||null);
  sel.value=val;
}
function syncImageCustom(){
  var sel=$("dep-image"),c=$("dep-image-custom");
  if(!sel||!c)return;
  c.style.display=sel.value==="__custom__"?"block":"none";
}
function loadDeployImages(){
  fetchJSON(apiUrl("images")).then(function(d){
    var sel=$("dep-image");
    if(!sel)return;
    var cur=sel.value;
    var opts=['<option value="">请选择镜像</option>'];
    (d.items||[]).forEach(function(x){
      var full=(x.Repository&&x.Repository.indexOf("<none>")<0)
        ?x.Repository+":"+(x.Tag||"latest")
        :String(x.ID||"").replace("sha256:","");
      if(!full||full.indexOf("<none>")>=0)return;
      opts.push('<option value="'+esc(full)+'">'+esc(full)+"</option>");
    });
    opts.push('<option value="__custom__">手动输入…</option>');
    sel.innerHTML=opts.join("");
    if(cur&&cur!=="__custom__"){
      sel.value=cur;
      if(sel.value!==cur)addImageOption(sel,cur);
    }else if(cur==="__custom__"){
      sel.value="__custom__";
    }
    syncImageCustom();
  }).catch(function(){});
}
function loadDeployNetworks(){
  fetchJSON(apiUrl("networks")).then(function(d){
    var sel=$("dep-network");
    if(!sel)return;
    var cur=sel.value;
    var opts=['<option value="">默认（bridge）</option>'];
    (d.items||[]).forEach(function(x){
      if(!x.Name)return;
      opts.push('<option value="'+esc(x.Name)+'">'+esc(x.Name)+"</option>");
    });
    sel.innerHTML=opts.join("");
    if(cur)sel.value=cur;
  }).catch(function(){});
}
$("dep-image").addEventListener("change",syncImageCustom);
$("btn-add-port").addEventListener("click",function(){
  var row=document.createElement("div");
  row.className="form-row";
  row.innerHTML='<input type="number" placeholder="主机端口" class="p-host">'+
    '<span class="arrow">→</span>'+
    '<input type="number" placeholder="容器端口" class="p-cont">'+
    '<select class="p-proto"><option>tcp</option><option>udp</option></select>'+
    '<button class="btn btn-ghost btn-sm" onclick="this.parentElement.remove()">✕</button>';
  $("dep-ports").appendChild(row);
});
$("btn-add-volume").addEventListener("click",function(){
  var row=document.createElement("div");
  row.className="form-row";
  row.innerHTML='<input type="text" placeholder="主机路径 /host/path" class="v-host">'+
    '<span class="arrow">→</span>'+
    '<input type="text" placeholder="容器路径 /cont/path" class="v-cont">'+
    '<button class="btn btn-ghost btn-sm" onclick="this.parentElement.remove()">✕</button>';
  $("dep-volumes").appendChild(row);
});
$("btn-add-env").addEventListener("click",function(){
  var row=document.createElement("div");
  row.className="form-row";
  row.innerHTML='<input type="text" placeholder="变量名" class="e-key">'+
    '<input type="text" placeholder="值" class="e-val">'+
    '<button class="btn btn-ghost btn-sm" onclick="this.parentElement.remove()">✕</button>';
  $("dep-env").appendChild(row);
});

$("btn-deploy-run").addEventListener("click",function(){
  var image=($("dep-image").value||"");
  if(image==="__custom__")image=($("dep-image-custom").value||"").trim();
  image=image.trim();
  if(!image){toast("请选择或输入镜像");return;}

  var ports=[],portErr=false,msg="";
  $("dep-ports").querySelectorAll(".form-row").forEach(function(r){
    var h=r.querySelector(".p-host").value;
    var c=r.querySelector(".p-cont").value;
    var p=r.querySelector(".p-proto").value;
    var hi=parseInt(h),ci=parseInt(c);if(h&&c&&hi>=1&&hi<=65535&&ci>=1&&ci<=65535)ports.push({host:hi,container:ci,protocol:p});else if(h||c){portErr=true;msg="端口须为 1-65535 整数";}
  });
  if(portErr){toast(msg);return;}
  var volumes=[];
  $("dep-volumes").querySelectorAll(".form-row").forEach(function(r){
    var h=r.querySelector(".v-host").value.trim();
    var c=r.querySelector(".v-cont").value.trim();
    if(h&&c)volumes.push({host:h,container:c});
  });
  var env=[];
  $("dep-env").querySelectorAll(".form-row").forEach(function(r){
    var k=r.querySelector(".e-key").value.trim();
    var v=r.querySelector(".e-val").value;
    if(k)env.push({key:k,value:v});
  });

  var payload={
    image:image,
    name:($("dep-name").value||"").trim(),
    restart:$("dep-restart").value,
    network:($("dep-network").value||"").trim(),
    ports:ports,
    volumes:volumes,
    env:env,
    command:($("dep-cmd").value||"").trim(),
    extra:($("dep-extra").value||"").trim()
  };

  toast("部署中…",8000);
  $("btn-deploy-run").disabled=true;
  fetchJSON(apiUrl("deploy/run"),{
    method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify(payload)
  }).then(function(d){
    $("btn-deploy-run").disabled=false;
    if(d.ok){
      toast("部署成功！容器 ID: "+(d.container_id||"").slice(0,12));
      // switch to containers tab
      document.querySelector('.tab[data-view="containers"]').click();
    }else{
      toast("部署失败: "+(d.error||"未知错误"));
    }
  }).catch(function(){
    $("btn-deploy-run").disabled=false;
    toast("请求失败");
  });
});

$("btn-deploy-compose").addEventListener("click",function(){
  var name=($("compose-name").value||"").trim();
  var yaml=$("compose-yaml").value;
  if(!yaml.trim()){toast("请填写 docker-compose.yml 内容");return;}
  toast("部署 Compose 中…",15000);
  $("btn-deploy-compose").disabled=true;
  fetchJSON(apiUrl("deploy/compose"),{
    method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({name:name,yaml:yaml})
  }).then(function(d){
    $("btn-deploy-compose").disabled=false;
    if(d.ok){
      toast("Compose 部署成功！项目: "+(d.project||""));
      loadComposeList();
      document.querySelector('.tab[data-view="containers"]').click();
    }else{
      toast("部署失败: "+(d.error||"").slice(0,100));
    }
  }).catch(function(){
    $("btn-deploy-compose").disabled=false;
    toast("请求失败");
  });
});

// ==================== COMPOSE LIST ====================
function loadComposeList(){
  var box = $("compose-list");
  if(!box) return;
  fetchJSON(apiUrl("compose")).then(function(d){
    var items = d.items || [];
    if(!items.length){ box.innerHTML = '<div class="empty">暂无 Compose 项目</div>'; return; }
    box.innerHTML = items.map(function(x){
      var svc = (x.services || []).length;
      return '<div class="c-card" data-proj="'+esc(x.name)+'">'+
        '<div class="row1"><span class="cname">'+esc(x.name)+"</span></div>"+
        '<div class="meta"><span class="chip">'+svc+" 个服务</span></div>"+
        '<div class="actions">'+
        '<button class="btn btn-sm btn-warn" data-act="down">停止</button>'+
        '<button class="btn btn-sm btn-danger" data-act="rmproj">删除</button>'+
        "</div></div>";
    }).join("");
    box.querySelectorAll(".c-card").forEach(function(card){
      card.querySelectorAll("button").forEach(function(btn){
        btn.addEventListener("click", function(){
          var proj = card.getAttribute("data-proj");
          var act = btn.getAttribute("data-act");
          if(act === "down"){
            if(!confirm("停止 Compose 项目 "+proj+"？")) return;
            fetchJSON(apiUrl("compose/"+encodeURIComponent(proj)+"/down"),{method:"POST"}).then(function(r){
              if(r.ok){ toast("已停止"); loadComposeList(); loadContainers(); loadOverview(); }
              else toast("失败: "+(r.error||""));
            }).catch(function(){ toast("请求失败"); });
          } else {
            if(!confirm("删除项目 "+proj+" 及其所有容器和文件？此操作不可恢复。")) return;
            fetchJSON(apiUrl("compose/"+encodeURIComponent(proj)+"/remove"),{method:"POST"}).then(function(r){
              if(r.ok){ toast("已删除"); loadComposeList(); loadContainers(); loadOverview(); }
              else toast("失败: "+(r.error||""));
            }).catch(function(){ toast("请求失败"); });
          }
        });
      });
    });
  }).catch(function(){ box.innerHTML = '<div class="empty">加载失败</div>'; });
}

// hook compose list into deploy tab
document.querySelectorAll(".tab").forEach(function(btn){
  btn.addEventListener("click", function(){
    if(btn.getAttribute("data-view") === "deploy") loadComposeList();
  });
});

// ==================== VOLUMES ====================
var allVolumes=[];
function loadVolumes(){
  fetchJSON(apiUrl("volumes")).then(function(d){
    allVolumes=d.items||[];
    renderVolumes();
  }).catch(function(){
    $("volume-list").innerHTML='<div class="empty"><div class="icon">⚠️</div>加载失败</div>';
  });
}
function renderVolumes(){
  var kw=($("search-vol").value||"").toLowerCase();
  var items=allVolumes.filter(function(x){
    if(!kw)return true;
    return(x.Name||"").toLowerCase().indexOf(kw)>=0;
  });
  var box=$("volume-list");
  if(!items.length){box.innerHTML='<div class="empty"><div class="icon">📁</div>暂无存储卷</div>';return;}
  box.innerHTML=items.map(function(x){
    return '<div class="c-card" data-name="'+esc(x.Name)+'">'+
      '<div class="row1"><span class="cname">'+esc(x.Name)+"</span></div>"+
      '<div class="meta"><span class="chip">'+esc(x.Driver||"local")+"</span></div>"+
      '<div class="actions">'+
      '<button class="btn btn-sm btn-danger" data-act="rmvol">删除</button>'+
      "</div></div>";
  }).join("");
  box.querySelectorAll(".c-card").forEach(function(card){
    card.querySelectorAll("button").forEach(function(btn){
      btn.addEventListener("click",function(){
        var name=card.getAttribute("data-name");
        if(!confirm("确认删除存储卷 "+name+"？数据将丢失。"))return;
        fetchJSON(apiUrl("volumes/"+encodeURIComponent(name)+"/remove"),{
          method:"POST",headers:{"Content-Type":"application/json"},body:"{}"
        }).then(function(d){
          if(d.ok){toast("已删除");loadVolumes();loadOverview();}
          else toast("删除失败: "+(d.error||""));
        }).catch(function(){toast("请求失败");});
      });
    });
  });
}

$("btn-create-vol").addEventListener("click",function(){
  openModal("创建存储卷",
    '<label>存储卷名<input type="text" id="vol-name" placeholder="如 my-data"></label>',
    '<button class="btn btn-ghost" onclick="document.getElementById(\'modal\').classList.remove(\'open\')">取消</button>'+
    '<button class="btn btn-primary" id="btn-vol-go">创建</button>'
  );
  setTimeout(function(){
    var go=$("btn-vol-go");
    if(go)go.addEventListener("click",function(){
      var name=($("vol-name").value||"").trim();
      if(!name){toast("请输入存储卷名");return;}
      closeModal();
      fetchJSON(apiUrl("volumes"),{
        method:"POST",headers:{"Content-Type":"application/json"},
        body:JSON.stringify({name:name})
      }).then(function(d){
        if(d.ok){toast("已创建");loadVolumes();loadOverview();}
        else toast("创建失败: "+(d.error||""));
      }).catch(function(){toast("请求失败");});
    });
  },50);
});

// ==================== NETWORKS ====================
var allNetworks=[];
function loadNetworks(){
  fetchJSON(apiUrl("networks")).then(function(d){
    allNetworks=d.items||[];
    renderNetworks();
  }).catch(function(){
    $("network-list").innerHTML='<div class="empty"><div class="icon">⚠️</div>加载失败</div>';
  });
}
function renderNetworks(){
  var kw=($("search-net").value||"").toLowerCase();
  var items=allNetworks.filter(function(x){
    if(!kw)return true;
    return(x.Name||"").toLowerCase().indexOf(kw)>=0||
      (x.Driver||"").toLowerCase().indexOf(kw)>=0;
  });
  var box=$("network-list");
  if(!items.length){box.innerHTML='<div class="empty"><div class="icon">🌐</div>暂无网络</div>';return;}
  box.innerHTML=items.map(function(x){
    return '<div class="c-card" data-id="'+esc(x.ID)+'" data-name="'+esc(x.Name)+'">'+
      '<div class="row1"><span class="cname">'+esc(x.Name)+"</span></div>"+
      '<div class="cimg">'+esc(x.ID||"").slice(0,12)+"</div>"+
      '<div class="meta">'+
      '<span class="chip">'+esc(x.Driver||"")+"</span>"+
      '<span class="chip">'+esc(x.Scope||"")+"</span>"+
      "</div>"+
      '<div class="actions">'+
      (x.Name!=="bridge"&&x.Name!=="host"&&x.Name!=="none"?
        '<button class="btn btn-sm btn-danger" data-act="rmnet">删除</button>':"")+
      "</div></div>";
  }).join("");
  box.querySelectorAll(".c-card").forEach(function(card){
    card.querySelectorAll("button").forEach(function(btn){
      btn.addEventListener("click",function(){
        var id=card.getAttribute("data-id");
        var name=card.getAttribute("data-name");
        if(!confirm("确认删除网络 "+name+"？"))return;
        fetchJSON(apiUrl("networks/"+encodeURIComponent(id)+"/remove"),{
          method:"POST",headers:{"Content-Type":"application/json"},body:"{}"
        }).then(function(d){
          if(d.ok){toast("已删除");loadNetworks();loadOverview();}
          else toast("删除失败: "+(d.error||""));
        }).catch(function(){toast("请求失败");});
      });
    });
  });
}

$("btn-create-net").addEventListener("click",function(){
  openModal("创建网络",
    '<label>网络名<input type="text" id="net-name" placeholder="如 my-net"></label>'+
    '<label>驱动类型<select id="net-driver">'+
    '<option value="bridge">bridge</option>'+
    '<option value="macvlan">macvlan</option>'+
    '<option value="overlay">overlay</option>'+
    "</select></label>"+
    '<label>父接口（macvlan 必填）<input type="text" id="net-parent" placeholder="如 eth0"></label>'+
    '<label>子网（可选）<input type="text" id="net-subnet" placeholder="如 192.168.1.0/24"></label>'+
    '<label>网关（可选）<input type="text" id="net-gateway" placeholder="如 192.168.1.1"></label>',
    '<button class="btn btn-ghost" onclick="document.getElementById(\'modal\').classList.remove(\'open\')">取消</button>'+
    '<button class="btn btn-primary" id="btn-net-go">创建</button>'
  );
  setTimeout(function(){
    var go=$("btn-net-go");
    if(go)go.addEventListener("click",function(){
      var name=($("net-name").value||"").trim();
      var driver=$("net-driver").value;
      var parent=($("net-parent").value||"").trim();
      var subnet=($("net-subnet").value||"").trim();
      var gateway=($("net-gateway").value||"").trim();
      if(!name){toast("请输入网络名");return;}
      if(driver==="macvlan"&&!parent){toast("macvlan 需要填写父接口（如 eth0）");return;}
      closeModal();
      fetchJSON(apiUrl("networks"),{
        method:"POST",headers:{"Content-Type":"application/json"},
        body:JSON.stringify({name:name,driver:driver,parent:parent,subnet:subnet,gateway:gateway})
      }).then(function(d){
        if(d.ok){toast("已创建");loadNetworks();loadOverview();}
        else toast("创建失败: "+(d.error||""));
      }).catch(function(){toast("请求失败");});
    });
  },50);
});

// ==================== SEARCH / REFRESH ====================
$("search-box").addEventListener("input",renderContainers);
$("search-img").addEventListener("input",renderImages);
$("image-sort").addEventListener("change",renderImages);
$("search-vol").addEventListener("input",renderVolumes);
$("search-net").addEventListener("input",renderNetworks);
$("chk-all").addEventListener("change",loadContainers);
$("btn-refresh").addEventListener("click",function(){
  loadOverview();loadContainers();loadImages();loadVolumes();loadNetworks();
  toast("已刷新");
});

// ==================== INIT ====================
// 恢复拉取任务状态（页面刷新/切回后仍能看到进度）
fetchJSON(apiUrl("images/pull/status")).then(function(d){
  if(d.task){
    renderPullStatus();
    if(d.task.status==="running")startPullWatch();
  }
}).catch(function(){});

fetchJSON(apiUrl("info")).then(function(d){
  var v=d.docker&&(d.docker.Server&&d.docker.Server.Version);
  $("docker-ver").textContent=v?"Docker "+v:"Docker 已连接";
}).catch(function(){
  $("docker-ver").textContent="后端未连接";
});

loadOverview();
loadContainers();

// running stat card -> containers view filtered to running
var cardRunning = document.getElementById("card-running");
if(cardRunning) cardRunning.addEventListener("click",function(){
  document.querySelector('.tab[data-view="containers"]').click();
  var chk = document.getElementById("chk-all");
  if(chk) { chk.checked = false; loadContainers(); }
});

})();

