#!/usr/bin/env python3
"""Local Runner: run any GGUF model on any hardware.

Models are memory-mapped, so the OS pages weights in from disk as needed.
If a model is bigger than your RAM it still runs (slowly) as long as it fits on disk.
"""
import atexit, gc, json, os, socket, sys, tempfile, uuid, platform, re, shutil, subprocess, threading, time, webbrowser
from pathlib import Path
from typing import Optional, Union

import httpx
import psutil
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from huggingface_hub import HfApi
from pydantic import BaseModel

HERE = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
MODELS = Path(os.environ.get("LOCALRUNNER_MODELS", Path.home() / "LocalRunner" / "models"))
MODELS.mkdir(parents=True, exist_ok=True)
HF_TOKEN = os.environ.get("HF_TOKEN")
SPLIT = re.compile(r"-(\d{5})-of-(\d{5})\.gguf$")

app = FastAPI()
_origins = [o for o in os.environ.get("LOCALRUNNER_CORS", "").split(",") if o]
if _origins:
    app.add_middleware(CORSMiddleware, allow_origins=_origins, allow_methods=["*"], allow_headers=["*"])
api = HfApi(token=HF_TOKEN)
lock = threading.Lock()
jobs = {}


server = {"on": True}
logs = []


@app.middleware("http")
async def api_gate(request, call_next):
    if not request.url.path.startswith("/v1"):
        return await call_next(request)
    if not server["on"]:
        return JSONResponse({"error": "The server is stopped."}, status_code=503)
    resp = await call_next(request)
    logs.append(f"{time.strftime('%H:%M:%S')}  {request.method} {request.url.path}  {resp.status_code}")
    del logs[:-200]
    return resp


class ServerReq(BaseModel):
    on: bool


@app.get("/api/server")
def get_server():
    return {"on": server["on"], "logs": logs[-60:]}


@app.post("/api/server")
def set_server(r: ServerReq):
    server["on"] = r.on
    return {"on": r.on}


# ---------- hardware ----------
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def engine_path():
    """Find llama-server (llama.cpp): bundled engine folder, LOCALRUNNER_ENGINE, or PATH."""
    exe = "llama-server.exe" if os.name == "nt" else "llama-server"
    for d in (os.environ.get("LOCALRUNNER_ENGINE"), HERE / "engine", Path.cwd() / "engine"):
        if d and (Path(d) / exe).exists():
            return Path(d) / exe
    w = shutil.which("llama-server")
    return Path(w) if w else None


def gpu_memory():
    if platform.system() == "Darwin" and platform.machine() == "arm64":
        return int(psutil.virtual_memory().total * 0.7)  # unified memory, Metal limit
    exe = engine_path()
    if exe:  # works for NVIDIA, AMD and Intel GPUs
        try:
            r = subprocess.run([str(exe), "--list-devices"], capture_output=True, text=True, timeout=20,
                               cwd=str(exe.parent), creationflags=NO_WINDOW)
            sizes = [int(m) for m in re.findall(r"\((\d+) MiB,", r.stdout + r.stderr)]
            if sizes:
                return max(sizes) * 1024 ** 2
        except Exception:
            pass
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=5, creationflags=NO_WINDOW).stdout
        return int(max(float(x) for x in out.split()) * 1024 ** 2)
    except Exception:
        return 0


RAM = psutil.virtual_memory().total
VRAM = gpu_memory()


def fit(size, cap=None):
    cap = VRAM + remote_free() if cap is None else cap  # local GPU memory plus any helper computers
    if cap and size < cap * 0.85:
        return "gpu"
    if size < RAM * 0.7:
        return "ram"
    return "disk"


# ---------- local library ----------
def group(p):
    m = SPLIT.search(p.name)
    if not m:
        return [p]
    prefix, total = p.name[:m.start()], m.group(2)
    return sorted(q for q in p.parent.iterdir()
                  if q.name.startswith(prefix + "-") and q.name.endswith(f"-of-{total}.gguf"))


def safe_path(rel):
    p = (MODELS / rel).resolve()
    if MODELS.resolve() not in p.parents or p.suffix != ".gguf":
        raise HTTPException(400, "Invalid model path")
    return p


@app.get("/api/specs")
def specs():
    return {"ram": RAM, "vram": VRAM, "disk_free": shutil.disk_usage(MODELS).free,
            "cores": psutil.cpu_count(logical=False) or psutil.cpu_count(),
            "cluster_vram": remote_free(), "uninstaller": uninstaller() is not None, "cluster_nodes": len(online_workers()),
            "engine": engine_path() is not None, "dir": str(MODELS),
            "loaded": [{"file": k, "name": Path(k).stem, "mode": v["mode"], "remote": len(v.get("rpc") or [])} for k, v in loaded.items()]}


@app.get("/api/models")
def models():
    out = []
    for p in sorted(MODELS.rglob("*.gguf")):
        m = SPLIT.search(p.name)
        if m and m.group(1) != "00001":
            continue
        size = sum(q.stat().st_size for q in group(p))
        out.append({"file": p.relative_to(MODELS).as_posix(), "name": p.stem, "size": size, "fit": fit(size), "mtime": p.stat().st_mtime})
    return out


def prune(p):
    """Remove folders left empty after a model is deleted (never the models folder itself)."""
    d = p.parent
    while d != MODELS and MODELS in d.parents:
        try:
            d.rmdir()
        except OSError:
            break
        d = d.parent


@app.delete("/api/models")
def delete_model(file: str):
    p = safe_path(file)
    drop(file)
    try:
        for q in group(p):
            q.unlink(missing_ok=True)
            Path(str(q) + ".part").unlink(missing_ok=True)
    except PermissionError:
        raise HTTPException(409, "That file is in use. Eject the model and try again.")
    prune(p)
    return {"ok": True}


def partial_files():
    return [q for q in MODELS.rglob("*.part") if q.is_file()]


@app.get("/api/partials")
def get_partials():
    f = partial_files()
    return {"files": len(f), "size": sum(q.stat().st_size for q in f),
            "active": any(j_["status"] == "running" for j_ in jobs.values())}


@app.delete("/api/partials")
def clear_partials():
    if any(j_["status"] == "running" for j_ in jobs.values()):
        raise HTTPException(409, "A download is still running. Wait for it to finish first.")
    for q in partial_files():
        q.unlink(missing_ok=True)
        prune(q)
    return {"ok": True}


class ConfirmReq(BaseModel):
    confirm: bool


def uninstaller():
    exe = Path(sys.executable).parent / "unins000.exe"
    return exe if getattr(sys, "frozen", False) and exe.exists() else None


@app.post("/api/uninstall")
def uninstall(r: ConfirmReq):
    exe = uninstaller()
    if not (r.confirm and exe):
        raise HTTPException(400, "Uninstall from the Start menu or Windows Settings > Apps instead.")
    subprocess.Popen([str(exe)], close_fds=True, creationflags=0x00000008 | 0x00000200)  # detached, so it outlives this app

    def bye():
        time.sleep(1)
        for k in list(loaded):
            drop(k)
        worker_stop()
        os._exit(0)
    threading.Thread(target=bye, daemon=True).start()
    return {"ok": True}


# ---------- discover & download ----------
@app.get("/api/search")
def search(q: str):
    res = api.list_models(search=q, filter="gguf", sort="downloads", limit=20)
    return [{"id": m.id, "downloads": m.downloads or 0} for m in res]


@app.get("/api/repo")
def repo(id: str):
    info = api.model_info(id, files_metadata=True)
    groups = {}
    for s in info.siblings:
        if not s.rfilename.endswith(".gguf") or "mmproj" in s.rfilename:
            continue
        key = SPLIT.sub(".gguf", s.rfilename)
        g = groups.setdefault(key, {"name": Path(key).stem, "size": 0, "parts": []})
        g["size"] += s.size or 0
        g["parts"].append(s.rfilename)
    out = []
    for g in groups.values():
        g["parts"].sort()
        g["fit"] = fit(g["size"])
        out.append(g)
    return sorted(out, key=lambda g: g["size"])


class DownloadReq(BaseModel):
    repo: str
    parts: list[str]
    size: int


def run_download(job, repo_id, parts):
    headers = {"Authorization": f"Bearer {HF_TOKEN}"} if HF_TOKEN else {}
    try:
        with httpx.Client(follow_redirects=True, timeout=None) as c:
            for f in parts:
                dest = MODELS / repo_id.replace("/", "__") / f
                dest.parent.mkdir(parents=True, exist_ok=True)
                if dest.exists():
                    job["done"] += dest.stat().st_size
                    continue
                part = dest.with_name(dest.name + ".part")
                have = part.stat().st_size if part.exists() else 0
                h = dict(headers, **({"Range": f"bytes={have}-"} if have else {}))
                with c.stream("GET", f"https://huggingface.co/{repo_id}/resolve/main/{f}", headers=h) as r:
                    if r.status_code == 416:  # already complete
                        part.rename(dest)
                        job["done"] += dest.stat().st_size
                        continue
                    r.raise_for_status()
                    if r.status_code != 206:
                        have = 0
                    job["done"] += have
                    with open(part, "ab" if have else "wb") as fh:
                        for chunk in r.iter_bytes(1 << 20):
                            fh.write(chunk)
                            job["done"] += len(chunk)
                part.rename(dest)
        job["status"] = "done"
    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)


@app.post("/api/download")
def download(r: DownloadReq):
    if any(".." in f or not f.endswith(".gguf") for f in r.parts):
        raise HTTPException(400, "Invalid file name")
    if r.size > shutil.disk_usage(MODELS).free:
        raise HTTPException(400, "Not enough free disk space for this model.")
    key = f"{r.repo}/{r.parts[0]}"
    if jobs.get(key, {}).get("status") == "running":
        return {"ok": True}
    jobs[key] = {"name": Path(r.parts[0]).stem, "done": 0, "total": r.size, "status": "running"}
    threading.Thread(target=run_download, args=(jobs[key], r.repo, r.parts), daemon=True).start()
    return {"ok": True}


@app.get("/api/downloads")
def downloads():
    return list(jobs.values())


# ---------- load, presets, chat ----------
loaded = {}  # file -> {llm, mode, size, gpu, t, lock}
MAX_LOADED = int(os.environ.get("LOCALRUNNER_MAX_LOADED", 2))
PRESETS = MODELS.parent / "presets.json"
DEFAULT_PRESETS = [
    {"name": "Default", "system": "You are a helpful assistant.", "temperature": 0.7, "top_p": 0.95, "max_tokens": 1024, "repeat_penalty": 1.1},
    {"name": "Concise", "system": "Answer in as few words as possible.", "temperature": 0.4, "top_p": 0.9, "max_tokens": 512, "repeat_penalty": 1.1},
    {"name": "Coder", "system": "You are an expert programmer. Reply with correct, minimal code and brief notes.", "temperature": 0.2, "top_p": 0.9, "max_tokens": 2048, "repeat_penalty": 1.05},
    {"name": "Coding agent", "system": "Read files before changing them. Make small, careful edits and run tests when they exist.", "temperature": 0.2, "top_p": 0.9, "max_tokens": 2048, "repeat_penalty": 1.05},
    {"name": "Creative writer", "system": "You are a vivid, imaginative writer.", "temperature": 1.0, "top_p": 0.98, "max_tokens": 2048, "repeat_penalty": 1.1},
]


def free_port():
    with socket.socket() as sk:
        sk.bind(("127.0.0.1", 0))
        return sk.getsockname()[1]


def drop(file):
    e = loaded.pop(file, None)
    if e:
        e["proc"].terminate()


atexit.register(lambda: [drop(k) for k in list(loaded)])


# ---------- cluster: run one model across several computers (llama.cpp RPC) ----------
# A "worker" computer runs ggml-rpc-server and lends its GPU/RAM. The "main" computer starts
# llama-server with --rpc so the model's layers are split across all of them.
CLUSTER = MODELS.parent / "cluster.json"
RPC_PORT, DISC_PORT, MAGIC = 50052, 50054, b"LOCALRUNNER?"
cluster = {"use": True, "worker": False, "port": RPC_PORT, "workers": []}  # workers: [{addr, name}]
wstatus = {}  # addr -> {online, total, free, error}  (cached: a busy worker can't be probed)
wstate = {"proc": None, "stop": None, "error": ""}
cl_lock = threading.Lock()
ADDR = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*(:\d{1,5})?$")


def save_cluster():
    try:
        CLUSTER.write_text(json.dumps(cluster, indent=1))
    except OSError:
        pass


def lan_ips():
    out = []
    for nic in psutil.net_if_addrs().values():
        for a in nic:
            if a.family == socket.AF_INET and not a.address.startswith(("127.", "169.254.")):
                out.append((a.address, a.broadcast))
    return out


def online_workers():
    if not cluster["use"]:
        return []
    return [w["addr"] for w in cluster["workers"] if wstatus.get(w["addr"], {}).get("online")]


def remote_free():
    return sum(wstatus[a].get("free", 0) for a in online_workers())


def cluster_busy():
    return any(m.get("rpc") for m in loaded.values())


def drop_remote():
    with lock:
        for k in [k for k, m in loaded.items() if m.get("rpc")]:
            drop(k)


def rpc_exe():
    exe = engine_path()
    if not exe:
        return None
    for n in ("ggml-rpc-server", "rpc-server"):  # the binary was renamed in newer llama.cpp builds
        p = exe.parent / (n + (".exe" if os.name == "nt" else ""))
        if p.exists():
            return p
    return None


def probe(addr):
    """Ask the engine to list the devices on a worker. This also tells us how much memory it has."""
    exe = engine_path()
    if not exe:
        return {"online": False, "error": "Engine not found"}
    try:
        r = subprocess.run([str(exe), "--rpc", addr, "--list-devices"], capture_output=True, text=True,
                           timeout=25, cwd=str(exe.parent), creationflags=NO_WINDOW)
        out = r.stdout + r.stderr
    except subprocess.TimeoutExpired:
        return {"online": False, "error": "No answer. It may be off, blocked by a firewall, or busy with another computer."}
    except OSError as ex:
        return {"online": False, "error": str(ex)}
    devs = [(int(m[1]), int(m[2])) for m in re.finditer(r"^\s*RPC\d+:\s*.*?\((\d+) MiB, (\d+) MiB free\)", out, re.M)]
    if devs:
        return {"online": True, "total": sum(d[0] for d in devs) * 1024 ** 2, "free": sum(d[1] for d in devs) * 1024 ** 2}
    host, _, port = addr.rpartition(":")
    try:
        socket.create_connection((host, int(port)), 4).close()
        return {"online": True, "total": 0, "free": 0, "error": "Connected, but memory size is unknown"}
    except OSError:
        return {"online": False, "error": "Cannot connect. Is Local Runner sharing on that computer, and is the firewall open?"}


def probe_all():
    if cluster_busy():  # a worker serves one computer at a time, so keep the last known numbers
        return
    ts = [threading.Thread(target=lambda a=w["addr"]: wstatus.__setitem__(a, probe(a))) for w in cluster["workers"]]
    [t.start() for t in ts]
    [t.join() for t in ts]


# --- this computer as a worker ---
def disc_listen(stop):
    """Answer 'who is out there?' broadcasts from other Local Runner computers."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind(("", DISC_PORT))
    except OSError:
        return
    s.settimeout(1)
    while not stop.is_set():
        try:
            data, addr = s.recvfrom(256)
        except socket.timeout:
            continue
        except OSError:
            break
        if data == MAGIC:
            s.sendto(json.dumps({"app": "localrunner", "name": platform.node(), "port": cluster["port"],
                                 "ram": RAM, "vram": VRAM}).encode(), addr)
    s.close()


def worker_running():
    p = wstate["proc"]
    return p is not None and p.poll() is None


def worker_stop():
    if wstate["stop"]:
        wstate["stop"].set()
    p = wstate["proc"]
    if p and p.poll() is None:
        p.terminate()
    wstate["proc"] = wstate["stop"] = None


def worker_start(port):
    exe = rpc_exe()
    if not exe:
        raise HTTPException(500, "This build of the engine has no RPC server (ggml-rpc-server). Reinstall Local Runner.")
    worker_stop()
    log = tempfile.TemporaryFile()
    proc = subprocess.Popen([str(exe), "-H", "0.0.0.0", "-p", str(port), "-c"], stdout=log, stderr=subprocess.STDOUT,
                            cwd=str(exe.parent), creationflags=NO_WINDOW)
    time.sleep(1.5)
    if proc.poll() is not None:
        log.seek(0)
        msg = log.read().decode(errors="replace")[-300:]
        wstate["error"] = msg
        raise HTTPException(500, f"Could not start sharing (is port {port} already in use?): {msg}")
    wstate.update(proc=proc, stop=threading.Event(), error="")
    threading.Thread(target=disc_listen, args=(wstate["stop"],), daemon=True).start()


atexit.register(worker_stop)


def scan(targets=None, wait=2.5):
    """Broadcast on the local network and collect replies from computers that are sharing."""
    own = {ip for ip, _ in lan_ips()}
    if targets is None:
        targets = {"255.255.255.255"} | {b for _, b in lan_ips() if b}
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    s.bind(("", 0))
    s.settimeout(0.3)
    for t in targets:
        try:
            s.sendto(MAGIC, (t, DISC_PORT))
        except OSError:
            pass
    found, end = {}, time.time() + wait
    while time.time() < end:
        try:
            data, (ip, _) = s.recvfrom(1024)
            d = json.loads(data)
        except socket.timeout:
            continue
        except (OSError, ValueError):
            continue
        if d.get("app") == "localrunner" and ip not in own:
            found[ip] = {"addr": f"{ip}:{d.get('port', RPC_PORT)}", "name": d.get("name", ""), "ram": d.get("ram", 0), "vram": d.get("vram", 0)}
    s.close()
    return list(found.values())


def cluster_boot():
    try:
        cluster.update(json.loads(CLUSTER.read_text()))
    except (OSError, ValueError):
        pass
    if cluster["worker"]:
        try:
            worker_start(cluster["port"])
        except Exception as ex:
            wstate["error"] = str(getattr(ex, "detail", ex))
    threading.Thread(target=probe_all, daemon=True).start()


def worker_info():
    return {"on": worker_running(), "port": cluster["port"], "available": rpc_exe() is not None, "error": wstate["error"]}


@app.get("/api/cluster")
def get_cluster():
    return {"use": cluster["use"], "busy": cluster_busy(), "ips": [ip for ip, _ in lan_ips()], "worker": worker_info(),
            "workers": [{**w, **wstatus.get(w["addr"], {})} for w in cluster["workers"]], "free": remote_free()}


class UseReq(BaseModel):
    on: bool


@app.post("/api/cluster/use")
def cluster_use(r: UseReq):
    cluster["use"] = r.on
    save_cluster()
    drop_remote()
    return {"use": r.on}


class WorkerOnReq(BaseModel):
    on: bool
    port: int = 0


@app.post("/api/cluster/worker")
def cluster_worker(r: WorkerOnReq):
    if r.on:
        port = r.port or cluster["port"]
        if not 1024 <= port <= 65535:
            raise HTTPException(400, "Port must be between 1024 and 65535")
        worker_start(port)
        cluster.update(worker=True, port=port)
    else:
        worker_stop()
        cluster["worker"] = False
    save_cluster()
    return worker_info()


class AddReq(BaseModel):
    addr: str
    name: str = ""


@app.post("/api/cluster/workers")
def cluster_add(r: AddReq):
    addr = r.addr.strip()
    if not ADDR.match(addr):
        raise HTTPException(400, "Enter an address like 192.168.1.20 or 192.168.1.20:50052")
    if ":" not in addr:
        addr += f":{RPC_PORT}"
    if int(addr.rsplit(":", 1)[1]) > 65535:
        raise HTTPException(400, "Invalid port")
    with cl_lock:
        if not any(w["addr"] == addr for w in cluster["workers"]):
            cluster["workers"].append({"addr": addr, "name": r.name.strip()[:60]})
            save_cluster()
    if not cluster_busy():
        wstatus[addr] = probe(addr)
    return get_cluster()


@app.delete("/api/cluster/workers")
def cluster_remove(addr: str):
    with cl_lock:
        cluster["workers"] = [w for w in cluster["workers"] if w["addr"] != addr]
        save_cluster()
    wstatus.pop(addr, None)
    drop_remote()
    return get_cluster()


@app.post("/api/cluster/scan")
def cluster_scan():
    known = {w["addr"] for w in cluster["workers"]}
    return [f for f in scan() if f["addr"] not in known]


@app.post("/api/cluster/refresh")
def cluster_refresh():
    probe_all()
    return get_cluster()


def start_engine(exe, path, ctx, ngl, mode, rpc=()):
    port = free_port()
    cmd = [str(exe), "-m", str(path), "-c", str(ctx), "-ngl", str(ngl), "-np", "1",
           "--host", "127.0.0.1", "--port", str(port), "--no-webui"]
    if rpc:  # spread the model over the helper computers as well
        cmd += ["--rpc", ",".join(rpc)]
    if mode == "disk":
        cmd.append("--no-warmup")  # don't read the whole file up front
    log = tempfile.TemporaryFile()
    proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, cwd=str(exe.parent), creationflags=NO_WINDOW)
    t0 = time.time()
    while time.time() - t0 < 900:
        if proc.poll() is not None:
            log.seek(0)
            raise RuntimeError(log.read().decode(errors="replace")[-400:])
        try:
            if httpx.get(f"http://127.0.0.1:{port}/health", timeout=2).status_code == 200:
                return proc, port
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    proc.terminate()
    raise RuntimeError("Timed out while loading the model")


def chat_stream(e, **kw):
    """Yield OpenAI-style chunks from the model's engine process."""
    with httpx.stream("POST", f"http://127.0.0.1:{e['port']}/v1/chat/completions",
                      json={**kw, "stream": True}, timeout=None) as r:
        if r.status_code != 200:
            raise RuntimeError(r.read().decode(errors="replace")[:300])
        for line in r.iter_lines():
            if line.startswith("data: ") and line != "data: [DONE]":
                yield json.loads(line[6:])


def chat_once(e, **kw):
    r = httpx.post(f"http://127.0.0.1:{e['port']}/v1/chat/completions", json=kw, timeout=None)
    return JSONResponse(r.json(), status_code=r.status_code)


def get_llm(file, n_ctx=4096):
    """Return a running model, starting its engine (and evicting the oldest ones) if needed."""
    exe = engine_path()
    if not exe:
        raise HTTPException(500, "Model engine (llama-server) not found. Reinstall the app or set LOCALRUNNER_ENGINE.")
    with lock:
        if file in loaded:
            loaded[file]["t"] = time.time()
            return loaded[file]
        path = safe_path(file)
        if not path.exists():
            raise HTTPException(404, "Model file not found")
        size = sum(q.stat().st_size for q in group(path))
        rpc = online_workers()

        def plan(cap):
            mode = fit(size, cap)
            f = min(1.0, cap * 0.8 / size) if cap else 0
            return mode, list(dict.fromkeys([999, 0] if mode == "gpu" else [int(80 * f), int(40 * f), 0] if f else [0]))

        mode, cands = plan(VRAM + remote_free() if rpc else VRAM)
        attempts = [(n, rpc, mode) for n in cands if n and rpc]
        m2, c2 = plan(VRAM)  # last resort if the cluster can't load it: this computer alone
        attempts += [(n, [], m2) for n in c2] if rpc else [(n, [], mode) for n in cands]
        for k in [k for k, m in loaded.items() if attempts[0][0] and m["gpu"]]:
            drop(k)  # GPU memory is shared: GPU models load one at a time
        while loaded and (len(loaded) >= MAX_LOADED or sum(m["size"] for m in loaded.values()) + size > RAM * 0.7):
            drop(min(loaded, key=lambda k: loaded[k]["t"]))
        err = ""
        for ngl, rp, mode in attempts:  # if the GPU runs out of memory, retry with fewer layers on it
            try:
                proc, port = start_engine(exe, path, n_ctx if mode != "disk" else min(n_ctx, 2048), ngl, mode, rp)
                break
            except RuntimeError as ex:
                err = str(ex)
        else:
            raise HTTPException(500, f"Could not load model: {err}")
        loaded[file] = {"proc": proc, "port": port, "mode": mode, "size": size, "gpu": bool(ngl), "rpc": rp,
                        "t": time.time(), "lock": threading.Lock()}
        return loaded[file]


class LoadReq(BaseModel):
    file: str
    n_ctx: int = 4096


@app.post("/api/load")
def load(r: LoadReq):
    return {"ok": True, "mode": get_llm(r.file, r.n_ctx)["mode"]}


@app.post("/api/unload")
def unload(r: LoadReq):
    drop(r.file)
    return {"ok": True}


@app.get("/api/presets")
def get_presets():
    try:
        return json.loads(PRESETS.read_text()) or DEFAULT_PRESETS
    except Exception:
        return DEFAULT_PRESETS


@app.put("/api/presets")
def put_presets(presets: list[dict]):
    PRESETS.write_text(json.dumps(presets, indent=2))
    return {"ok": True}


def sse(obj):
    return f"data: {json.dumps(obj)}\n\n"


class ChatReq(BaseModel):
    model: str
    messages: list
    system: str = ""
    n_ctx: int = 4096
    temperature: float = 0.7
    top_p: float = 0.95
    max_tokens: int = 1024
    repeat_penalty: float = 1.1


@app.post("/api/chat")
def chat(r: ChatReq):
    if not r.model:
        raise HTTPException(400, "Load a model first")
    e = get_llm(r.model, r.n_ctx)
    msgs = ([{"role": "system", "content": r.system}] if r.system else []) + r.messages

    def gen():
        start, n = time.time(), 0
        with e["lock"]:
            try:
                for ch in chat_stream(e, 
                        messages=msgs, temperature=r.temperature, top_p=r.top_p,
                        max_tokens=r.max_tokens, repeat_penalty=r.repeat_penalty, stream=True):
                    t = ch["choices"][0]["delta"].get("content")
                    if t:
                        n += 1
                        yield sse({"t": t})
                yield sse({"done": True, "tps": n / max(time.time() - start, 1e-6)})
            except Exception as ex:
                yield sse({"error": str(ex)})

    return StreamingResponse(gen(), media_type="text/event-stream")


# ---------- OpenAI-compatible API (/v1) ----------
def auth(authorization: Optional[str] = Header(None)):
    key = os.environ.get("LOCALRUNNER_API_KEY")
    if key and authorization != f"Bearer {key}":
        raise HTTPException(401, "Invalid API key")


def resolve(name):
    files = [m["file"] for m in models()]
    name = name or next(iter(loaded), "")
    for f in files:
        if name in (f, Path(f).stem):
            return f
    hit = [f for f in files if name and name.lower() in f.lower()]
    if hit:
        return hit[0]
    raise HTTPException(404, f"Model '{name}' not found")


class OAIReq(BaseModel):
    model: str = ""
    messages: list
    stream: bool = False
    temperature: float = 0.7
    top_p: float = 0.95
    max_tokens: Optional[int] = None
    stop: Optional[Union[str, list]] = None
    presence_penalty: float = 0.0
    frequency_penalty: float = 0.0
    seed: Optional[int] = None


@app.get("/v1/models", dependencies=[Depends(auth)])
def oai_models():
    return {"object": "list", "data": [{"id": m["file"], "object": "model", "owned_by": "local"} for m in models()]}


@app.post("/v1/chat/completions", dependencies=[Depends(auth)])
def oai_chat(r: OAIReq):
    e = get_llm(resolve(r.model))
    kw = r.model_dump(exclude={"model", "messages", "stream"}, exclude_none=True)

    if not r.stream:
        with e["lock"]:
            return chat_once(e, messages=r.messages, **kw)

    def gen():
        with e["lock"]:
            for ch in chat_stream(e, messages=r.messages, stream=True, **kw):
                yield sse(ch)
        yield "data: [DONE]\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


PICKS = [  # edit freely; repos are Hugging Face GGUF repositories
    {"repo": "bartowski/Llama-3.2-1B-Instruct-GGUF", "title": "Llama 3.2 1B", "desc": "Tiny and fast. Runs on almost any PC.", "gb": 1.5},
    {"repo": "bartowski/Llama-3.2-3B-Instruct-GGUF", "title": "Llama 3.2 3B", "desc": "Good everyday chat on 8 GB machines.", "gb": 3},
    {"repo": "bartowski/Qwen2.5-7B-Instruct-GGUF", "title": "Qwen 2.5 7B", "desc": "Strong all-rounder. Likes 16 GB of RAM or a GPU.", "gb": 6},
    {"repo": "bartowski/Qwen2.5-Coder-7B-Instruct-GGUF", "title": "Qwen 2.5 Coder 7B", "desc": "Best starting point for the coding agent.", "gb": 6},
]


@app.get("/api/picks")
def picks():
    budget = max(VRAM, RAM * 0.6) / 1e9
    return [dict(p, fits=p["gb"] <= budget) for p in PICKS]


# ---------- coding agent ----------
TOOLS_PROMPT = """You are a coding agent working inside the user's project folder. Solve the task step by step using tools.
To use a tool, reply with ONE fenced block and nothing after it:
```tool
{"name": "<tool>", "args": {...}}
```
Tools:
- list_dir {"path": "."}: list files in a folder
- read_file {"path": "src/app.py"}: read a file
- write_file {"path": "src/app.py", "content": "..."}: create or overwrite a file with the full content
- run_command {"command": "python app.py"}: run a shell command in the project folder
After each tool call you will receive its result. When the task is finished, reply with a short summary and no tool block."""
TOOL_RE = re.compile(r"```(?:tool|json)?\s*(\{.*?\})\s*```", re.S)
pending = {}  # tool id -> {"e": Event, "ok": bool}


def ws_path(root, rel):
    root = Path(root).resolve()
    p = (root / rel).resolve()
    if p != root and root not in p.parents:
        raise ValueError("Path is outside the project folder")
    return p


def run_tool(root, name, a):
    if name == "list_dir":
        p = ws_path(root, a.get("path", "."))
        return "\n".join(sorted(x.name + ("/" if x.is_dir() else "") for x in p.iterdir() if x.name != ".git"))[:4000]
    if name == "read_file":
        return ws_path(root, a["path"]).read_text(errors="replace")[:12000]
    if name == "write_file":
        p = ws_path(root, a["path"])
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(a["content"])
        return f"Wrote {len(a['content'])} characters to {a['path']}"
    if name == "run_command":
        r = subprocess.run(a["command"], shell=True, cwd=root, capture_output=True, text=True, timeout=60,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return f"exit code {r.returncode}\n{(r.stdout + r.stderr)[-4000:]}"
    return f"Unknown tool: {name}"


class AgentReq(ChatReq):
    workspace: str
    approval: str = "ask"  # ask | edits | all
    max_steps: int = 12


class ApproveReq(BaseModel):
    id: str
    ok: bool


@app.post("/api/agent/approve")
def approve(r: ApproveReq):
    p = pending.get(r.id)
    if p:
        p["ok"] = r.ok
        p["e"].set()
    return {"ok": True}


@app.post("/api/agent")
def agent(r: AgentReq):
    root = Path(r.workspace).expanduser()
    if not root.is_dir():
        raise HTTPException(400, "Choose a project folder first.")
    e = get_llm(r.model, r.n_ctx)
    msgs = [{"role": "system", "content": TOOLS_PROMPT + ("\n\n" + r.system if r.system else "")}] + r.messages

    def gen():
        with e["lock"]:
            for _ in range(r.max_steps):
                text = ""
                try:
                    for ch in chat_stream(e, 
                            messages=msgs, temperature=r.temperature, top_p=r.top_p,
                            max_tokens=r.max_tokens, repeat_penalty=r.repeat_penalty, stream=True):
                        t = ch["choices"][0]["delta"].get("content")
                        if t:
                            text += t
                            yield sse({"t": t})
                except Exception as ex:
                    yield sse({"error": str(ex)})
                    return
                m = TOOL_RE.search(text)
                if not m:
                    break
                msgs.append({"role": "assistant", "content": text})
                try:
                    call = json.loads(m.group(1))
                    name, args = call["name"], call.get("args", {})
                except Exception:
                    msgs.append({"role": "user", "content": "That tool block was not valid JSON. Try again."})
                    continue
                need = (name == "run_command" and r.approval != "all") or (name == "write_file" and r.approval == "ask")
                tid = uuid.uuid4().hex[:8]
                yield sse({"tool": {"id": tid, "name": name, "args": args, "needs": need}})
                ok = True
                if need:
                    pending[tid] = {"e": threading.Event(), "ok": False}
                    ok = pending[tid]["e"].wait(300) and pending[tid]["ok"]
                    pending.pop(tid, None)
                if ok:
                    try:
                        res = run_tool(root, name, args)
                    except Exception as ex:
                        res = f"Error: {ex}"
                else:
                    res = "The user denied this action."
                yield sse({"result": {"id": tid, "text": res[:3000]}})
                msgs.append({"role": "user", "content": f"Tool result:\n{res}"})
            yield sse({"done": True})

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.get("/")
def index():
    return FileResponse(HERE / "index.html")


if __name__ == "__main__":
    import socket
    import uvicorn

    cluster_boot()
    host, port = os.environ.get("LOCALRUNNER_HOST", "127.0.0.1"), int(os.environ.get("LOCALRUNNER_PORT", 1234))
    srv = threading.Thread(target=lambda: uvicorn.run(app, host=host, port=port, log_level="warning", log_config=None), daemon=True)
    srv.start()
    for _ in range(100):  # wait for the engine to come up
        try:
            socket.create_connection(("127.0.0.1", port), 0.2).close()
            break
        except OSError:
            time.sleep(0.1)
    try:
        import webview
    except ImportError:  # no desktop window library: fall back to the browser
        webbrowser.open(f"http://127.0.0.1:{port}")
        srv.join()
    else:
        class Api:
            def pick_folder(self):
                r = win.create_file_dialog(webview.FOLDER_DIALOG)
                return r[0] if r else ""

        win = webview.create_window("Local Runner", f"http://127.0.0.1:{port}", width=1320, height=860, js_api=Api())
        webview.start()
