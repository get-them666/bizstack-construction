# Google Cloud Run deployment

`.github/workflows/deploy-cloud-run.yml` builds the repository's existing
Dockerfile, pushes an immutable commit-tagged image to Artifact Registry, and
deploys it to the existing `bizstack-construction` Cloud Run service. It runs
for pushes to `main` and manual dispatches from `main`.

## One-time Google Cloud setup

Create or select the production project and enable the Cloud Run, Artifact
Registry, IAM Credentials, and Security Token Service APIs. Create an Artifact
Registry Docker repository named `cloud-run` in the same region as the Cloud
Run service. The service must already exist and have its production runtime
settings configured before enabling this image-only deployment.

Configure Workload Identity Federation for GitHub Actions:

1. Create a Workload Identity Pool and OIDC provider for
   `https://token.actions.githubusercontent.com`. Map
   `google.subject=assertion.sub`, `attribute.repository=assertion.repository`,
   and `attribute.ref=assertion.ref`; set the provider condition to
   `assertion.repository == 'get-them666/bizstack-construction' &&
   assertion.ref == 'refs/heads/main'`.
2. Create a dedicated deploy service account. Grant this principal permission
   to impersonate it (`roles/iam.workloadIdentityUser` on that service
   account): `principalSet://iam.googleapis.com/projects/PROJECT_NUMBER/locations/global/workloadIdentityPools/POOL_ID/attribute.repository/get-them666/bizstack-construction`.
3. Grant the deploy service account `roles/artifactregistry.writer` on the
   `cloud-run` repository and `roles/run.admin` for the project. Grant
   `roles/iam.serviceAccountUser` on the Cloud Run service's runtime service
   account so Cloud Run can continue deploying with that identity.
4. Add these **Actions repository variables** under
   **Settings → Secrets and variables → Actions → Variables**:

   | Variable | Value |
   | --- | --- |
   | `GCP_PROJECT_ID` | Google Cloud project ID |
   | `GCP_REGION` | Region containing the Artifact Registry repository and Cloud Run service |
   | `GCP_WORKLOAD_IDENTITY_PROVIDER` | Full provider resource name, e.g. `projects/PROJECT_NUMBER/locations/global/workloadIdentityPools/POOL_ID/providers/PROVIDER_ID` |
   | `GCP_DEPLOY_SERVICE_ACCOUNT` | Deploy service account email |

No service-account key, database password, or other secret belongs in GitHub
repository variables, workflow files, or source control.

## Cloud SQL and database secret

Before the first workflow run, configure the existing Cloud Run service with
its Cloud SQL instance connection and grant its runtime service account
`roles/cloudsql.client` for that instance. Create the `DATABASE_URL` value in
Google Secret Manager and configure the Cloud Run service to expose it to the
container as the `DATABASE_URL` environment variable using a Secret Manager
secret reference. Grant the runtime service account
`roles/secretmanager.secretAccessor` on that secret. Keep the database URL and
credentials out of this repository.

The workflow deploys only a new image. It does not set or clear environment
variables, secrets, Cloud SQL connections, runtime identity, or other service
configuration. Keep those settings on the existing Cloud Run service; do not
add `--set-env-vars`, `--set-secrets`, or Cloud SQL clear/set flags to the
deployment command.

## Smoke test and DNS

After a deployment, smoke-test the Cloud Run service URL, including a health
check and the application's database-backed paths. Confirm the expected
revision, application behavior, and Cloud SQL connectivity before changing
any traffic routing. Only after those checks pass should you update the
relevant Cloudflare DNS records to point to Cloud Run. Keep Cloudflare DNS
unchanged until then.
