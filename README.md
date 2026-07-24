# OpenPISP Payer Stub

A lightweight payer application simulator for testing PISP integrations.

Simulates a payer (bank customer) interacting with a PISP: fetching payment
requests, granting or declining consent, completing bank authentication flows,
viewing payment history, and filing disputes. Useful for automated testing and
interactive demos without a real mobile/web app.

## Quick start

```bash
docker run -p 8000:8000 \
  -e PISP_URL=https://finova.prisac.com \
  -e PAYER_NAME="Alice" \
  ghcr.io/openpisp/payer-stub:latest
```

## Configuration

| Variable | Default | Description |
|---|---|---|
| `PISP_URL` | `http://localhost:8001` | PISP base URL |
| `PAYER_NAME` | `Test Payer` | Display name for the simulated payer |
| `AUTO_APPROVE` | `false` | Auto-approve all payment requests |

## Part of the OpenPISP Reference Implementation

See [openpisp/reference-implementation](https://github.com/openpisp/reference-implementation).

## Roadmap & release history

- **Roadmap** — tracked org-wide on the [OpenPISP Roadmap board](https://github.com/orgs/openpisp/projects/4); no roadmap files live in this repo.
- **Release history** — recorded in `CHANGELOG.md` (Keep a Changelog format; added at first tagged release).
