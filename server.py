import sys
import os
import platform
import subprocess


def _ensure_venv():
    """Same bootstrap as agent.py — see that file for full docstring."""
    base = os.path.dirname(os.path.abspath(__file__))
    is_windows = platform.system() == "Windows"
    py_subpath = ("Scripts", "python.exe") if is_windows else ("bin", "python")

    default_venv = os.path.join(base, "venv")
    default_python = os.path.join(default_venv, *py_subpath)
    if os.path.exists(default_python):
        venv_dir, venv_python = default_venv, default_python
    else:
        venv_dir = os.path.join(base, f"venv-{platform.system().lower()}")
        venv_python = os.path.join(venv_dir, *py_subpath)

    requirements = os.path.join(base, "requirements.txt")
    marker = os.path.join(venv_dir, ".requirements-installed")

    if os.path.exists(venv_python) and os.path.abspath(sys.executable) == os.path.abspath(venv_python):
        return

    if not os.path.isdir(venv_dir):
        print(f"[setup] creating venv at {venv_dir} ...", flush=True)
        subprocess.check_call([sys.executable, "-m", "venv", venv_dir])

    if os.path.exists(requirements):
        req_mtime = os.path.getmtime(requirements)
        marker_mtime = os.path.getmtime(marker) if os.path.exists(marker) else 0
        if req_mtime > marker_mtime:
            print("[setup] installing requirements — this can take 1-3 minutes on first run...", flush=True)
            subprocess.check_call([venv_python, "-m", "pip", "install", "--upgrade", "pip"])
            subprocess.check_call([venv_python, "-m", "pip", "install", "-r", requirements])
            with open(marker, "w") as f:
                f.write(str(req_mtime))
            print("[setup] done.", flush=True)

    if os.path.abspath(sys.executable) != os.path.abspath(venv_python):
        os.execv(venv_python, [venv_python] + sys.argv)


_ensure_venv()

import json
import subprocess
import uuid
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from agent import run_agent, set_active_context, get_active_context

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
K8S_DIR = os.path.join(BASE_DIR, "k8s")
STATIC_DIR = os.path.join(BASE_DIR, "static")

app = FastAPI(title="K8s AI Agent")

# In-memory chat history per session (personal use only — process-lifetime)
SESSIONS: dict[str, list] = {}


class ChatRequest(BaseModel):
    message: str
    session_id: Optional[str] = None


class ChatResponse(BaseModel):
    session_id: str
    reply: str


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest):
    session_id = req.session_id or str(uuid.uuid4())
    history = SESSIONS.setdefault(session_id, [])
    try:
        reply = run_agent(req.message, history)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return ChatResponse(session_id=session_id, reply=reply)


@app.post("/chat/reset")
def chat_reset(session_id: str):
    SESSIONS.pop(session_id, None)
    return {"ok": True}


@app.get("/manifests")
def list_manifests():
    if not os.path.isdir(K8S_DIR):
        return {"files": []}
    files = []
    for name in sorted(os.listdir(K8S_DIR)):
        full = os.path.join(K8S_DIR, name)
        if os.path.isfile(full) and name.endswith((".yaml", ".yml")):
            files.append({
                "name": name,
                "size": os.path.getsize(full),
                "mtime": os.path.getmtime(full),
            })
    return {"files": files}


@app.get("/manifests/{name}")
def get_manifest(name: str):
    if "/" in name or "\\" in name or name.startswith("."):
        raise HTTPException(status_code=400, detail="Invalid filename")
    path = os.path.join(K8S_DIR, name)
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="Not found")
    with open(path, "r") as f:
        return {"name": name, "content": f.read()}


@app.delete("/manifests/{name}")
def delete_manifest(name: str):
    """First `kubectl delete -f <file>`, then remove the local YAML file."""
    if "/" in name or "\\" in name or name.startswith("."):
        raise HTTPException(status_code=400, detail="Invalid filename")
    path = os.path.join(K8S_DIR, name)
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="Not found")

    # Step 1: kubectl delete -f <file> --ignore-not-found
    result = _kubectl_run(["delete", "-f", path, "--ignore-not-found=true"], timeout=30)
    if result.returncode != 0:
        raise HTTPException(
            status_code=500,
            detail=f"kubectl delete failed (file not removed): {_clean_kubectl_error(result.stderr)}",
        )
    kubectl_output = result.stdout.strip() or "nothing to delete on cluster"

    # Step 2: remove the local file
    try:
        os.unlink(path)
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"file removal failed: {e}")

    return {"name": name, "kubectl_output": kubectl_output, "file_deleted": True}


def _clean_kubectl_error(stderr: str) -> str:
    """Distill kubectl's noisy multi-line stderr into a single friendly message."""
    if not stderr:
        return "kubectl failed"
    text = stderr.strip()
    if "Unable to connect to the server" in text or "connection refused" in text.lower() or "connectex" in text.lower():
        return "no cluster reachable — is Docker Desktop / minikube / kind running?"
    if "the server has asked for the client to provide credentials" in text:
        return "kubectl: not authenticated"
    if "context" in text.lower() and "does not exist" in text.lower():
        return "kubectl: current context is invalid"
    # Fallback: keep only the last non-empty, non-debug line
    lines = [ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("E0")]
    return lines[-1] if lines else text.splitlines()[-1]


def _kubectl_base() -> list[str]:
    cmd = ["kubectl"]
    ctx = get_active_context()
    if ctx:
        cmd += ["--context", ctx]
    return cmd


def _kubectl_run(args: list[str], timeout: int = 15) -> subprocess.CompletedProcess:
    cmd = _kubectl_base() + args
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        raise HTTPException(status_code=500, detail="kubectl not found in PATH")
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="kubectl timed out")


def _kubectl_json(args: list[str]):
    result = _kubectl_run(args + ["-o", "json"])
    if result.returncode != 0:
        raise HTTPException(status_code=500, detail=_clean_kubectl_error(result.stderr))
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=500, detail=f"kubectl returned invalid JSON: {e}")


@app.get("/cluster/contexts")
def list_contexts():
    # Read from kubeconfig directly — doesn't require an active cluster connection
    cmd = ["kubectl", "config", "get-contexts", "-o", "name"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    except FileNotFoundError:
        raise HTTPException(status_code=500, detail="kubectl not found in PATH")
    if result.returncode != 0:
        raise HTTPException(status_code=500, detail=result.stderr.strip() or "failed to list contexts")
    names = [n for n in result.stdout.splitlines() if n.strip()]
    # Determine the kubeconfig's current-context as a sensible default
    cur = subprocess.run(["kubectl", "config", "current-context"], capture_output=True, text=True, timeout=5)
    kubeconfig_current = cur.stdout.strip() if cur.returncode == 0 else None
    return {
        "contexts": names,
        "active": get_active_context() or kubeconfig_current,
        "kubeconfig_current": kubeconfig_current,
    }


class SetContextRequest(BaseModel):
    name: Optional[str] = None  # None / empty → fall back to kubeconfig's current-context


@app.post("/cluster/context")
def set_context(req: SetContextRequest):
    set_active_context(req.name)
    return {"active": get_active_context()}


@app.get("/cluster/namespaces")
def list_namespaces():
    data = _kubectl_json(["get", "namespaces"])
    return {"items": [{"name": i["metadata"]["name"], "status": i["status"]["phase"]} for i in data.get("items", [])]}


def _ns_args(namespace: str) -> list[str]:
    """Translate the UI's namespace selector into kubectl flags.
    The special sentinel '__all__' means '-A' (all namespaces).
    """
    if namespace == "__all__":
        return ["-A"]
    return ["-n", namespace]


def _age(timestamp: str) -> str:
    """Format a K8s creationTimestamp like '2024-05-11T20:00:00Z' as '5s' / '12m' / '3h' / '2d'."""
    if not timestamp:
        return ""
    from datetime import datetime, timezone
    try:
        created = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        seconds = int((datetime.now(timezone.utc) - created).total_seconds())
    except Exception:
        return ""
    if seconds < 60:
        return f"{max(seconds, 0)}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def _meta(i: dict) -> tuple[str, str, str]:
    """Return (name, namespace, age) for a list-item from kubectl JSON."""
    m = i.get("metadata", {})
    return m.get("name", ""), m.get("namespace", ""), _age(m.get("creationTimestamp", ""))


@app.get("/cluster/pods")
def list_pods(namespace: str = "default"):
    data = _kubectl_json(["get", "pods", *_ns_args(namespace)])
    items = []
    for i in data.get("items", []):
        status = i.get("status", {})
        container_statuses = status.get("containerStatuses", [])
        ready = sum(1 for c in container_statuses if c.get("ready"))
        total = len(container_statuses)
        restarts = sum(c.get("restartCount", 0) for c in container_statuses)
        items.append({
            "name": i["metadata"]["name"],
            "namespace": i["metadata"].get("namespace", ""),
            "ready": f"{ready}/{total}" if total else "0/0",
            "status": status.get("phase", "Unknown"),
            "restarts": restarts,
        })
    return {"items": items}


@app.get("/cluster/deployments")
def list_deployments(namespace: str = "default"):
    data = _kubectl_json(["get", "deployments", *_ns_args(namespace)])
    items = []
    for i in data.get("items", []):
        spec = i.get("spec", {})
        status = i.get("status", {})
        items.append({
            "name": i["metadata"]["name"],
            "namespace": i["metadata"].get("namespace", ""),
            "replicas": spec.get("replicas", 0),
            "ready": status.get("readyReplicas", 0) or 0,
            "available": status.get("availableReplicas", 0) or 0,
        })
    return {"items": items}


@app.get("/cluster/services")
def list_services(namespace: str = "default"):
    data = _kubectl_json(["get", "services", *_ns_args(namespace)])
    items = []
    for i in data.get("items", []):
        spec = i.get("spec", {})
        ports = ",".join(f"{p.get('port')}/{p.get('protocol', 'TCP')}" for p in spec.get("ports", []))
        items.append({
            "name": i["metadata"]["name"],
            "namespace": i["metadata"].get("namespace", ""),
            "type": spec.get("type", ""),
            "cluster_ip": spec.get("clusterIP", ""),
            "ports": ports,
        })
    return {"items": items}


@app.get("/cluster/configmaps")
def list_configmaps(namespace: str = "default"):
    data = _kubectl_json(["get", "configmaps", *_ns_args(namespace)])
    items = []
    for i in data.get("items", []):
        name, ns, age = _meta(i)
        items.append({
            "name": name, "namespace": ns,
            "keys": len((i.get("data") or {}).keys()),
            "age": age,
        })
    return {"items": items}


@app.get("/cluster/secrets")
def list_secrets(namespace: str = "default"):
    data = _kubectl_json(["get", "secrets", *_ns_args(namespace)])
    items = []
    for i in data.get("items", []):
        name, ns, age = _meta(i)
        items.append({
            "name": name, "namespace": ns,
            "type": i.get("type", ""),
            "keys": len((i.get("data") or {}).keys()),
            "age": age,
        })
    return {"items": items}


@app.get("/cluster/ingresses")
def list_ingresses(namespace: str = "default"):
    data = _kubectl_json(["get", "ingresses", *_ns_args(namespace)])
    items = []
    for i in data.get("items", []):
        name, ns, age = _meta(i)
        spec = i.get("spec", {})
        status = i.get("status", {})
        hosts = ",".join(r.get("host", "*") for r in spec.get("rules", []) if r.get("host"))
        lb = status.get("loadBalancer", {}).get("ingress", [])
        address = ",".join(x.get("ip") or x.get("hostname", "") for x in lb)
        items.append({
            "name": name, "namespace": ns,
            "class": spec.get("ingressClassName", ""),
            "hosts": hosts or "-",
            "address": address or "-",
            "age": age,
        })
    return {"items": items}


@app.get("/cluster/pvcs")
def list_pvcs(namespace: str = "default"):
    data = _kubectl_json(["get", "persistentvolumeclaims", *_ns_args(namespace)])
    items = []
    for i in data.get("items", []):
        name, ns, age = _meta(i)
        spec = i.get("spec", {})
        status = i.get("status", {})
        capacity = (status.get("capacity") or {}).get("storage", "-")
        items.append({
            "name": name, "namespace": ns,
            "status": status.get("phase", "Unknown"),
            "volume": spec.get("volumeName", "-"),
            "capacity": capacity,
            "storage_class": spec.get("storageClassName", "-"),
            "age": age,
        })
    return {"items": items}


@app.get("/cluster/hpas")
def list_hpas(namespace: str = "default"):
    data = _kubectl_json(["get", "horizontalpodautoscalers", *_ns_args(namespace)])
    items = []
    for i in data.get("items", []):
        name, ns, age = _meta(i)
        spec = i.get("spec", {})
        status = i.get("status", {})
        ref = spec.get("scaleTargetRef", {})
        items.append({
            "name": name, "namespace": ns,
            "reference": f"{ref.get('kind', '')}/{ref.get('name', '')}",
            "min": spec.get("minReplicas", 0),
            "max": spec.get("maxReplicas", 0),
            "current": status.get("currentReplicas", 0) or 0,
            "age": age,
        })
    return {"items": items}


@app.get("/cluster/jobs")
def list_jobs(namespace: str = "default"):
    data = _kubectl_json(["get", "jobs", *_ns_args(namespace)])
    items = []
    for i in data.get("items", []):
        name, ns, age = _meta(i)
        spec = i.get("spec", {})
        status = i.get("status", {})
        items.append({
            "name": name, "namespace": ns,
            "completions": f"{status.get('succeeded', 0) or 0}/{spec.get('completions', 1)}",
            "active": status.get("active", 0) or 0,
            "failed": status.get("failed", 0) or 0,
            "age": age,
        })
    return {"items": items}


@app.get("/cluster/cronjobs")
def list_cronjobs(namespace: str = "default"):
    data = _kubectl_json(["get", "cronjobs", *_ns_args(namespace)])
    items = []
    for i in data.get("items", []):
        name, ns, age = _meta(i)
        spec = i.get("spec", {})
        status = i.get("status", {})
        last = status.get("lastScheduleTime", "")
        items.append({
            "name": name, "namespace": ns,
            "schedule": spec.get("schedule", ""),
            "suspend": "yes" if spec.get("suspend") else "no",
            "active": len(status.get("active", []) or []),
            "last_schedule": _age(last) if last else "-",
            "age": age,
        })
    return {"items": items}


# Serve static frontend
if os.path.isdir(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
def root():
    index = os.path.join(STATIC_DIR, "index.html")
    if not os.path.isfile(index):
        return JSONResponse({"error": "static/index.html missing"}, status_code=500)
    return FileResponse(index)


def _open_browser_when_ready(url: str = "http://127.0.0.1:8000", delay: float = 1.5):
    """Open the dashboard in the user's default browser shortly after startup.
    Works on Windows, macOS, native Linux, and WSL. Skip with NO_BROWSER=1.
    """
    if os.environ.get("NO_BROWSER"):
        return
    import threading

    def _open():
        try:
            release = platform.uname().release.lower()
            if "microsoft" in release or "wsl" in release:
                # WSL: launch the Windows default browser via cmd.exe
                subprocess.Popen(
                    ["cmd.exe", "/c", "start", "", url],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
            else:
                import webbrowser
                webbrowser.open(url)
        except Exception as e:
            print(f"[browser] could not auto-open {url}: {e}", flush=True)

    threading.Timer(delay, _open).start()


if __name__ == "__main__":
    import uvicorn
    _open_browser_when_ready()
    uvicorn.run("server:app", host="127.0.0.1", port=8000, reload=False)
