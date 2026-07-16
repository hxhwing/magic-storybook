#!/usr/bin/env bash
#
# magic-storybook — 0→1 deployment for Cloud Shell.
# Run from the repo root:  bash deploy.sh
#
# Deploys TWO Cloud Run services from the same image:
#   • <name>-frontend : web UI for users, protected by IAP.
#   • <name>-a2a      : A2A endpoint for Gemini Enterprise, IAM-authenticated
#                       (Discovery Engine SA run.invoker). NO IAP.
# (Cloud Run built-in IAP intercepts ALL ingress and rejects GE's service-to-
#  service run.invoker token, so IAP and A2A must live on separate services.)
#
# Resilient: a failing command (e.g. missing setIamPolicy permission) is recorded
# and the script CONTINUES; the list of failed commands is printed at the end so
# you can re-run them offline with sufficient permissions.
#
# Override via env vars, e.g.:  REGION=us-central1 IAP_MEMBER=group:foo@x.com bash deploy.sh
set -uo pipefail

FAILED=()
run() {
  echo "▶ $*"
  if "$@"; then return 0; fi
  echo "  ⚠️  FAILED (continuing): $*"
  FAILED+=("$*")
  return 0
}

# ── Config ───────────────────────────────────────────────────────────────────
PROJECT_ID="${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null)}"
PROJECT_NUMBER="$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')"
REGION="${REGION:-us-central1}"                       # Cloud Run + bucket + Firestore all here
NAME="${SERVICE_NAME:-magic-storybook}"
VERTEX_LOCATION="${VERTEX_LOCATION:-global}"          # Gemini 3 models resolve in 'global'

FRONTEND_SERVICE="${NAME}-frontend"
A2A_SERVICE="${NAME}-a2a"
BUCKET="magic-storybook-${PROJECT_ID}"
FIRESTORE_DATABASE="magic-storybook-${PROJECT_ID}"
FIRESTORE_COLLECTION="storybooks"

FRONTEND_URL="https://${FRONTEND_SERVICE}-${PROJECT_NUMBER}.${REGION}.run.app"
A2A_URL="https://${A2A_SERVICE}-${PROJECT_NUMBER}.${REGION}.run.app"

IAP_MEMBER="${IAP_MEMBER:-group:googlers@google.com}"
CURRENT_USER="$(gcloud config get-value account 2>/dev/null)"   # the Cloud Shell user running this
GE_APP_ID="${GE_APP_ID:-}"

RUNTIME_SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"
DE_SA="service-${PROJECT_NUMBER}@gcp-sa-discoveryengine.iam.gserviceaccount.com"
IAP_SA="service-${PROJECT_NUMBER}@gcp-sa-iap.iam.gserviceaccount.com"

# SIGNING_SA = the SA to mint V4 signed URLs as (keyless, via IAM signBlob). It also
# needs the Token Creator role on itself (granted below).
COMMON_ENV="GOOGLE_CLOUD_PROJECT=${PROJECT_ID},GCP_LOCATION=${VERTEX_LOCATION},GCS_BUCKET=${BUCKET},FIRESTORE_DATABASE=${FIRESTORE_DATABASE},FIRESTORE_COLLECTION=${FIRESTORE_COLLECTION},SIGNED_URL_TTL_SECONDS=604800,SIGNING_SA=${RUNTIME_SA},AGENT_VERSION=0.1.0"
RUN_FLAGS="--memory 8Gi --cpu 4 --timeout 3600 --no-cpu-throttling --min-instances 1 --max-instances 10 --concurrency 40 --labels created-by=adk"

echo "▶ Project:   $PROJECT_ID ($PROJECT_NUMBER) / $REGION"
echo "▶ Frontend:  $FRONTEND_SERVICE  (IAP)   → $FRONTEND_URL"
echo "▶ A2A:       $A2A_SERVICE  (IAM)   → $A2A_URL"
echo "▶ Bucket:    gs://$BUCKET (private) | Firestore db: $FIRESTORE_DATABASE / coll: $FIRESTORE_COLLECTION"
echo

# ── 1. Enable APIs ───────────────────────────────────────────────────────────
run gcloud services enable \
  run.googleapis.com cloudbuild.googleapis.com aiplatform.googleapis.com \
  firestore.googleapis.com storage.googleapis.com discoveryengine.googleapis.com \
  iap.googleapis.com --project "$PROJECT_ID"

# ── 2. GCS bucket (PRIVATE; media served via V4 signed URLs) ─────────────────
if gcloud storage buckets describe "gs://${BUCKET}" --project "$PROJECT_ID" >/dev/null 2>&1; then
  echo "▶ GCS bucket gs://$BUCKET already exists — skipping create."
else
  run gcloud storage buckets create "gs://${BUCKET}" \
    --project "$PROJECT_ID" --location "$REGION" --uniform-bucket-level-access
fi
# bucket stays private (no allUsers) — the app mints time-limited signed URLs.

# ── 3. Firestore (named database, Native mode, same region) ──────────────────
if gcloud firestore databases describe --database="${FIRESTORE_DATABASE}" --project "$PROJECT_ID" >/dev/null 2>&1; then
  echo "▶ Firestore database '$FIRESTORE_DATABASE' already exists — skipping create."
else
  run gcloud firestore databases create \
    --database="${FIRESTORE_DATABASE}" --location="$REGION" \
    --type=firestore-native --project "$PROJECT_ID"
fi

# ── 4. Runtime service account permissions ───────────────────────────────────
run gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${RUNTIME_SA}" --role=roles/datastore.user --condition=None
run gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${RUNTIME_SA}" --role=roles/aiplatform.user --condition=None
run gcloud storage buckets add-iam-policy-binding "gs://${BUCKET}" \
  --member="serviceAccount:${RUNTIME_SA}" --role=roles/storage.objectAdmin
# Needed to mint V4 signed URLs without a key (IAM signBlob → self-impersonation).
run gcloud iam service-accounts add-iam-policy-binding "$RUNTIME_SA" \
  --project "$PROJECT_ID" \
  --member="serviceAccount:${RUNTIME_SA}" --role=roles/iam.serviceAccountTokenCreator

# ── 5. Deploy the A2A service (IAM-authenticated, no IAP) ─────────────────────
run gcloud run deploy "$A2A_SERVICE" \
  --project "$PROJECT_ID" --region "$REGION" --source . $RUN_FLAGS \
  --no-allow-unauthenticated \
  --update-env-vars "${COMMON_ENV},APP_URL=${A2A_URL},READER_BASE_URL=${FRONTEND_URL}"

gcloud beta services identity create --service=discoveryengine.googleapis.com --project "$PROJECT_ID" >/dev/null 2>&1 || true
run gcloud run services add-iam-policy-binding "$A2A_SERVICE" \
  --project "$PROJECT_ID" --region "$REGION" \
  --member="serviceAccount:${DE_SA}" --role=roles/run.invoker

# ── 6. Deploy the frontend service (IAP-protected, for users) ────────────────
run gcloud beta run deploy "$FRONTEND_SERVICE" \
  --project "$PROJECT_ID" --region "$REGION" --source . $RUN_FLAGS \
  --no-allow-unauthenticated --iap \
  --update-env-vars "${COMMON_ENV},APP_URL=${FRONTEND_URL},READER_BASE_URL=${FRONTEND_URL}"

gcloud beta services identity create --service=iap.googleapis.com --project "$PROJECT_ID" >/dev/null 2>&1 || true
run gcloud run services add-iam-policy-binding "$FRONTEND_SERVICE" \
  --project "$PROJECT_ID" --region "$REGION" \
  --member="serviceAccount:${IAP_SA}" --role=roles/run.invoker
run gcloud beta iap web add-iam-policy-binding \
  --member="$IAP_MEMBER" --role=roles/iap.httpsResourceAccessor \
  --resource-type=cloud-run --service="$FRONTEND_SERVICE" \
  --region="$REGION" --project="$PROJECT_ID"
# Also grant the current Cloud Shell user so whoever runs this can reach the app.
if [[ -n "$CURRENT_USER" && "user:$CURRENT_USER" != "$IAP_MEMBER" ]]; then
  run gcloud beta iap web add-iam-policy-binding \
    --member="user:${CURRENT_USER}" --role=roles/iap.httpsResourceAccessor \
    --resource-type=cloud-run --service="$FRONTEND_SERVICE" \
    --region="$REGION" --project="$PROJECT_ID"
fi

# ── 7. Register the A2A service with Gemini Enterprise (optional) ─────────────
if [[ -n "$GE_APP_ID" ]]; then
  # Invoke via `uv tool run` so we don't depend on ~/.local/bin being on PATH
  # (uv fetches google-agents-cli on demand if it isn't installed yet).
  run uv tool run --from google-agents-cli agents-cli publish gemini-enterprise \
    --registration-type a2a \
    --agent-card-url "${A2A_URL}/a2a/app/.well-known/agent-card.json" \
    --gemini-enterprise-app-id "$GE_APP_ID" \
    --deployment-target cloud_run --project-id "$PROJECT_ID" \
    --display-name "魔法绘本" \
    --description "✨说出你的想法，我就把它变成一本魔法绘本！生成完整故事、逐页精美插画、有声朗读和专属主题曲，还给你一个能直接翻阅的沉浸式阅读器，画风任你选。"
else
  echo "▶ Skipping Gemini Enterprise registration (set GE_APP_ID to enable)."
fi

# ── Summary ──────────────────────────────────────────────────────────────────
echo
echo "✅ deploy.sh finished."
echo "   Frontend (IAP, users):  ${FRONTEND_URL}/"
echo "   A2A (IAM, GE):          ${A2A_URL}/a2a/app/.well-known/agent-card.json"
if (( ${#FAILED[@]} )); then
  echo
  echo "⚠️  ${#FAILED[@]} command(s) FAILED (likely missing permissions) — re-run offline:"
  for c in "${FAILED[@]}"; do echo "    $c"; done
  exit 1
fi
echo "   All steps succeeded."
