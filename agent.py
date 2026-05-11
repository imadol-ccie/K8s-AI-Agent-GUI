import sys
import os

def _ensure_venv():
    base = os.path.dirname(os.path.abspath(__file__))
    # Windows path first, then Unix fallback
    venv_python = os.path.join(base, "venv", "Scripts", "python.exe")
    if not os.path.exists(venv_python):
        venv_python = os.path.join(base, "venv", "bin", "python")
    if os.path.exists(venv_python) and os.path.abspath(sys.executable) != os.path.abspath(venv_python):
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

def apply_yaml(yaml_content: str):
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(yaml_content)
        temp_file = f.name
    cmd = ["kubectl", "apply", "-f", temp_file]
    result = subprocess.run(cmd, capture_output=True, text=True)
    os.unlink(temp_file)
    return result.stdout if result.returncode == 0 else result.stderr

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


tools = [create_deployment, create_service]

# Prompt
prompt = ChatPromptTemplate.from_messages([
    ("system", "You are a helpful assistant that creates Kubernetes deployments and services. Before creating any deployment or service, first ask the user whether to deploy to the 'default' namespace or a different one. If they pick a different namespace, ask them for its exact name. The chosen namespace will be created automatically if it does not already exist. Never assume a namespace without asking."),
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


if __name__ == "__main__":
    print("🤖 Kubernetes AI Agent Initialized")

    chat_history = []

    while True:
        try:
            user_input = input("\n💡 What should I do? (or 'exit'): ").strip()
            if user_input.lower() in ["exit", "quit"]:
                break

            result = agent_executor.invoke({
                "input": user_input,
                "chat_history": chat_history,
            })

            output_text = extract_output(result.get("output", ""))
            print("\nAgent Output:\n", output_text)

            # Append this turn to history so the agent remembers it next round
            chat_history.append({"role": "human", "content": user_input})
            chat_history.append({"role": "assistant", "content": output_text})

        except Exception as e:
            print(f"❌ Error: {e}")
