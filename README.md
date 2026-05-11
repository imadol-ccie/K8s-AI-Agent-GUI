# K8s AI Agent

A conversational AI agent that manages Kubernetes resources using natural language. Powered by Google Gemini and LangChain, it creates Deployments and Services via `kubectl` and saves the generated manifests to disk.

## Features

- Create Kubernetes **Deployments** by describing what you want in plain English
- Create Kubernetes **Services** (ClusterIP, NodePort, LoadBalancer)
- **Prompts for the target namespace** before each create, and auto-creates non-`default` namespaces on the fly
- Automatically saves generated YAML manifests to the `k8s/` directory
- Remembers conversation context across turns within a session
- Auto-activates the project virtual environment on startup

## Project Structure

```
K8s-AI-Agent/
├── agent.py          # Main AI agent
├── k8s/              # Generated YAML manifests (auto-created)
│   ├── <name>-deployment.yaml
│   ├── <name>-service.yaml
│   └── <namespace>-namespace.yaml
├── requirements.txt
└── venv/             # Python virtual environment
```

## Requirements

- Python 3.12+
- `kubectl` installed and configured (pointing to a running cluster)
- A Google Gemini API key

## Setup

1. **Clone the repository**

   ```bash
   git clone <repo-url>
   cd K8s-AI-Agent
   ```

2. **Create and activate the virtual environment**

   ```bash
   # Windows
   python -m venv venv

   # macOS / Linux
   python3 -m venv venv
   ```

3. **Install dependencies**

   ```bash
   # Windows
   venv\Scripts\pip install -r requirements.txt

   # macOS / Linux
   venv/bin/pip install -r requirements.txt
   ```

4. **Set your Gemini API key**

   Create a `.env` file in the project root (loaded automatically via `python-dotenv`):

   ```
   GOOGLE_API_KEY=your-key-here
   ```

   Or export it as an environment variable:

   ```bash
   export GOOGLE_API_KEY=your-key-here   # macOS / Linux
   $env:GOOGLE_API_KEY="your-key-here"   # Windows PowerShell
   ```

## Usage

```bash
python agent.py
```

The script auto-activates the venv if it is not already active.

### Example prompts

```
💡 What should I do? create a deployment named web-app using nginx image with 3 replicas
🤖 Would you like to deploy this to the 'default' namespace or a different one?
💡 What should I do? use the staging namespace
💡 What should I do? create a service for web-app on port 80 in staging
💡 What should I do? exit
```

The agent always asks which namespace to use before creating a Deployment or Service. If you pick a namespace other than `default` and it doesn't exist yet, it is created automatically (the Namespace YAML is also saved under `k8s/`).

### Tool input formats

**create_deployment**
```
name: <name>, image: <image>, replicas: <n>, namespace: <namespace>
```

**create_service**
```
name: <name>, port: <port>, type: ClusterIP|NodePort|LoadBalancer, namespace: <namespace>
```

`namespace` defaults to `default` if omitted.

## Generated Manifests

Every time the agent creates a resource, the YAML is saved under `k8s/`:

```
k8s/web-app-deployment.yaml
k8s/web-app-service.yaml
```

You can re-apply them at any time:

```bash
kubectl apply -f k8s/
```

## Dependencies

| Package | Purpose |
|---|---|
| `langchain` | Agent orchestration |
| `langchain-google-genai` | Gemini LLM integration |
| `langchain-core` | Core LangChain primitives |
| `pyyaml` | YAML generation |
| `google-generativeai` | Google AI SDK |
| `pydantic` | Data validation |
| `python-dotenv` | Loads `GOOGLE_API_KEY` from `.env` |

## Contributors
- balkanbgboy