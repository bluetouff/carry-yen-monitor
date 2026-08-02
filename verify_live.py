#!/usr/bin/env python3
"""Compare les artefacts YCT servis en HTTPS au bundle local et valide data.json."""

import argparse
import hashlib
import os
import sys
import tempfile
import urllib.parse
import urllib.request

import verify_snapshot

MAX_BYTES = 8 * 1024 * 1024


def fetch(base_url, path):
    url = urllib.parse.urljoin(base_url.rstrip("/") + "/", path)
    request = urllib.request.Request(url, headers={"User-Agent": "yct-live-verifier/1.0"})
    with urllib.request.urlopen(request, timeout=20) as response:
        if response.geturl().split("://", 1)[0] != "https":
            raise RuntimeError("redirection hors HTTPS pour %s" % path)
        body = response.read(MAX_BYTES + 1)
        if len(body) > MAX_BYTES:
            raise RuntimeError("reponse trop volumineuse pour %s" % path)
        return body, {key.lower(): value for key, value in response.headers.items()}


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="https://yct.l0g.fr/")
    parser.add_argument("--bundle-dir", default=os.path.dirname(os.path.abspath(__file__)))
    args = parser.parse_args()
    if urllib.parse.urlparse(args.base_url).scheme != "https":
        print("[yct-live] ECHEC : base URL non HTTPS", file=sys.stderr)
        return 1

    web_dir = os.path.join(args.bundle_dir, "web")
    if not os.path.isdir(web_dir):
        web_dir = args.bundle_dir

    errors = []
    for name in ("index.html", "app.css", "app.js"):
        local_path = os.path.join(web_dir, name)
        try:
            with open(local_path, "rb") as handle:
                local = handle.read()
            remote, headers = fetch(args.base_url, name)
            local_hash, remote_hash = sha256(local), sha256(remote)
            if local_hash != remote_hash:
                errors.append("%s differe (local %s, prod %s)" % (name, local_hash, remote_hash))
            else:
                print("[yct-live] %s %s" % (name, remote_hash))
            if name == "index.html":
                csp = headers.get("content-security-policy", "")
                if "default-src 'none'" not in csp or "connect-src 'self'" not in csp:
                    errors.append("CSP de production incomplete")
                if "max-age=63072000" not in headers.get("strict-transport-security", ""):
                    errors.append("HSTS de production incomplet")
                if "no-cache" not in headers.get("cache-control", ""):
                    errors.append("index.html doit etre revalide (Cache-Control no-cache absent)")
        except Exception as exc:  # noqa: BLE001
            errors.append("%s illisible : %s" % (name, exc))

    data = None
    try:
        snapshot, headers = fetch(args.base_url, "data.json")
        if "max-age=300" not in headers.get("cache-control", ""):
            errors.append("Cache-Control de data.json inattendu")
        with tempfile.NamedTemporaryFile(suffix=".json") as handle:
            handle.write(snapshot)
            handle.flush()
            snapshot_errors, data = verify_snapshot.validate(handle.name)
        errors.extend("data.json : %s" % error for error in snapshot_errors)
        if not snapshot_errors:
            print(
                "[yct-live] data.json OK generated=%s cot=%s fx=%s"
                % (data["generated"], data["cot"][-1]["d"], data["fx"][-1]["d"])
            )
    except Exception as exc:  # noqa: BLE001
        errors.append("data.json illisible : %s" % exc)

    status_data = None
    try:
        status_body, headers = fetch(args.base_url, "status.json")
        if "no-store" not in headers.get("cache-control", ""):
            errors.append("Cache-Control de status.json doit contenir no-store")
        with tempfile.NamedTemporaryFile(suffix=".json") as handle:
            handle.write(status_body)
            handle.flush()
            status_errors, status_data = verify_snapshot.validate_status(handle.name, data or {})
        errors.extend("status.json : %s" % error for error in status_errors)
        if not status_errors:
            print("[yct-live] status.json OK checked=%s published=%s" % (
                status_data["checked_at"], status_data["published"]
            ))
    except Exception as exc:  # noqa: BLE001
        errors.append("status.json illisible : %s" % exc)

    massive_status = (((status_data or {}).get("sources") or {}).get("massive") or {}).get("status")
    if massive_status == "fresh":
        try:
            market_body, headers = fetch(args.base_url, "market.json")
            if "max-age=60" not in headers.get("cache-control", ""):
                errors.append("Cache-Control de market.json inattendu")
            with tempfile.NamedTemporaryFile(suffix=".json") as handle:
                handle.write(market_body)
                handle.flush()
                market_errors, market = verify_snapshot.validate_market(handle.name)
            errors.extend("market.json : %s" % error for error in market_errors)
            if not market_errors:
                print("[yct-live] market.json OK mid=%.5f at=%s" % (
                    float(market["mid"]), market["data_as_of"]
                ))
        except Exception as exc:  # noqa: BLE001
            errors.append("market.json illisible : %s" % exc)

    if errors:
        for error in errors:
            print("[yct-live] ECHEC : %s" % error, file=sys.stderr)
        return 1
    print("[yct-live] OK : bundle local = artefacts HTTPS, snapshot sain")
    return 0


if __name__ == "__main__":
    sys.exit(main())
