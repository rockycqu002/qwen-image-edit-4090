#!/usr/bin/env python3
"""Create / read / update the RunPod Serverless template + endpoint for this worker through the v1 REST API.

  python deploy/runpod_api.py create --image ghcr.io/<owner>/qwen-image-edit-4090@sha256:... [--max-workers 1]
  python deploy/runpod_api.py show   --endpoint <id>
  python deploy/runpod_api.py update --endpoint <id> --max-workers 5
  python deploy/runpod_api.py billing --endpoint <id> [--days 1]

Nothing here touches any endpoint other than the ids you pass. The API key comes from $RUNPOD_API_KEY or
~/.config/runpod/api_key and is never printed.
"""
import argparse, json, os, sys, urllib.error, urllib.request

REST = "https://rest.runpod.io/v1"
US_AND_IS2 = ["US-IL-1", "US-TX-3", "US-KS-2", "US-GA-2", "US-WA-1", "US-TX-1", "US-TX-4", "US-CA-2", "US-NC-1", "US-DE-1",
              "US-KS-3", "US-GA-1", "US-MD-1", "EUR-IS-2"]
GPU = "NVIDIA GeForce RTX 4090"
OLD_ENDPOINT = "w2ww53ksg9wtj7"   # production FireRed endpoint: this script refuses to update it


def key():
    k = os.environ.get("RUNPOD_API_KEY")
    if not k:
        k = open(os.path.expanduser("~/.config/runpod/api_key")).read().strip()
    return k


def call(method, path, body=None):
    req = urllib.request.Request(REST + path, data=json.dumps(body).encode() if body is not None else None, method=method,
                                 headers={"Authorization": f"Bearer {key()}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        sys.exit(f"{method} {path} -> HTTP {e.code}: {e.read()[:800].decode(errors='replace')}")


def create(a):
    env = {"QIE_STEPS": str(a.steps), "QIE_RESOLUTION": str(a.resolution), "QIE_LOG_LEVEL": "INFO"}
    tpl = call("POST", "/templates", {"name": a.name, "imageName": a.image, "isServerless": True, "containerDiskInGb": a.disk,
                                      "env": env, "category": "NVIDIA", **({"containerRegistryAuthId": a.registry_auth} if a.registry_auth else {})})
    print("template:", json.dumps({k: tpl.get(k) for k in ("id", "name", "imageName", "containerDiskInGb", "env")}, ensure_ascii=False))
    ep = call("POST", "/endpoints", {
        "templateId": tpl["id"], "name": a.name, "computeType": "GPU", "gpuTypeIds": [GPU], "gpuCount": 1,
        "minCudaVersion": "13.0", "dataCenterIds": a.datacenters or US_AND_IS2,
        "workersMin": 0, "workersMax": a.max_workers, "idleTimeout": 5, "executionTimeoutMs": 60000,
        "flashboot": True, "scalerType": "REQUEST_COUNT", "scalerValue": 1})
    print("endpoint:", json.dumps(ep, ensure_ascii=False, indent=1))
    show(argparse.Namespace(endpoint=ep["id"]))


def show(a):
    ep = call("GET", f"/endpoints/{a.endpoint}")
    keep = ("id", "name", "templateId", "gpuTypeIds", "gpuCount", "minCudaVersion", "allowedCudaVersions", "dataCenterIds", "workersMin", "workersMax",
            "idleTimeout", "executionTimeoutMs", "flashboot", "scalerType", "scalerValue", "computeType", "networkVolumeId", "createdAt")
    print(json.dumps({k: ep.get(k) for k in keep}, ensure_ascii=False, indent=1))
    if ep.get("templateId"):
        tpl = call("GET", f"/templates/{ep['templateId']}")
        print("template:", json.dumps({k: tpl.get(k) for k in ("id", "name", "imageName", "containerDiskInGb", "env", "isServerless")}, ensure_ascii=False, indent=1))


def update(a):
    if a.endpoint == OLD_ENDPOINT:
        sys.exit("refusing to modify the production endpoint")
    body = {}
    if a.max_workers is not None: body["workersMax"] = a.max_workers
    if a.min_workers is not None: body["workersMin"] = a.min_workers
    if a.timeout_ms is not None: body["executionTimeoutMs"] = a.timeout_ms
    print(json.dumps(call("PATCH", f"/endpoints/{a.endpoint}", body), ensure_ascii=False, indent=1))
    show(a)


def billing(a):
    print(json.dumps(call("GET", f"/billing/endpoints?endpointId={a.endpoint}&days={a.days}"), ensure_ascii=False, indent=1)[:4000])


def main():
    p = argparse.ArgumentParser(); sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("create"); c.add_argument("--image", required=True); c.add_argument("--name", default="qwen-image-2p1-int8-4090")
    c.add_argument("--max-workers", type=int, default=1); c.add_argument("--disk", type=int, default=20); c.add_argument("--steps", type=int, default=20)
    c.add_argument("--resolution", type=int, default=1024); c.add_argument("--registry-auth"); c.add_argument("--datacenters", nargs="*"); c.set_defaults(fn=create)
    s = sub.add_parser("show"); s.add_argument("--endpoint", required=True); s.set_defaults(fn=show)
    u = sub.add_parser("update"); u.add_argument("--endpoint", required=True); u.add_argument("--max-workers", type=int); u.add_argument("--min-workers", type=int)
    u.add_argument("--timeout-ms", type=int); u.set_defaults(fn=update)
    b = sub.add_parser("billing"); b.add_argument("--endpoint", required=True); b.add_argument("--days", type=int, default=1); b.set_defaults(fn=billing)
    a = p.parse_args(); a.fn(a)


if __name__ == "__main__":
    main()
