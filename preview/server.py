"""Flask app exposing /webhook, /tick, /reconcile, /healthz."""

import logging
import os
import sys
from pathlib import Path

from flask import Flask, make_response, request
from google.cloud import firestore

sys.path.insert(0, str(Path(__file__).parent.parent))  # so we can import wrike
from wrike import WrikeApi

from auth import authorize_scheduler_request
from store import Store
from webhook_auth import compute_handshake_response, verify_event_signature
from worker import process_one_job

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("preview")

BATCH_SIZE = 3  # max jobs drained per /tick invocation; tunable


def _make_app():
    app = Flask(__name__)

    project = os.environ.get("GOOGLE_CLOUD_PROJECT", "local-dev")
    db = firestore.Client(project=project)
    store = Store(db)

    config_path = os.environ.get("WRIKE_CONFIG_PATH")
    if not config_path:
        # Dev mode fallback: write to /tmp (ephemeral, not in any repo)
        # so an accidental volume-mount can't pick up the token.
        config_path = "/tmp/wrike-config.json"

    if not os.path.exists(config_path):
        token = os.environ.get("WRIKE_API_TOKEN", "")
        if not token:
            raise RuntimeError(
                "Wrike credentials missing: set WRIKE_CONFIG_PATH to a JSON file "
                "or WRIKE_API_TOKEN to the token string."
            )
        Path(config_path).parent.mkdir(parents=True, exist_ok=True)
        with open(config_path, "w") as fh:
            fh.write(f'{{"WRIKE_API_TOKEN":"{token}"}}\n')
    wrike = WrikeApi(config_path)

    signing_secret = os.environ.get("WEBHOOK_SIGNING_SECRET", "")

    @app.get("/healthz")
    def healthz():
        return ("ok", 200)

    @app.post("/webhook")
    def webhook():
        body = request.get_data()
        x_hook_secret = request.headers.get("X-Hook-Secret")
        x_hook_signature = request.headers.get("X-Hook-Signature")

        try:
            json_body = request.get_json(silent=True) or {}
        except Exception:
            json_body = {}

        # Handshake: Wrike sends a verification body with X-Hook-Secret.
        if json_body.get("requestType") == "WebHook secret verification" and x_hook_secret:
            response_value = compute_handshake_response(signing_secret, x_hook_secret)
            r = make_response("", 200)
            r.headers["X-Hook-Secret"] = response_value
            return r

        # Event delivery — verify signature.
        if not verify_event_signature(signing_secret, body, x_hook_signature):
            log.warning("webhook signature mismatch")
            return ("", 200)  # do NOT 4xx — Wrike suspends on 4xx

        event_type = json_body.get("eventType")
        if event_type != "AttachmentAdded":
            return ("", 200)

        attachment_id = json_body.get("attachmentId")
        task_id = json_body.get("taskId")
        if not attachment_id:
            log.warning("webhook event missing attachmentId: %s", json_body)
            return ("", 200)

        try:
            created = store.create_job_if_absent(attachment_id, task_id=task_id)
            log.info("webhook attachment=%s task=%s created=%s", attachment_id, task_id, created)
        except Exception as e:
            # Reconcile is the safety net; log loudly and return 200 so Wrike
            # doesn't retry-and-suspend on a Firestore outage.
            log.error("webhook firestore write failed: %s", e)
        return ("", 200)

    @app.post("/tick")
    def tick():
        if not authorize_scheduler_request(request.headers):
            log.warning("tick: unauthorized")
            return ("", 200)

        processed = []
        for _ in range(BATCH_SIZE):
            attachment_id = process_one_job(store, wrike)
            if attachment_id is None:
                break
            processed.append(attachment_id)
        return ({"processed": processed}, 200)

    @app.post("/reconcile")
    def reconcile_route():
        if not authorize_scheduler_request(request.headers):
            log.warning("reconcile: unauthorized")
            return ("", 200)
        # Implementation comes in Phase 5.
        return ({"status": "not_yet_implemented"}, 200)

    return app


app = _make_app()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
