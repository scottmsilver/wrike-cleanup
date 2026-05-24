"""Interactive setup for the Wrike preview service on GCP.

Walks operator through project creation, Firestore, secrets, service accounts,
image build, Cloud Run deploy, scheduler jobs, and Wrike webhook registration.

Idempotent: detects what's already done and skips. Safe to re-run.

Usage:
    python preview/setup.py              # interactive
    python preview/setup.py --dry-run    # print commands without executing
    python preview/setup.py --project my-proj --region us-central1
    python preview/setup.py --non-interactive --project my-proj
"""

import argparse
import json
import secrets
import subprocess
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Global state (populated after arg parsing)
# ---------------------------------------------------------------------------

_args: argparse.Namespace | None = None


def _dry_run() -> bool:
    return _args is not None and _args.dry_run


def _non_interactive() -> bool:
    return _args is not None and _args.non_interactive


# ---------------------------------------------------------------------------
# Subprocess wrapper
# ---------------------------------------------------------------------------


def sh(
    cmd: list[str],
    *,
    capture: bool = False,
    check: bool = True,
    input_text: str | None = None,
) -> str:
    """Run *cmd* (list of args).

    In dry-run mode, print the command and return ''.
    Otherwise run it, optionally capturing stdout.
    """
    display = " ".join(cmd)
    if _dry_run():
        print(f"  [dry-run] {display}")
        return ""

    kwargs: dict = {"text": True}
    if capture:
        kwargs["stdout"] = subprocess.PIPE
        kwargs["stderr"] = subprocess.PIPE
    if input_text is not None:
        kwargs["input"] = input_text

    result = subprocess.run(cmd, **kwargs)  # noqa: S603
    if check and result.returncode != 0:
        raise subprocess.CalledProcessError(result.returncode, cmd)
    if capture:
        return (result.stdout or "").strip()
    return ""


def sh_ok(cmd: list[str], *, input_text: str | None = None) -> bool:
    """Return True if *cmd* exits 0, False otherwise (never raises)."""
    if _dry_run():
        print(f"  [dry-run check] {' '.join(cmd)}")
        return False  # conservative: assume resource absent so we print create cmds
    try:
        subprocess.run(  # noqa: S603
            cmd,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
            input=input_text,
        )
        return True
    except subprocess.CalledProcessError:
        return False


# ---------------------------------------------------------------------------
# Prompt helpers
# ---------------------------------------------------------------------------


def prompt(question: str, default: str | None = None) -> str:
    """Read a line from stdin.

    In --non-interactive mode, return *default* if set; otherwise abort.
    """
    if _non_interactive():
        if default is not None:
            return default
        sys.exit(f"ERROR: --non-interactive mode but no default for prompt: {question!r}")
    suffix = f" [{default}]" if default is not None else ""
    try:
        value = input(f"{question}{suffix}: ").strip()
    except EOFError:
        value = ""
    return value if value else (default or "")


def confirm(question: str, default: bool = True) -> bool:
    """Ask a yes/no question; return bool."""
    default_str = "Y/n" if default else "y/N"
    if _non_interactive():
        return default
    try:
        ans = input(f"{question} [{default_str}]: ").strip().lower()
    except EOFError:
        return default
    if not ans:
        return default
    return ans.startswith("y")


# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------


def preflight_check() -> None:
    """Abort early if gcloud is missing or unauthenticated."""
    if _dry_run():
        print("  [dry-run] Skipping gcloud preflight checks.")
        return
    if not sh_ok(["gcloud", "version"]):
        sys.exit(
            "ERROR: `gcloud` not found or not working. "
            "Install the Google Cloud SDK: https://cloud.google.com/sdk/docs/install"
        )
    # Check auth — 'gcloud auth list' exits 0 even when empty; check output.
    if not _dry_run():
        result = subprocess.run(  # noqa: S603
            ["gcloud", "auth", "list", "--filter=status:ACTIVE", "--format=value(account)"],
            capture_output=True,
            text=True,
        )
        if not result.stdout.strip():
            sys.exit(
                "ERROR: No active gcloud account found. Run:\n"
                "  gcloud auth login\n"
                "  gcloud auth application-default login"
            )


# ---------------------------------------------------------------------------
# Error-recovery wrapper
# ---------------------------------------------------------------------------


def run_step(step_fn, *args, **kwargs):
    """Run *step_fn*; on failure ask Continue / Retry / Abort."""
    while True:
        try:
            return step_fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            print(f"\nERROR in step: {exc}")
            if _non_interactive():
                sys.exit(1)
            choice = input("  [C]ontinue / [R]etry / [A]bort? ").strip().upper() or "A"
            if choice.startswith("C"):
                return None
            if choice.startswith("R"):
                continue
            sys.exit(1)


# ---------------------------------------------------------------------------
# Billing preflight
# ---------------------------------------------------------------------------


def ensure_billing_linked(project: str) -> None:
    """Ensure a billing account is linked to *project* before enabling APIs.

    In dry-run mode, just prints what would be checked/done and returns.
    In non-interactive mode, errors out if billing is not already linked.
    """
    if _dry_run():
        print("  [dry-run] Skipping billing account check.")
        print(f"  [dry-run] gcloud beta billing projects describe {project}")
        print("  [dry-run] gcloud beta billing accounts list")
        print(f"  [dry-run] gcloud beta billing projects link {project}" " --billing-account=<chosen>")
        return

    # --- 1. Check current billing status ---
    billing_name = sh(
        [
            "gcloud",
            "beta",
            "billing",
            "projects",
            "describe",
            project,
            "--format=value(billingAccountName)",
        ],
        capture=True,
        check=False,
    )
    if billing_name:
        # Strip "billingAccounts/" prefix for display
        short_id = billing_name.removeprefix("billingAccounts/")
        print(f"  Billing already linked: {short_id}")
        return

    # --- 2a. Caller pre-specified the account via --billing-account ---
    if _args is not None and getattr(_args, "billing_account", None):
        chosen = _args.billing_account
        print(f"  Linking pre-selected billing account {chosen!r} to project {project!r} ...")
        sh(["gcloud", "beta", "billing", "projects", "link", project, f"--billing-account={chosen}"])
        print(f"  Billing account {chosen!r} linked.")
        return

    # --- 2b. Billing not linked — list open accounts ---
    if _non_interactive():
        sys.exit(
            f"ERROR: Billing not linked for project {project!r}.\n"
            f"  Run: gcloud beta billing projects link {project} --billing-account=ACCOUNT_ID\n"
            "  Or pass --billing-account=ACCOUNT_ID, or re-run setup interactively."
        )

    # Use --format=json to avoid gcloud's csv column-aliasing surprises:
    # `--format=csv(name,displayName)` actually returns columns named
    # `account_id,name` for billing accounts, which silently drops the ID
    # if we DictReader-lookup by the field name we asked for.
    accounts_json = sh(
        [
            "gcloud",
            "beta",
            "billing",
            "accounts",
            "list",
            "--filter=open=true",
            "--format=json",
        ],
        capture=True,
        check=False,
    )

    accounts: list[tuple[str, str]] = []
    if accounts_json:
        try:
            data = json.loads(accounts_json)
        except json.JSONDecodeError:
            data = []
        for entry in data:
            raw_name = (entry.get("name") or "").strip()
            display_name = (entry.get("displayName") or "").strip()
            if not raw_name:
                continue
            account_id = raw_name.removeprefix("billingAccounts/")
            accounts.append((account_id, display_name))

    # --- 3. No open billing accounts ---
    if not accounts:
        print(
            "\n  WARNING: No open billing accounts found.\n"
            "  Create one at: https://console.cloud.google.com/billing/create\n"
            "  Then re-run this setup, or link manually:\n"
            f"    gcloud beta billing projects link {project} --billing-account=ACCOUNT_ID"
        )
        while True:
            choice = input("  [C]ontinue anyway / [R]etry / [A]bort? ").strip().upper() or "A"
            if choice.startswith("C"):
                return
            if choice.startswith("R"):
                ensure_billing_linked(project)
                return
            sys.exit(1)

    # --- 4. Let operator pick an account ---
    print(f"\n  Found {len(accounts)} open billing account(s):")
    for i, (acct_id, display_name) in enumerate(accounts, start=1):
        print(f"    [{i}] {acct_id}  {display_name}")

    default_choice = "1"
    raw = (
        prompt(
            f"  Pick one to link to project {project!r} (or 's' to skip)",
            default=default_choice,
        )
        .strip()
        .lower()
    )

    if raw == "s":
        print(
            "  Skipping billing link. NOTE: the next step (enable APIs) will fail\n"
            "  until you link a billing account manually:\n"
            f"    gcloud beta billing projects link {project} --billing-account=ACCOUNT_ID"
        )
        return

    try:
        idx = int(raw) - 1
        if not (0 <= idx < len(accounts)):
            raise ValueError
    except ValueError:
        print(f"  Invalid choice {raw!r}; defaulting to [1].")
        idx = 0

    chosen_id = accounts[idx][0]

    # --- 5. Link the chosen account ---
    print(f"  Linking billing account {chosen_id!r} to project {project!r} ...")
    sh(
        [
            "gcloud",
            "beta",
            "billing",
            "projects",
            "link",
            project,
            f"--billing-account={chosen_id}",
        ]
    )
    print(f"  Billing account {chosen_id!r} linked.")


# ---------------------------------------------------------------------------
# Step 1: GCP project + APIs
# ---------------------------------------------------------------------------


def step1_project_and_apis(cfg: dict) -> None:
    print("\n=== Step 1: GCP project + APIs ===")
    print("Create (or reuse) the GCP project and enable required APIs.")

    # Project ID
    default_project = cfg.get("project") or f"wrike-preview-{secrets.token_hex(4)}"
    project = cfg.get("project") or prompt("GCP project ID", default=default_project)
    cfg["project"] = project

    # Region
    region = cfg.get("region") or prompt("GCP region", default="us-central1")
    cfg["region"] = region

    # Check / create project
    if sh_ok(["gcloud", "projects", "describe", project]):
        print(f"  Project {project!r} already exists — skipping create.")
    else:
        print(f"  Creating project {project!r} ...")
        sh(["gcloud", "projects", "create", project])

    sh(["gcloud", "config", "set", "project", project])

    # Billing preflight — must be linked before APIs can be enabled
    print("  Checking billing account ...")
    ensure_billing_linked(project)

    # Enable APIs (gcloud is idempotent here)
    apis = [
        "run.googleapis.com",
        "firestore.googleapis.com",
        "cloudscheduler.googleapis.com",
        "artifactregistry.googleapis.com",
        "secretmanager.googleapis.com",
        "cloudbuild.googleapis.com",
    ]
    print(f"  Enabling {len(apis)} APIs (idempotent) ...")
    sh(["gcloud", "services", "enable", *apis, "--project", project])
    print("  APIs enabled.")


# ---------------------------------------------------------------------------
# Step 2: Firestore
# ---------------------------------------------------------------------------


def step2_firestore(cfg: dict) -> None:
    print("\n=== Step 2: Firestore Native database ===")
    print("Create Firestore (Native mode) database and deploy composite indexes.")

    project = cfg["project"]
    region = cfg["region"]

    if sh_ok(
        [
            "gcloud",
            "firestore",
            "databases",
            "describe",
            "--database=(default)",
            "--project",
            project,
        ]
    ):
        print("  Firestore (default) database already exists — skipping create.")
    else:
        print("  Creating Firestore database ...")
        sh(
            [
                "gcloud",
                "firestore",
                "databases",
                "create",
                "--location",
                region,
                "--project",
                project,
            ]
        )

    # Deploy composite indexes. gcloud firestore indexes composite create
    # only creates ONE index per call (no --file flag, despite what the manual
    # docs imply). We parse firestore.indexes.json and emit one call per index.
    index_file = Path("preview/firestore.indexes.json")
    if not index_file.exists() and not _dry_run():
        print(
            f"  WARNING: {index_file} not found — skipping index deployment.\n"
            "  Add it later via the Firebase CLI or per-index gcloud calls."
        )
        return

    print("  Deploying composite indexes ...")
    if _dry_run():
        print(f"  [dry-run] (would parse {index_file} and emit one gcloud call per index)")
        print(
            "  [dry-run] gcloud firestore indexes composite create "
            "--database=(default) --collection-group=jobs --query-scope=COLLECTION "
            "--field-config field-path=status,order=ascending ..."
        )
        return

    indexes_doc = json.loads(index_file.read_text())
    for idx in indexes_doc.get("indexes", []):
        collection_group = idx["collectionGroup"]
        query_scope = idx.get("queryScope", "COLLECTION")
        cmd = [
            "gcloud",
            "firestore",
            "indexes",
            "composite",
            "create",
            "--database=(default)",
            f"--collection-group={collection_group}",
            f"--query-scope={query_scope}",
        ]
        for field in idx["fields"]:
            field_path = field["fieldPath"]
            order = field.get("order", "ASCENDING").lower()
            cmd += ["--field-config", f"field-path={field_path},order={order}"]
        cmd += ["--project", project]

        # gcloud errors with ALREADY_EXISTS if the index is already there;
        # we treat that as success so the step is idempotent.
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0:
            print(f"  Created index on {collection_group}: {[f['fieldPath'] for f in idx['fields']]}")
        elif "ALREADY_EXISTS" in (result.stderr or "") or "already exists" in (result.stderr or "").lower():
            print(f"  Index on {collection_group} already exists; skipping.")
        else:
            print(result.stdout)
            print(result.stderr, file=sys.stderr)
            raise subprocess.CalledProcessError(result.returncode, cmd, output=result.stdout, stderr=result.stderr)


# ---------------------------------------------------------------------------
# Step 3: Secrets
# ---------------------------------------------------------------------------

_PLACEHOLDER = "FILL_ME_IN"


def _read_config_token() -> str | None:
    """Return WRIKE_API_TOKEN from config.json if it looks real."""
    try:
        with open("config.json") as f:
            data = json.load(f)
        token = data.get("WRIKE_API_TOKEN", "")
        if token and token != _PLACEHOLDER:
            return token
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return None


def _create_secret(name: str, value: str, project: str) -> None:
    sh(
        [
            "gcloud",
            "secrets",
            "create",
            name,
            "--data-file=-",
            "--project",
            project,
        ],
        input_text=value,
    )


def step3_secrets(cfg: dict) -> None:
    print("\n=== Step 3: Secrets in Secret Manager ===")
    print("Create wrike-api-token, webhook-signing-secret, internal-secret.")

    project = cfg["project"]

    # --- wrike-api-token ---
    if sh_ok(["gcloud", "secrets", "describe", "wrike-api-token", "--project", project]):
        print("  Secret 'wrike-api-token' already exists — skipping.")
    else:
        existing_token = _read_config_token()
        if existing_token:
            use_it = confirm(
                "  Found WRIKE_API_TOKEN in config.json — use it for 'wrike-api-token'?",
                default=True,
            )
            wrike_token = existing_token if use_it else ""
        else:
            wrike_token = ""

        if not wrike_token:
            wrike_token = prompt("  Enter your Wrike API token", default=None)
        if not wrike_token:
            sys.exit("ERROR: Wrike API token is required.")
        _create_secret("wrike-api-token", wrike_token, project)
        print("  Created secret 'wrike-api-token'.")

    # --- webhook-signing-secret ---
    if sh_ok(
        [
            "gcloud",
            "secrets",
            "describe",
            "webhook-signing-secret",
            "--project",
            project,
        ]
    ):
        print("  Secret 'webhook-signing-secret' already exists — skipping.")
    else:
        signing = secrets.token_hex(32)
        _create_secret("webhook-signing-secret", signing, project)
        print("  Created secret 'webhook-signing-secret' (auto-generated 32-byte hex).")

    # --- internal-secret ---
    if sh_ok(["gcloud", "secrets", "describe", "internal-secret", "--project", project]):
        print("  Secret 'internal-secret' already exists — skipping.")
    else:
        internal = secrets.token_hex(32)
        _create_secret("internal-secret", internal, project)
        print("  Created secret 'internal-secret' (auto-generated 32-byte hex).")


# ---------------------------------------------------------------------------
# Step 4: Service accounts + IAM
# ---------------------------------------------------------------------------


def step4_service_accounts(cfg: dict) -> None:
    print("\n=== Step 4: Service accounts + IAM ===")
    print("Create runtime and scheduler service accounts; bind IAM roles.")

    project = cfg["project"]
    runtime_sa = f"preview-sa@{project}.iam.gserviceaccount.com"
    scheduler_sa = f"preview-scheduler@{project}.iam.gserviceaccount.com"
    cfg["runtime_sa"] = runtime_sa
    cfg["scheduler_sa"] = scheduler_sa

    for account_id, display in [
        ("preview-sa", "Cloud Run runtime for preview service"),
        ("preview-scheduler", "Cloud Scheduler invoker for preview service"),
    ]:
        email = f"{account_id}@{project}.iam.gserviceaccount.com"
        if sh_ok(
            [
                "gcloud",
                "iam",
                "service-accounts",
                "describe",
                email,
                "--project",
                project,
            ]
        ):
            print(f"  Service account {email!r} already exists — skipping.")
        else:
            sh(
                [
                    "gcloud",
                    "iam",
                    "service-accounts",
                    "create",
                    account_id,
                    f"--display-name={display}",
                    "--project",
                    project,
                ]
            )
            print(f"  Created service account {email!r}.")

    # Project-level IAM: datastore.user for runtime SA
    print(f"  Binding roles/datastore.user to {runtime_sa} ...")
    sh(
        [
            "gcloud",
            "projects",
            "add-iam-policy-binding",
            project,
            f"--member=serviceAccount:{runtime_sa}",
            "--role=roles/datastore.user",
            "--condition=None",
        ]
    )

    # Per-secret IAM: secretAccessor for runtime SA
    for secret_name in ["wrike-api-token", "webhook-signing-secret", "internal-secret"]:
        print(f"  Binding secretAccessor on {secret_name!r} to {runtime_sa} ...")
        sh(
            [
                "gcloud",
                "secrets",
                "add-iam-policy-binding",
                secret_name,
                f"--member=serviceAccount:{runtime_sa}",
                "--role=roles/secretmanager.secretAccessor",
                "--project",
                project,
            ]
        )


# ---------------------------------------------------------------------------
# Step 5: Artifact Registry + image build
# ---------------------------------------------------------------------------


def step5_artifact_registry_and_build(cfg: dict) -> None:
    print("\n=== Step 5: Artifact Registry + container image ===")
    print("Create Docker repository and build the server image via Cloud Build.")

    project = cfg["project"]
    region = cfg["region"]
    image = f"{region}-docker.pkg.dev/{project}/preview/server:latest"
    cfg["image"] = image

    if sh_ok(
        [
            "gcloud",
            "artifacts",
            "repositories",
            "describe",
            "preview",
            "--location",
            region,
            "--project",
            project,
        ]
    ):
        print("  Artifact Registry repo 'preview' already exists — skipping create.")
    else:
        sh(
            [
                "gcloud",
                "artifacts",
                "repositories",
                "create",
                "preview",
                "--location",
                region,
                "--repository-format=docker",
                "--project",
                project,
            ]
        )
        print("  Created Artifact Registry repo 'preview'.")

    print(f"  Building container image (this may take several minutes)...\n" f"    {image}")
    sh(
        [
            "gcloud",
            "builds",
            "submit",
            "preview/",
            f"--tag={image}",
            "--project",
            project,
        ]
    )
    print("  Image build complete.")


# ---------------------------------------------------------------------------
# Step 6: Initial Cloud Run deploy (without OIDC_AUDIENCE)
# ---------------------------------------------------------------------------


def step6_deploy_initial(cfg: dict) -> None:
    print("\n=== Step 6: Initial Cloud Run deploy ===")
    print("Deploy the service. OIDC_AUDIENCE will be set in Step 8 once we know the URL.")

    project = cfg["project"]
    region = cfg["region"]
    image = cfg["image"]
    runtime_sa = cfg["runtime_sa"]
    scheduler_sa = cfg["scheduler_sa"]

    service_exists = sh_ok(
        [
            "gcloud",
            "run",
            "services",
            "describe",
            "preview",
            "--region",
            region,
            "--project",
            project,
        ]
    )

    do_deploy = True
    if service_exists:
        print("  Cloud Run service 'preview' already exists.")
        do_deploy = confirm("  Redeploy it now?", default=False)

    if do_deploy:
        env_vars = ",".join(
            [
                f"GOOGLE_CLOUD_PROJECT={project}",
                f"SCHEDULER_SA_EMAIL={scheduler_sa}",
            ]
        )
        sh(
            [
                "gcloud",
                "run",
                "deploy",
                "preview",
                f"--image={image}",
                "--region",
                region,
                f"--service-account={runtime_sa}",
                "--allow-unauthenticated",
                "--concurrency=2",
                "--memory=1Gi",
                "--timeout=300",
                f"--set-env-vars={env_vars}",
                "--set-secrets=WRIKE_API_TOKEN=wrike-api-token:latest,"
                "WEBHOOK_SIGNING_SECRET=webhook-signing-secret:latest,"
                "INTERNAL_SECRET=internal-secret:latest",
                "--project",
                project,
            ]
        )

    # Capture service URL
    url = sh(
        [
            "gcloud",
            "run",
            "services",
            "describe",
            "preview",
            "--region",
            region,
            "--format=value(status.url)",
            "--project",
            project,
        ],
        capture=True,
    )
    if not url and _dry_run():
        url = "https://preview-DRYRUN.run.app"
    cfg["url"] = url
    print(f"\n  Service URL: {url}\n")


# ---------------------------------------------------------------------------
# Step 7: Cloud Run invoker IAM for Scheduler
# ---------------------------------------------------------------------------


def step7_run_invoker_iam(cfg: dict) -> None:
    print("\n=== Step 7: Cloud Run invoker IAM ===")
    print("Grant the scheduler SA permission to invoke the Cloud Run service.")

    project = cfg["project"]
    region = cfg["region"]
    scheduler_sa = cfg["scheduler_sa"]

    # add-iam-policy-binding is idempotent (gcloud deduplicates bindings)
    sh(
        [
            "gcloud",
            "run",
            "services",
            "add-iam-policy-binding",
            "preview",
            "--region",
            region,
            f"--member=serviceAccount:{scheduler_sa}",
            "--role=roles/run.invoker",
            "--project",
            project,
        ]
    )
    print("  Invoker binding applied.")


# ---------------------------------------------------------------------------
# Step 8: Second deploy with OIDC_AUDIENCE
# ---------------------------------------------------------------------------


def step8_deploy_with_oidc(cfg: dict) -> None:
    print("\n=== Step 8: Redeploy with OIDC_AUDIENCE ===")
    print(
        "Cloud Scheduler uses OIDC tokens whose audience must equal the service URL.\n"
        "We couldn't set OIDC_AUDIENCE in Step 6 because we didn't know the URL yet.\n"
        "This second deploy adds it."
    )

    project = cfg["project"]
    region = cfg["region"]
    image = cfg["image"]
    runtime_sa = cfg["runtime_sa"]
    scheduler_sa = cfg["scheduler_sa"]
    url = cfg["url"]

    env_vars = ",".join(
        [
            f"GOOGLE_CLOUD_PROJECT={project}",
            f"SCHEDULER_SA_EMAIL={scheduler_sa}",
            f"OIDC_AUDIENCE={url}",
        ]
    )
    sh(
        [
            "gcloud",
            "run",
            "deploy",
            "preview",
            f"--image={image}",
            "--region",
            region,
            f"--service-account={runtime_sa}",
            "--allow-unauthenticated",
            "--concurrency=2",
            "--memory=1Gi",
            "--timeout=300",
            f"--set-env-vars={env_vars}",
            "--set-secrets=WRIKE_API_TOKEN=wrike-api-token:latest,"
            "WEBHOOK_SIGNING_SECRET=webhook-signing-secret:latest,"
            "INTERNAL_SECRET=internal-secret:latest",
            "--project",
            project,
        ]
    )
    print("  Redeployed with OIDC_AUDIENCE set.")


# ---------------------------------------------------------------------------
# Step 9: Cloud Scheduler jobs
# ---------------------------------------------------------------------------


def step9_scheduler_jobs(cfg: dict) -> None:
    print("\n=== Step 9: Cloud Scheduler jobs ===")
    print("Create wrike-tick (every 1m) and wrike-reconcile (every 30m).")

    project = cfg["project"]
    region = cfg["region"]
    scheduler_sa = cfg["scheduler_sa"]
    url = cfg["url"]

    # Read the internal secret value for the header
    print("  Reading internal-secret from Secret Manager ...")
    internal_secret_value = sh(
        [
            "gcloud",
            "secrets",
            "versions",
            "access",
            "latest",
            "--secret=internal-secret",
            "--project",
            project,
        ],
        capture=True,
    )
    if not internal_secret_value and _dry_run():
        internal_secret_value = "<internal-secret-value>"

    jobs = [
        {
            "name": "wrike-tick",
            "schedule": "* * * * *",
            "uri": f"{url}/tick",
        },
        {
            "name": "wrike-reconcile",
            "schedule": "*/30 * * * *",
            "uri": f"{url}/reconcile",
        },
    ]

    for job in jobs:
        if sh_ok(
            [
                "gcloud",
                "scheduler",
                "jobs",
                "describe",
                job["name"],
                "--location",
                region,
                "--project",
                project,
            ]
        ):
            print(f"  Scheduler job {job['name']!r} already exists — skipping.")
        else:
            sh(
                [
                    "gcloud",
                    "scheduler",
                    "jobs",
                    "create",
                    "http",
                    job["name"],
                    "--location",
                    region,
                    f"--schedule={job['schedule']}",
                    f"--uri={job['uri']}",
                    "--http-method=POST",
                    f"--oidc-service-account-email={scheduler_sa}",
                    f"--oidc-token-audience={url}",
                    f"--headers=X-Internal-Secret={internal_secret_value}",
                    "--project",
                    project,
                ]
            )
            print(f"  Created scheduler job {job['name']!r}.")


# ---------------------------------------------------------------------------
# Step 10: Wrike webhook registration
# ---------------------------------------------------------------------------


def step10_register_webhook(cfg: dict) -> None:
    print("\n=== Step 10: Register Wrike webhook (optional) ===")
    print("Register the webhook so Wrike notifies this service when attachments are added.")

    # Caller can pre-answer this prompt via --register-webhook yes|no
    preset = getattr(_args, "register_webhook", None) if _args is not None else None
    if preset == "yes":
        should_register = True
        print("  --register-webhook=yes; proceeding without prompt.")
    elif preset == "no":
        should_register = False
        print("  --register-webhook=no; skipping.")
    else:
        should_register = confirm("  Register Wrike webhook now?", default=True)

    if not should_register:
        cfg["webhook_registered"] = False
        print(
            "  Skipped. Register later with:\n"
            "    python preview/register_webhook.py "
            f"--hook-url {cfg['url']}/webhook --secret <signing-secret>"
        )
        return

    project = cfg["project"]
    url = cfg["url"]

    # Verify config.json has a real token
    existing_token = _read_config_token()
    if not existing_token:
        print(
            "  ERROR: config.json is missing or has a placeholder WRIKE_API_TOKEN.\n"
            "  Cannot register webhook without a valid token in config.json.\n"
            "  Skipping. Register later with:\n"
            "    python preview/register_webhook.py "
            f"--hook-url {url}/webhook --secret <signing-secret>"
        )
        cfg["webhook_registered"] = False
        return

    print("  Reading webhook-signing-secret ...")
    signing = sh(
        [
            "gcloud",
            "secrets",
            "versions",
            "access",
            "latest",
            "--secret=webhook-signing-secret",
            "--project",
            project,
        ],
        capture=True,
    )
    if not signing and _dry_run():
        signing = "<webhook-signing-secret-value>"

    sh(
        [
            sys.executable,
            "preview/register_webhook.py",
            "--hook-url",
            f"{url}/webhook",
            "--secret",
            signing,
        ]
    )
    cfg["webhook_registered"] = True
    print("  Webhook registered.")


# ---------------------------------------------------------------------------
# Final summary
# ---------------------------------------------------------------------------


def print_summary(cfg: dict) -> None:
    project = cfg.get("project", "<unknown>")
    url = cfg.get("url", "<unknown>")
    webhook = (
        "registered" if cfg.get("webhook_registered") else ("skipped — register later via preview/register_webhook.py")
    )
    print(
        "\n"
        "====================================================\n"
        "  Deployment complete.\n"
        f"  Service URL: {url}\n"
        "  Scheduler jobs: wrike-tick (every 1m), wrike-reconcile (every 30m)\n"
        f"  Wrike webhook: {webhook}\n"
        "\n"
        "  To verify: upload a DOCX to any Wrike task and watch:\n"
        "    gcloud logging read 'resource.type=\"cloud_run_revision\"' "
        f"--limit 20 --project={project}\n"
        "\n"
        "  Admin operations (run from this dir with "
        f"GOOGLE_CLOUD_PROJECT={project} exported):\n"
        "    python preview/admin.py stats\n"
        "    python preview/admin.py list-failed\n"
        "    python preview/admin.py requeue <attachment_id>\n"
        "===================================================="
    )


# ---------------------------------------------------------------------------
# Argument parsing + main
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Interactive GCP setup for the Wrike preview service.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print every gcloud command without executing.",
    )
    parser.add_argument(
        "--project",
        metavar="PROJECT_ID",
        default=None,
        help="GCP project ID (skip the interactive prompt).",
    )
    parser.add_argument(
        "--region",
        metavar="REGION",
        default=None,
        help="GCP region (default: us-central1).",
    )
    parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="Error rather than prompt for missing values (for CI).",
    )
    parser.add_argument(
        "--billing-account",
        metavar="ACCOUNT_ID",
        default=None,
        help="Billing account ID to link (skip the interactive picker).",
    )
    parser.add_argument(
        "--register-webhook",
        choices=["yes", "no"],
        default=None,
        help="Auto-answer the final Wrike webhook registration prompt.",
    )
    return parser.parse_args()


def main() -> None:
    global _args
    _args = parse_args()

    if _dry_run():
        print("=== DRY RUN MODE — no commands will be executed ===\n")

    preflight_check()

    # Build initial config from CLI flags; steps fill in the rest.
    cfg: dict = {}
    if _args.project:
        cfg["project"] = _args.project
    if _args.region:
        cfg["region"] = _args.region

    steps = [
        step1_project_and_apis,
        step2_firestore,
        step3_secrets,
        step4_service_accounts,
        step5_artifact_registry_and_build,
        step6_deploy_initial,
        step7_run_invoker_iam,
        step8_deploy_with_oidc,
        step9_scheduler_jobs,
        step10_register_webhook,
    ]

    for step_fn in steps:
        run_step(step_fn, cfg)

    print_summary(cfg)


if __name__ == "__main__":
    main()
