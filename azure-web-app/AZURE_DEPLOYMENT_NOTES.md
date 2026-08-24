# Azure Deployment Reference — POC/Demo Hosting

**Last updated:** 2026-08-21
**Purpose:** Reusable Azure setup for hosting Docker-based POC demos (React frontend + Python backend + SQLite). Infrastructure is provisioned once and reused for every future demo — only the container image changes.

---

## 1. Account / Subscription

| Field | Value |
|---|---|
| Subscription name | `Automation SL- test` |
| Subscription ID | `e9341310-03e3-448a-9da9-0694b8e5887e` |
| Tenant | Coforge Limited (`ntlgnoida.onmicrosoft.com`) |
| Tenant ID | `b727a530-a0d5-4fb8-bd40-d8f9763e97db` |

This is a work/corporate Azure subscription, not personal — be mindful of org policy and cost when provisioning additional resources.

---

## 2. Existing resources reused (already existed before this demo, provisioned for other projects)

| Resource | Name | Type | Location | Notes |
|---|---|---|---|---|
| Resource Group | `ia-practice1` | Resource Group | eastus | Shared across multiple personal/POC projects |
| App Service Plan | `ASP-iapractice1-8b89` | Linux App Service Plan | eastus | SKU **Premium0V3** — plenty of headroom to host many Web Apps on the same plan (Premium tier supports up to 100 apps/plan), so no new plan was created |
| Container Registry | `iapracticeregistry` | Azure Container Registry | eastus | SKU **Basic**, admin credentials **enabled**. Login server: `iapracticeregistry.azurecr.io` |

Other apps already on this plan/registry (for reference, unrelated to this demo):
- `agent-iq` Web App → runs image `iapracticeregistry.azurecr.io/agentiq-catalyst:latest`
- `agent-iq-app` Static Web App (eastus2)
- `adb-grant-loan-agent` Bot Service

---

## 3. New resource created for this project

| Resource | Name | Notes |
|---|---|---|
| Web App | `iapractice1-demo` | Linux container Web App on `ASP-iapractice1-8b89` |
| URL | `https://iapractice1-demo.azurewebsites.net` | HTTPS automatic on default domain |
| Current image | `mcr.microsoft.com/azuredocs/aci-helloworld:latest` (placeholder — swap once the real Dockerfile is built) |
| Auth to ACR | ACR **admin username/password** (same pattern as `agent-iq`) — not managed identity, kept simple since this is deliberately a low-security POC/demo setup |
| App setting | `WEBSITES_PORT=80` (matches placeholder image; **must be updated** to match the real app's listening port) |

### Deliberately skipped (documented decision, not an oversight)
- **System-assigned Managed Identity + AcrPull role** — skipped in favor of ACR admin credentials, for simplicity. Revisit only if this ever needs tighter security.
- **Storage Account + Azure Files mount for SQLite persistence** — skipped. SQLite data in the container is **ephemeral**: it resets on every restart/redeploy. Acceptable for demo purposes since each new demo overwrites the previous one anyway. If a demo ever needs durable data across restarts, either add an Azure Files mount or move to Postgres Flexible Server.
- **Application Insights, Key Vault, custom domain** — not provisioned; add only if a specific need arises.

---

## 4. Architecture (planned, once Dockerfile exists)

Single container: Python backend serves the React production build as static files, API under `/api/*`, one exposed port. Multi-stage Dockerfile builds React first, copies the build output into the Python image.

```
azure-web-app/
├── backend/
│   ├── app/main.py
│   ├── requirements.txt
├── frontend/
│   ├── src/, package.json
├── Dockerfile
└── .dockerignore
```

---

## 5. Workflow: shipping a new demo (repeat for every new POC)

This is the **only thing that changes** between demos — all infrastructure above stays fixed.

```powershell
# 1. Build the image in the cloud directly from your project folder (no local docker needed)
#    Use a distinct version tag each time — do NOT always overwrite "latest" —
#    so you can roll back instantly if a new demo build has issues.
az acr build --registry iapracticeregistry --image poc-demo:v2 .

# 2. Point the existing Web App at the new image
az webapp config container set --resource-group ia-practice1 --name iapractice1-demo `
  --container-image-name iapracticeregistry.azurecr.io/poc-demo:v2

# 3. Update the listening port if it changed
az webapp config appsettings set --resource-group ia-practice1 --name iapractice1-demo --settings WEBSITES_PORT=8000

# 4. Restart to pull and start the new image
az webapp restart --resource-group ia-practice1 --name iapractice1-demo
```

Same URL every time: **https://iapractice1-demo.azurewebsites.net**

### Rollback (if a new demo build is broken)
```powershell
az webapp config container set --resource-group ia-practice1 --name iapractice1-demo `
  --container-image-name iapracticeregistry.azurecr.io/poc-demo:v1
az webapp restart --resource-group ia-practice1 --name iapractice1-demo
```

---

## 6. Known limitations / things to remember

1. **One demo live at a time.** Swapping the image replaces whatever is currently running. If two demos ever need to be reachable simultaneously, create a second Web App on the same App Service Plan (cheap — plan has capacity) rather than reusing `iapractice1-demo`.
2. **SQLite is ephemeral** on this Web App — data does not survive restarts/redeploys. Do not rely on it for anything that must persist across demo swaps.
3. **ACR image cleanup.** Old tagged images are not auto-deleted and accumulate storage cost over time. Periodically check and prune:
   ```powershell
   az acr repository show-tags --name iapracticeregistry --repository poc-demo
   az acr repository delete --name iapracticeregistry --image poc-demo:v1 --yes
   ```
4. **ACR admin credentials** are a shared static secret for the whole registry — don't commit them to source control. Fetch on demand:
   ```powershell
   az acr credential show --name iapracticeregistry --query "passwords[0].value" -o tsv
   ```

---

## 7. Quick reference — all resource identifiers

```
Resource Group:      ia-practice1                          (eastus)
App Service Plan:    ASP-iapractice1-8b89                  (Linux, Premium0V3)
Container Registry:  iapracticeregistry.azurecr.io          (Basic, admin enabled)
Web App (this demo): iapractice1-demo                       (agent: iapractice1-demo.azurewebsites.net)
```
