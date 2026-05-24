# Setup

One-time GCP wiring for the Wrike preview service. Assumes `gcloud` is installed and authenticated.

## Quick path: interactive script

Run from the repo root:

```bash
python preview/setup.py
```

This walks through every step below interactively, prompts for project ID / region, auto-generates the random secrets, reads your Wrike token from `config.json` if present, and handles the two-pass deploy dance (initial deploy → capture URL → redeploy with `OIDC_AUDIENCE`). It's idempotent — safe to re-run.

Flags:
- `--dry-run` — print every `gcloud` command without executing.
- `--project <id>` / `--region <r>` — skip prompts.
- `--non-interactive` — fail on any missing input (for CI).

The manual sections below document what the script does, in case you want to run individual steps yourself.

---

## 1. Project + APIs

```bash
PROJECT=wrike-preview-<your-suffix>
REGION=us-central1
gcloud projects create $PROJECT
gcloud config set project $PROJECT
gcloud services enable run.googleapis.com firestore.googleapis.com \
    cloudscheduler.googleapis.com artifactregistry.googleapis.com \
    secretmanager.googleapis.com cloudbuild.googleapis.com
```

## 2. Firestore (Native mode)

```bash
gcloud firestore databases create --location=$REGION
gcloud firestore indexes create --database='(default)' \
    --file=preview/firestore.indexes.json
```

## 3. Secrets

```bash
echo -n "$WRIKE_TOKEN" | gcloud secrets create wrike-api-token --data-file=-
openssl rand -hex 32 | gcloud secrets create webhook-signing-secret --data-file=-
openssl rand -hex 32 | gcloud secrets create internal-secret --data-file=-
```

## 4. Service accounts

```bash
gcloud iam service-accounts create preview-sa \
    --display-name="Cloud Run runtime for preview service"
gcloud iam service-accounts create preview-scheduler \
    --display-name="Cloud Scheduler invoker for preview service"

# Runtime SA needs Firestore + Secret access
gcloud projects add-iam-policy-binding $PROJECT \
    --member=serviceAccount:preview-sa@$PROJECT.iam.gserviceaccount.com \
    --role=roles/datastore.user
for s in wrike-api-token webhook-signing-secret internal-secret; do
  gcloud secrets add-iam-policy-binding $s \
      --member=serviceAccount:preview-sa@$PROJECT.iam.gserviceaccount.com \
      --role=roles/secretmanager.secretAccessor
done
```

## 5. Artifact Registry + container image

```bash
gcloud artifacts repositories create preview --location=$REGION --repository-format=docker
gcloud builds submit preview/ \
    --tag $REGION-docker.pkg.dev/$PROJECT/preview/server:latest
```

## 6. Deploy Cloud Run

```bash
gcloud run deploy preview \
    --image $REGION-docker.pkg.dev/$PROJECT/preview/server:latest \
    --region $REGION \
    --service-account preview-sa@$PROJECT.iam.gserviceaccount.com \
    --allow-unauthenticated \
    --concurrency 2 \
    --memory 1Gi \
    --timeout 300 \
    --set-env-vars GOOGLE_CLOUD_PROJECT=$PROJECT,SCHEDULER_SA_EMAIL=preview-scheduler@$PROJECT.iam.gserviceaccount.com,OIDC_AUDIENCE=https://preview-xxxxxx.run.app \
    --set-secrets WRIKE_API_TOKEN=wrike-api-token:latest,WEBHOOK_SIGNING_SECRET=webhook-signing-secret:latest,INTERNAL_SECRET=internal-secret:latest
```

Capture the deployed URL (e.g., `https://preview-xxxxxx.run.app`).

> **Note**: after the initial deploy returns the service URL, re-run the `gcloud run deploy` command with `OIDC_AUDIENCE=<the URL>` added to `--set-env-vars`. Without this, all OIDC-authenticated requests will be rejected.

## 7. Cloud Run invoker IAM for Scheduler

```bash
gcloud run services add-iam-policy-binding preview \
    --region $REGION \
    --member=serviceAccount:preview-scheduler@$PROJECT.iam.gserviceaccount.com \
    --role=roles/run.invoker
```

## 8. Cloud Scheduler jobs

```bash
INTERNAL_SECRET=$(gcloud secrets versions access latest --secret=internal-secret)
URL=https://preview-xxxxxx.run.app
gcloud scheduler jobs create http wrike-tick \
    --location=$REGION \
    --schedule='* * * * *' \
    --uri=$URL/tick \
    --http-method=POST \
    --oidc-service-account-email=preview-scheduler@$PROJECT.iam.gserviceaccount.com \
    --oidc-token-audience=$URL \
    --headers="X-Internal-Secret=$INTERNAL_SECRET"

gcloud scheduler jobs create http wrike-reconcile \
    --location=$REGION \
    --schedule='*/30 * * * *' \
    --uri=$URL/reconcile \
    --http-method=POST \
    --oidc-service-account-email=preview-scheduler@$PROJECT.iam.gserviceaccount.com \
    --oidc-token-audience=$URL \
    --headers="X-Internal-Secret=$INTERNAL_SECRET"
```

## 9. Register the Wrike webhook

```bash
SIGNING=$(gcloud secrets versions access latest --secret=webhook-signing-secret)
python preview/register_webhook.py \
    --hook-url $URL/webhook \
    --secret $SIGNING
```

Wrike will hit `$URL/webhook` with the verification body; the deployed service responds with the HMAC; Wrike marks the webhook active.

## 10. Verify

Upload a DOCX to a real Wrike task. Watch Cloud Logging for the preview service:

```bash
gcloud logging read 'resource.type="cloud_run_revision" AND resource.labels.service_name="preview"' \
    --limit 20 --format="value(timestamp,jsonPayload.message,textPayload)"
```

Within ~1 minute, `preview_<id>.pdf` should appear on the task.

## Admin operations

```bash
GOOGLE_CLOUD_PROJECT=$PROJECT python preview/admin.py stats
GOOGLE_CLOUD_PROJECT=$PROJECT python preview/admin.py list-failed
GOOGLE_CLOUD_PROJECT=$PROJECT python preview/admin.py requeue <attachmentId>
GOOGLE_CLOUD_PROJECT=$PROJECT python preview/admin.py reset-reconcile-state
```
