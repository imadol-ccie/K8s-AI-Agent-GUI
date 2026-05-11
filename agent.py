import sys
import os
import platform
import subprocess


def _ensure_venv():
    """Bootstrap a per-OS venv and install requirements before importing anything else.

    Each OS gets its own venv directory so Windows and WSL/Linux can coexist
    without nuking each other on every switch:

      * `venv/`         — used if it already exists and matches the current OS
      * `venv-windows/` — used otherwise on Windows
      * `venv-linux/`   — used otherwise on Linux/WSL
      * `venv-darwin/`  — used otherwise on macOS

    First run on a given OS pays the one-time install cost; subsequent runs
    just re-exec into the existing venv and are essentially instant.
    """
    base = os.path.dirname(os.path.abspath(__file__))
    is_windows = platform.system() == "Windows"
    py_subpath = ("Scripts", "python.exe") if is_windows else ("bin", "python")

    # Prefer the legacy ./venv if it already matches our OS.
    default_venv = os.path.join(base, "venv")
    default_python = os.path.join(default_venv, *py_subpath)
    if os.path.exists(default_python):
        venv_dir, venv_python = default_venv, default_python
    else:
        venv_dir = os.path.join(base, f"venv-{platform.system().lower()}")
        venv_python = os.path.join(venv_dir, *py_subpath)

    requirements = os.path.join(base, "requirements.txt")
    marker = os.path.join(venv_dir, ".requirements-installed")

    # Already running inside the chosen venv? Nothing to do.
    if os.path.exists(venv_python) and os.path.abspath(sys.executable) == os.path.abspath(venv_python):
        return

    # Create the venv if missing (we no longer destroy any existing one).
    if not os.path.isdir(venv_dir):
        print(f"[setup] creating venv at {venv_dir} ...", flush=True)
        subprocess.check_call([sys.executable, "-m", "venv", venv_dir])

    # Install / update requirements only when requirements.txt is newer than the marker.
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

    # Re-exec under the venv python so all imports below run from the venv.
    if os.path.abspath(sys.executable) != os.path.abspath(venv_python):
        os.execv(venv_python, [venv_python] + sys.argv)


_ensure_venv()

import subprocess
import tempfile
import yaml
from dotenv import load_dotenv

load_dotenv()
from langchain_core.tools import tool, BaseTool
from langchain_classic.agents import create_tool_calling_agent, AgentExecutor
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder

# Initialize LLM
llm = ChatGoogleGenerativeAI(
    model="gemini-2.5-flash",
    google_api_key=os.getenv("GOOGLE_API_KEY"),
    temperature=0.1,
    max_tokens=2048
)

# --- Deployment Helpers ---
def generate_deployment_yaml(name: str, image: str, replicas: int = 1, namespace: str = "default", port: int = 80):
    deployment = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": name, "namespace": namespace},
        "spec": {
            "replicas": replicas,
            "selector": {"matchLabels": {"app": name}},
            "template": {
                "metadata": {"labels": {"app": name}},
                "spec": {
                    "containers": [{
                        "name": name,
                        "image": image,
                        "ports": [{"containerPort": port}]
                    }]
                }
            }
        }
    }
    return yaml.dump(deployment, default_flow_style=False)

# Active kubectl context — None means "use whatever kubectl's current-context is".
# Set by the web GUI via set_active_context() so all kubectl calls (including
# the agent's `kubectl apply`) target the cluster the user picked.
_ACTIVE_CONTEXT: str | None = None


def set_active_context(name: str | None) -> None:
    global _ACTIVE_CONTEXT
    _ACTIVE_CONTEXT = name or None


def get_active_context() -> str | None:
    return _ACTIVE_CONTEXT


def _kubectl_cmd(args: list[str]) -> list[str]:
    cmd = ["kubectl"]
    if _ACTIVE_CONTEXT:
        cmd += ["--context", _ACTIVE_CONTEXT]
    return cmd + args


def _run_kubectl_apply(yaml_content: str, dry_run: bool = False) -> dict:
    """Internal helper: run kubectl apply (optionally --dry-run=server).
    Returns {'ok': bool, 'output': str}.
    """
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(yaml_content)
        temp_file = f.name
    args = ["apply", "-f", temp_file]
    if dry_run:
        args.append("--dry-run=server")
    cmd = _kubectl_cmd(args)
    result = subprocess.run(cmd, capture_output=True, text=True)
    os.unlink(temp_file)
    return {
        "ok": result.returncode == 0,
        "output": (result.stdout if result.returncode == 0 else result.stderr).strip(),
    }


def apply_yaml(yaml_content: str):
    r = _run_kubectl_apply(yaml_content, dry_run=False)
    if r["ok"]:
        return f"SUCCESS: kubectl apply ok\n{r['output']}"
    return (
        f"FAILED: kubectl apply returned an error. "
        f"Nothing was deployed to the cluster.\nstderr: {r['output']}"
    )

# --- Namespace Helper ---
def generate_namespace_yaml(name: str):
    ns = {
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": {"name": name},
    }
    return yaml.dump(ns, default_flow_style=False)

def ensure_namespace(namespace: str) -> str:
    if namespace == "default" or not namespace:
        return ""
    yaml_content = generate_namespace_yaml(namespace)
    saved_path = save_yaml_file(yaml_content, namespace, "namespace")
    apply_result = apply_yaml(yaml_content)
    return f"Namespace manifest: {saved_path}\n{apply_result}\n"

# --- Service Helper ---
def generate_service_yaml(name: str, namespace: str = "default", port: int = 80, target_port: int = 80, service_type: str = "ClusterIP"):
    service = {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": f"{name}-svc", "namespace": namespace},
        "spec": {
            "selector": {"app": name},
            "ports": [{
                "port": port,
                "targetPort": target_port,
                "protocol": "TCP",
                "name": "http"
            }],
            "type": service_type
        }
    }
    return yaml.dump(service, default_flow_style=False)

# --- YAML File Saver ---
def save_yaml_file(yaml_content: str, name: str, kind: str):
    """Save a generated YAML manifest to the k8s/ directory."""
    output_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "k8s")
    os.makedirs(output_dir, exist_ok=True)
    filename = f"{name}-{kind.lower()}.yaml"
    filepath = os.path.join(output_dir, filename)
    with open(filepath, "w") as f:
        f.write(yaml_content)
    return filepath

# --- Tools ---

@tool
def create_deployment(tool_input: str) -> str:
    """Create a Kubernetes deployment. Always ask the user for the namespace before calling this tool; if they don't specify one, ask explicitly. If the namespace is not 'default', it will be created automatically. Input format example: name: web-app, image: httpd, replicas: 2, namespace: staging"""
    name = None
    image = None
    replicas = 1
    namespace = "default"

    tool_input = tool_input.strip().strip("{}'\"")

    try:
        parts = tool_input.split(",")
        for part in parts:
            if ":" in part:
                k, v = part.split(":", 1)
                k, v = k.strip().strip("'\""), v.strip().strip("'\"")
                if k == "name":
                    name = v
                elif k == "image":
                    image = v
                elif k == "replicas":
                    replicas = int(v)
                elif k == "namespace":
                    namespace = v
    except Exception:
        pass

    if not name or not image:
        parts = tool_input.split()
        if len(parts) >= 2:
            name = parts[0].replace("name:", "").strip()
            image = parts[1].replace("image:", "").strip()
            if len(parts) > 2:
                try:
                    replicas_val = parts[2].replace("replicas:", "").strip()
                    replicas = int(replicas_val)
                except ValueError:
                    pass

    if not name or not image:
        raise ValueError("Both 'name' and 'image' must be provided to create a deployment.")

    ns_result = ensure_namespace(namespace)

    yaml_content = generate_deployment_yaml(name, image, replicas, namespace=namespace)
    saved_path = save_yaml_file(yaml_content, name, "deployment")
    kubectl_result = apply_yaml(yaml_content)
    return f"{ns_result}{kubectl_result}\nYAML saved to: {saved_path}"


@tool
def create_service(tool_input: str) -> str:
    """Create a Kubernetes service. Always ask the user for the namespace before calling this tool; if they don't specify one, ask explicitly. If the namespace is not 'default', it will be created automatically. Input format example: name: web-app, port: 80, type: ClusterIP, namespace: staging"""
    name = None
    port = 80
    target_port = 80
    service_type = "ClusterIP"
    namespace = "default"

    tool_input = tool_input.strip().strip("{}'\"")

    try:
        parts = tool_input.split(",")
        for part in parts:
            if ":" in part:
                k, v = part.split(":", 1)
                k, v = k.strip().strip("'\""), v.strip().strip("'\"")
                if k == "name":
                    name = v
                elif k == "port":
                    port = int(v)
                elif k == "target_port":
                    target_port = int(v)
                elif k == "type":
                    service_type = v
                elif k == "namespace":
                    namespace = v
    except Exception:
        pass

    if not name:
        raise ValueError("A 'name' must be provided to create a service.")

    ns_result = ensure_namespace(namespace)

    yaml_content = generate_service_yaml(name, namespace=namespace, port=port, target_port=target_port, service_type=service_type)
    saved_path = save_yaml_file(yaml_content, name, "service")
    kubectl_result = apply_yaml(yaml_content)
    return f"{ns_result}{kubectl_result}\nYAML saved to: {saved_path}"


@tool
def apply_kubernetes_manifest(yaml_content: str) -> str:
    """Apply ANY Kubernetes resource by providing the full YAML manifest.

    Use this for any resource type other than Deployment or Service —
    ConfigMap, Secret, Ingress, PersistentVolumeClaim, HorizontalPodAutoscaler,
    Job, CronJob, StatefulSet, DaemonSet, NetworkPolicy, ServiceAccount,
    Role/RoleBinding, ResourceQuota, etc.

    The YAML is first validated against the cluster with `kubectl apply
    --dry-run=server`. Only if validation succeeds is the real apply executed.
    Malformed manifests are caught early and the cluster is never touched.

    IMPORTANT:
      - You must already have asked the user which namespace to use BEFORE
        calling this tool and embedded it in the YAML's metadata.namespace
        (unless the resource is cluster-scoped, e.g. ClusterRole).
      - For namespaced resources, include `metadata.namespace`. The namespace
        will be auto-created if it doesn't exist.
      - For Deployment or Service, prefer create_deployment / create_service
        — they have safer defaults.

    Input: a complete valid Kubernetes YAML manifest as a string. Example:
      apiVersion: v1
      kind: ConfigMap
      metadata:
        name: app-config
        namespace: default
      data:
        DB_HOST: postgres
        LOG_LEVEL: info
    """
    yaml_content = yaml_content.strip()
    # Parse the YAML to extract kind, name, namespace for file saving + namespace creation.
    try:
        doc = yaml.safe_load(yaml_content)
    except yaml.YAMLError as e:
        return f"FAILED: invalid YAML — could not parse.\nstderr: {e}"
    if not isinstance(doc, dict):
        return (
            "FAILED: expected a single Kubernetes resource (a YAML dict at top level). "
            "If you need to apply multiple resources, call this tool once per resource."
        )
    kind = doc.get("kind") or "Manifest"
    meta = doc.get("metadata") or {}
    name = meta.get("name") or "unnamed"
    namespace = meta.get("namespace")

    # Auto-create the namespace (if namespaced and not 'default')
    ns_result = ensure_namespace(namespace) if namespace else ""

    # Server-side dry-run validation
    dry = _run_kubectl_apply(yaml_content, dry_run=True)
    if not dry["ok"]:
        return (
            f"FAILED: server-side validation (--dry-run=server) rejected the YAML. "
            f"Nothing was applied.\nstderr: {dry['output']}"
        )

    # Save the manifest, then do the real apply
    saved_path = save_yaml_file(yaml_content, name, kind.lower())
    apply_result = apply_yaml(yaml_content)
    return f"{ns_result}{apply_result}\nYAML saved to: {saved_path}"


tools = [create_deployment, create_service, apply_kubernetes_manifest]

# Prompt
prompt = ChatPromptTemplate.from_messages([
    ("system",
     "You are a helpful assistant that helps the user create Kubernetes resources. "
     "Respond naturally to whatever the user says. "
     "For casual messages (greetings, small talk, questions about you), just reply briefly and "
     "naturally — do NOT mention namespaces, deployments, or push the user toward an action. "
     "\n\n"
     "TOOL SELECTION when the user asks to create a Kubernetes resource:\n"
     "  - Deployment → call create_deployment\n"
     "  - Service → call create_service\n"
     "  - Anything else (ConfigMap, Secret, Ingress, PersistentVolumeClaim, HPA, Job, CronJob, "
     "StatefulSet, DaemonSet, NetworkPolicy, ServiceAccount, RBAC roles/bindings, ResourceQuota, "
     "etc.) → generate the full YAML yourself and call apply_kubernetes_manifest with it. "
     "The YAML is validated server-side before apply, so don't worry about a small mistake "
     "wrecking the cluster — failed validation is caught and reported.\n"
     "\n"
     "FLOW for any resource creation:\n"
     "1) If the resource is namespaced and the user has not specified a namespace, ask whether "
     "to use 'default' or a different one (and if different, ask for the exact name). The "
     "chosen namespace is created automatically if it doesn't exist. Never assume a namespace "
     "without asking. Cluster-scoped resources (ClusterRole, ClusterRoleBinding, PersistentVolume, "
     "Namespace itself, StorageClass, etc.) don't need this step.\n"
     "2) Call the appropriate tool.\n"
     "3) Tool outputs include 'SUCCESS:' or 'FAILED:' lines. You MUST NOT claim that anything "
     "was created on the cluster if the tool output contains 'FAILED:'. In that case, tell the "
     "user the apply failed, show the error message verbatim, and suggest checking the kubectl "
     "context (the cluster the dashboard is pointing at).\n"
     "4) Only when every step reports SUCCESS, finish your reply by briefly asking if there's "
     "anything else they'd like to do. Keep the follow-up short."),
    MessagesPlaceholder(variable_name="chat_history"),
    ("human", "{input}"),
    MessagesPlaceholder(variable_name="agent_scratchpad"),
])

# Construct Agent
agent = create_tool_calling_agent(llm, tools, prompt)
agent_executor = AgentExecutor(agent=agent, tools=tools, verbose=False, handle_parsing_errors=True)

def extract_output(raw) -> str:
    """Normalize agent output: handle plain strings, dicts, and Gemini content-block lists with mixed dict/string parts."""
    if isinstance(raw, str):
        return raw
    if isinstance(raw, dict):
        return raw.get("text") or raw.get("content") or str(raw)
    if isinstance(raw, list):
        parts = []
        for item in raw:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text") or item.get("content")
                if text:
                    parts.append(text)
        return "".join(parts) if parts else str(raw)
    return str(raw)


def run_agent(user_input: str, chat_history: list) -> str:
    """Invoke the agent for a single turn and append to chat_history in place."""
    result = agent_executor.invoke({
        "input": user_input,
        "chat_history": chat_history,
    })
    output_text = extract_output(result.get("output", ""))
    chat_history.append({"role": "human", "content": user_input})
    chat_history.append({"role": "assistant", "content": output_text})
    return output_text


if __name__ == "__main__":
    print("🤖 Kubernetes AI Agent Initialized")

    chat_history = []

    while True:
        try:
            user_input = input("\n💡 What should I do? (or 'exit'): ").strip()
            if user_input.lower() in ["exit", "quit"]:
                break

            output_text = run_agent(user_input, chat_history)
            print("\nAgent Output:\n", output_text)

        except Exception as e:
            print(f"❌ Error: {e}")
