"""Read-only CLI monitor mounted by LangGraph's http.app."""
import base64
import json
import os
import stat
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from starlette.applications import Starlette
from starlette.responses import Response, JSONResponse
from starlette.routing import Route
from orchestrator.runtime.cli_logs import log_directory

PAGE = r'''<!doctype html>
<html lang="vi"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>CLI Logs</title><style>
:root{color-scheme:dark;font-family:system-ui,sans-serif;background:#0c111b;color:#e1e7f0}
*{box-sizing:border-box}body{margin:0}header{padding:17px 22px;border-bottom:1px solid #293244;display:flex;justify-content:space-between;align-items:center}
.title{font-size:19px;font-weight:700}.hint{font-size:12px;color:#93a1b7}.layout{display:flex;height:calc(100vh - 67px)}
aside{width:320px;flex-shrink:0;border-right:1px solid #293244;padding:12px;overflow:auto}main{min-width:0;flex:1;display:flex;flex-direction:column}
button{border:0;background:none;color:inherit;cursor:pointer}.file{display:block;width:100%;text-align:left;padding:12px;border-radius:9px;margin:3px 0;overflow:hidden}
.task-title{display:block;line-height:1.4;overflow-wrap:anywhere}.session-meta{display:block;font-size:11px;color:#93a1b7;margin-top:6px}.running{color:#48c98a}.failed,.timed_out{color:#ff7b72}
.file:hover{background:#1c2839}.file.active{background:#243b59;color:#a8d2ff}.toolbar{display:flex;gap:16px;align-items:center;flex-wrap:wrap;padding:12px 18px;border-bottom:1px solid #293244;font-size:13px}
.toolbar strong{margin-right:auto;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:100%}
pre{margin:0;padding:20px;background:#080d15;flex:1;overflow:auto;font:13px/1.5 ui-monospace,SFMono-Regular,Consolas,monospace;white-space:pre-wrap;overflow-wrap:anywhere}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:#48c98a;margin-right:7px}.muted{color:#91a0b5}
@media(max-width:650px){aside{width:130px;padding:6px}.toolbar{gap:9px;padding:10px}pre{padding:10px;font-size:12px}}
</style></head><body>
<header><div><div class="title">CLI · Terminal Monitor</div><div class="hint">Xem tiến độ trực tiếp · Chỉ đọc</div></div><span class="hint"><span class="dot"></span>Cập nhật trực tiếp</span></header>
<div class="layout"><aside><div class="hint" style="padding:10px">NHIỆM VỤ</div><div id="files"></div></aside>
<main><div class="toolbar"><strong id="name">Chờ nhiệm vụ bắt đầu…</strong><label><input type="checkbox" id="follow" checked> Auto-scroll</label>
<label><input type="checkbox" id="wrap" checked> Wrap</label>
<button id="clear" title="Clear the visible buffer">Clear view</button></div><pre id="output"></pre></main></div>
<script>
const files=document.getElementById('files'), output=document.getElementById('output');
const nameEl=document.getElementById('name'), follow=document.getElementById('follow');
let selected=null,offset=0,fileId=null,decoder=new TextDecoder(),busy=false,generation=0,autoSelect=true,filesBusy=false;
const MAX_LINES=2500, MAX_LINE_LENGTH=10000;
const palette=['#30363d','#f85149','#3fb950','#d29922','#58a6ff','#bc8cff','#39c5cf','#c9d1d9',
               '#6e7681','#ff7b72','#7ee787','#e3b341','#79c0ff','#d2a8ff','#56d4dd','#ffffff'];
let lines=[],column=0,style={},escapeSeq='',osc=false,oscEsc=false;
function newLine(){const node=document.createElement('div');node.style.minHeight='1.5em';output.appendChild(node);
const line={cells:[],node};lines.push(line);column=0;
if(lines.length>MAX_LINES){lines.shift().node.remove();}}
function resetRenderer(){output.replaceChildren();lines=[];column=0;style={};escapeSeq='';osc=false;oscEsc=false;newLine();}
function reset(){offset=0;fileId=null;decoder=new TextDecoder();resetRenderer();}
function choose(file,manual=false){if(manual)autoSelect=false;selected=file;generation++;reset();renderFiles();poll();}
let known=[];
const statusLabels={running:'Đang chạy',completed:'Đã xong',failed:'Lỗi',timed_out:'Quá thời gian',system:'Nhật ký hệ thống',history:'Lịch sử'};
function label(f){return (f.role?f.role+' · ':'')+f.title;}
function details(f){const date=new Date(f.started_at);return [statusLabels[f.status]||'Lịch sử',f.task_id,Number.isNaN(date.getTime())?'':date.toLocaleString()].filter(Boolean).join(' · ');}
function renderFiles(){files.replaceChildren();if(!known.length){files.textContent='Chờ nhiệm vụ bắt đầu…';return;}
for(const f of known){const b=document.createElement('button');b.className='file'+(f.name===selected?' active':'');
const title=document.createElement('span');title.className='task-title';title.textContent=label(f);
const meta=document.createElement('span');meta.className='session-meta '+f.status;meta.textContent=details(f);
b.append(title,meta);b.title=label(f)+'\n'+details(f);b.onclick=()=>choose(f.name,true);files.append(b);
if(f.name===selected){nameEl.textContent=label(f)+' · '+(statusLabels[f.status]||'Lịch sử');nameEl.title=f.name;}}}
async function refreshFiles(){if(filesBusy)return;filesBusy=true;
try{const r=await fetch('/terminals/api/files',{cache:'no-store'});if(!r.ok)throw Error(r.status);const data=await r.json();known=data.sessions||data.files.map(name=>({name,title:name,status:'history'}));
if(selected&&!known.some(f=>f.name===selected)){selected=null;generation++;reset();nameEl.textContent='Chờ nhiệm vụ bắt đầu…';}
if(known.length&&(!selected||(autoSelect&&known[0].status==='running'&&selected!==known[0].name)))choose(known[0].name);
renderFiles();}catch(e){nameEl.textContent='Không tải được nhiệm vụ: '+e.message;}finally{filesBusy=false;}}
function color(code){return palette[code<8?code:code-90+8];}
function sgr(params){if(!params.length)params=[0];
for(let i=0;i<params.length;i++){const p=params[i];
if(p===0)style={};else if(p===1)style.bold=true;else if(p===22)style.bold=false;
else if(p===4)style.underline=true;else if(p===24)style.underline=false;
else if(p===7)style.inverse=true;else if(p===27)style.inverse=false;
else if(p===39)delete style.fg;else if(p===49)delete style.bg;
else if(p>=30&&p<=37)style.fg=color(p-30);else if(p>=90&&p<=97)style.fg=color(p);
else if(p>=40&&p<=47)style.bg=color(p-40);else if(p>=100&&p<=107)style.bg=color(p-10);
else if((p===38||p===48)&&params[i+1]===5&&i+2<params.length){const n=Math.max(0,Math.min(255,params[i+2]));
const cube=n>=16&&n<232?[(n-16)/36|0,((n-16)%36)/6|0,(n-16)%6]:null;
const c=n<16?palette[n]:n>=232?`rgb(${8+(n-232)*10},${8+(n-232)*10},${8+(n-232)*10})`:
`rgb(${cube.map(v=>v===0?0:v*40+55).join(',')})`;style[p===38?'fg':'bg']=c;i+=2;}
else if((p===38||p===48)&&params[i+1]===2&&i+4<params.length){style[p===38?'fg':'bg']=`rgb(${params.slice(i+2,i+5).map(v=>Math.max(0,Math.min(255,v))).join(',')})`;i+=4;}
}}
function handleCSI(seq){const cmd=seq.slice(-1), args=seq.slice(2,-1).replace(/^[?=]/,'');
const params=args===''?[]:args.split(';').map(v=>Number(v)||0);
if(cmd==='m')sgr(params);
else if(cmd==='K'){const row=lines[lines.length-1];const mode=params[0]||0;
if(mode===2){row.cells=[];column=0;}else if(mode===0)row.cells.length=Math.min(column,row.cells.length);
else if(mode===1)for(let i=0;i<Math.min(column+1,row.cells.length);i++)row.cells[i]={ch:' ',style:{}};}
else if(cmd==='G')column=Math.max(0,(params[0]||1)-1);
else if(cmd==='C')column=Math.min(MAX_LINE_LENGTH,column+(params[0]||1));
else if(cmd==='D')column=Math.max(0,column-(params[0]||1));
else if(cmd==='J'&&params[0]===2)resetRenderer();}
function writeChar(ch){const row=lines[lines.length-1];if(column>=MAX_LINE_LENGTH)return;
while(row.cells.length<column)row.cells.push({ch:' ',style:{}});
row.cells[column]={ch,style:{...style}};column++;}
function paint(row){const fragment=document.createDocumentFragment();let text='',key='',old=null;
function flush(){if(!text)return;const span=document.createElement('span');span.textContent=text;
if(old){if(old.fg)span.style.color=old.fg;if(old.bg)span.style.backgroundColor=old.bg;
if(old.bold)span.style.fontWeight='bold';if(old.underline)span.style.textDecoration='underline';
if(old.inverse){span.style.color=old.bg||'#080d15';span.style.backgroundColor=old.fg||'#c9d1d9';}}
fragment.append(span);text='';}
for(const cell of row.cells){const k=JSON.stringify(cell.style);if(k!==key){flush();key=k;old=cell.style;}text+=cell.ch;}flush();row.node.replaceChildren(fragment);}
function appendText(text){if(!text)return;const nearBottom=output.scrollTop+output.clientHeight>=output.scrollHeight-45;
const dirty=new Set();for(const ch of text){
if(osc){if(ch==='\x07'||(oscEsc&&ch==='\\')){osc=false;oscEsc=false;}else oscEsc=ch==='\x1b';continue;}
if(escapeSeq){escapeSeq+=ch;
if(escapeSeq==='\x1b]'){osc=true;escapeSeq='';continue;}
if(escapeSeq.length===2&&ch!=='['){escapeSeq='';continue;}
if(escapeSeq.length>128){escapeSeq='';continue;}
if(/[\x40-\x7e]/.test(ch)&&escapeSeq.length>=3){handleCSI(escapeSeq);escapeSeq='';dirty.add(lines[lines.length-1]);}continue;}
if(ch==='\x1b'){escapeSeq=ch;continue;}
if(ch==='\n'){newLine();continue;}
if(ch==='\r'){column=0;continue;}
if(ch==='\b'){column=Math.max(0,column-1);continue;}
if(ch==='\t'){for(let i=0,n=8-column%8;i<n;i++)writeChar(' ');dirty.add(lines[lines.length-1]);continue;}
if(ch<' ')continue;writeChar(ch);dirty.add(lines[lines.length-1]);}
for(const row of dirty)if(row.node.isConnected)paint(row);
if(follow.checked||nearBottom)output.scrollTop=output.scrollHeight;}
async function poll(){if(!selected||busy)return;busy=true;const current=selected,version=generation;
try{const url='/terminals/api/read?name='+encodeURIComponent(current)+'&offset='+offset+(fileId?'&file_id='+encodeURIComponent(fileId):'');
const r=await fetch(url,{cache:'no-store'});if(!r.ok)throw Error('HTTP '+r.status);const data=await r.json();if(generation!==version)return;
if(data.reset){reset();fileId=data.file_id;}else if(!fileId)fileId=data.file_id;
const binary=atob(data.data);const bytes=Uint8Array.from(binary,c=>c.charCodeAt(0));
appendText(decoder.decode(bytes,{stream:true}));offset=data.offset;
}catch(e){if(generation===version)nameEl.textContent='Không đọc được tiến độ · '+e.message;}finally{busy=false;}}
document.getElementById('wrap').onchange=e=>output.style.whiteSpace=e.target.checked?'pre-wrap':'pre';
document.getElementById('clear').onclick=resetRenderer;
resetRenderer();refreshFiles();setInterval(refreshFiles,500);setInterval(poll,500);
</script></body></html>'''

class MonitorReader:
    log_dir: Path
    allowed_suffixes = {".log", ".txt"}
    max_chunk = 64 * 1024
    initial_tail = 128 * 1024

    def respond(self, payload, status=200, content_type="application/json; charset=utf-8"):
        if isinstance(payload, str):
            return Response(payload, status_code=status, media_type="text/html",
                            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})
        return JSONResponse(payload, status_code=status, headers={"Cache-Control": "no-store"})

    def logs(self):
        return sorted(p.name for p in self.log_dir.iterdir()
                      if p.is_file() and not p.is_symlink() and p.suffix.lower() in self.allowed_suffixes)

    def session(self, filename):
        path = self.log_dir / filename
        started = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()
        info = {}
        try:
            fd = os.open(self.log_dir / (filename + '.json'),
                         os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
            with os.fdopen(fd, 'rb') as stream:
                st = os.fstat(stream.fileno())
                if stat.S_ISREG(st.st_mode) and st.st_size <= 16384:
                    value = json.load(stream)
                    if isinstance(value, dict):
                        info = {key: value[key][:256] for key in
                                ('title', 'role', 'task_id', 'status', 'started_at', 'ended_at')
                                if isinstance(value.get(key), str)}
        except (OSError, ValueError):
            pass
        system = filename == 'orchestrator.log'
        return {'title': 'Tiến độ toàn bộ hệ thống' if system else 'Phiên CLI trước đây',
                'role': '', 'task_id': '', 'started_at': started,
                'status': 'system' if system else 'history', **info, 'name': filename}

    def read(self, url):
        parsed = urlsplit(url)
        if parsed.path in ("/terminals", "/terminals/"):
            return self.respond(PAGE, content_type="text/html; charset=utf-8")
        if parsed.path == "/terminals/api/files":
            filenames = self.logs()
            sessions = []
            for filename in filenames:
                try:
                    sessions.append(self.session(filename))
                except OSError:  # A log may be removed while listing it.
                    continue
            sessions.sort(key=lambda s: (s['status'] == 'running', s['started_at']), reverse=True)
            return self.respond({"files": filenames, "sessions": sessions})
        if parsed.path != "/terminals/api/read":
            return self.respond({"error": "Not found"}, 404)

        query = parse_qs(parsed.query)
        filename = query.get("name", [""])[0]
        # Accept only the exact basename of an allowed, non-symlink file.
        if filename not in self.logs():
            return self.respond({"error": "Unknown log file"}, 404)
        try:
            offset = int(query.get("offset", ["0"])[0])
            if offset < 0:
                raise ValueError()
        except ValueError:
            return self.respond({"error": "Invalid offset"}, 400)

        path = self.log_dir / filename
        try:
            # O_NOFOLLOW protects against a symlink swap after directory listing.
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
            with os.fdopen(fd, "rb") as stream:
                st = os.fstat(stream.fileno())
                if not stat.S_ISREG(st.st_mode):
                    return self.respond({"error": "Not a regular file"}, 400)
                identity = f"{st.st_dev}:{st.st_ino}"
                old_id = query.get("file_id", [None])[0]
                reset = (old_id is not None and old_id != identity) or offset > st.st_size
                if reset:
                    offset = 0
                if offset == 0 and st.st_size > self.initial_tail:
                    offset = st.st_size - self.initial_tail
                stream.seek(offset)
                chunk = stream.read(self.max_chunk)
                next_offset = offset + len(chunk)
        except (OSError, ValueError):
            return self.respond({"error": "Cannot read file"}, 404)
        return self.respond({"data": base64.b64encode(chunk).decode("ascii"), "offset": next_offset,
                      "file_id": identity, "reset": reset})


def terminal(request):
    reader = MonitorReader()
    reader.log_dir = log_directory()
    reader.log_dir.mkdir(parents=True, exist_ok=True)
    return reader.read(str(request.url))

app = Starlette(routes=[Route(path, terminal) for path in
    ("/terminals", "/terminals/", "/terminals/api/files", "/terminals/api/read")])
