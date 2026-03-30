import json
import os
import subprocess
from pathlib import Path

import yaml
from flask import Flask, jsonify, render_template, request

app = Flask(__name__)


def get_kubeconfig_paths():
    kubeconfig_env = os.environ.get("KUBECONFIG", "")
    if kubeconfig_env:
        sep = ";" if os.name == "nt" else ":"
        paths = [p for p in kubeconfig_env.split(sep) if p.strip()]
        if paths:
            return paths
    default = Path.home() / ".kube" / "config"
    if default.exists():
        return [str(default)]
    return []


def get_contexts():
    contexts = []
    current_context = None
    seen = set()

    for kc_path in get_kubeconfig_paths():
        try:
            with open(kc_path, "r", encoding="utf-8") as f:
                kc = yaml.safe_load(f)
            if not kc:
                continue
            for ctx in kc.get("contexts") or []:
                name = (ctx or {}).get("name")
                if name and name not in seen:
                    contexts.append(name)
                    seen.add(name)
            if not current_context and kc.get("current-context"):
                current_context = kc["current-context"]
        except Exception:
            pass

    return contexts, current_context


@app.route("/api/contexts")
def api_contexts():
    contexts, current = get_contexts()
    return jsonify({"contexts": contexts, "current": current})


def enrich_err_images(err_images, context=None):
    """Add affectedWorkloads to errored images by querying kubectl."""
    if not err_images:
        return err_images

    base_cmd = ["kubectl"]
    if context:
        base_cmd += ["--context", context]

    try:
        pods_result = subprocess.run(
            base_cmd + ["get", "pods", "-A", "-o", "json"],
            capture_output=True, text=True, timeout=30,
        )
        if pods_result.returncode != 0:
            return err_images
        pods = json.loads(pods_result.stdout).get("items", [])

        # Resolve ReplicaSet -> Deployment in one batch call
        rs_result = subprocess.run(
            base_cmd + ["get", "replicasets", "-A", "-o", "json"],
            capture_output=True, text=True, timeout=30,
        )
        rs_map = {}  # "namespace/rs-name" -> {kind, name}
        if rs_result.returncode == 0:
            for rs in json.loads(rs_result.stdout).get("items", []):
                meta = rs.get("metadata", {})
                owners = meta.get("ownerReferences", [])
                if owners:
                    key = f"{meta.get('namespace', '')}/{meta.get('name', '')}"
                    rs_map[key] = {"kind": owners[0].get("kind"), "name": owners[0].get("name")}
    except Exception:
        return err_images

    err_image_set = {e.get("Image", "") for e in err_images}
    image_workloads: dict[str, list] = {}

    for pod in pods:
        meta = pod.get("metadata", {})
        ns = meta.get("namespace", "")
        owners = meta.get("ownerReferences", [])

        workload_kind, workload_name = "Pod", meta.get("name", "")
        if owners:
            owner = owners[0]
            kind, name = owner.get("kind", "Pod"), owner.get("name", workload_name)
            if kind == "ReplicaSet":
                resolved = rs_map.get(f"{ns}/{name}")
                if resolved:
                    workload_kind, workload_name = resolved["kind"], resolved["name"]
                else:
                    workload_kind, workload_name = "ReplicaSet", name
            else:
                workload_kind, workload_name = kind, name

        all_containers = (
            pod.get("spec", {}).get("containers", []) +
            pod.get("spec", {}).get("initContainers", [])
        )
        for cspec in all_containers:
            img = cspec.get("image", "")
            if img not in err_image_set:
                continue
            wl = {"namespace": ns, "kind": workload_kind, "name": workload_name, "container": cspec.get("name", "")}
            if img not in image_workloads:
                image_workloads[img] = []
            if wl not in image_workloads[img]:
                image_workloads[img].append(wl)

    for err_img in err_images:
        err_img["affectedWorkloads"] = image_workloads.get(err_img.get("Image", ""), [])

    return err_images


@app.route("/api/scan", methods=["POST"])
def api_scan():
    data = request.get_json(silent=True) or {}
    context = (data.get("context") or "").strip()

    nova_path = os.environ.get("NOVA_PATH", "./nova.exe" if os.name == "nt" else "./nova")

    cmd = [nova_path, "find", "--containers", "--format", "json", "-a", "--show-non-semver", "--show-errored-containers"]
    if context:
        cmd += ["--context", context]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
        )
        output = result.stdout.strip()

        if result.returncode != 0 and not output:
            return jsonify({"error": result.stderr.strip() or "nova returned a non-zero exit code"}), 500

        try:
            parsed = json.loads(output)
        except json.JSONDecodeError:
            return jsonify({"error": "nova did not return valid JSON", "raw": output[:2000]}), 500

        if parsed.get("err_images"):
            parsed["err_images"] = enrich_err_images(parsed["err_images"], context or None)

        return jsonify(parsed)

    except FileNotFoundError:
        return jsonify({
            "error": f"nova not found at '{nova_path}'. "
                     "Install Fairwinds Nova or set the NOVA_PATH environment variable."
        }), 500
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Scan timed out after 120 seconds"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/")
def index():
    return render_template("index.html")


if __name__ == "__main__":
    app.run(debug=True, port=5000)
