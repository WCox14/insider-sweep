# Insider Buy Portfolio — Daily Sweep

A daily, rules-driven screen of SEC Form 4 open-market purchases. It ranks
insider buys by a 0–100 conviction score and publishes a dashboard.

## Rules in force
- Open-market purchases only (transaction code **P**)
- **$500,000** dollar floor
- Continuous ΔOwnership scoring (no hard gate — small ΔOwn just scores lower)
- Funds / BDCs / SPACs / shells excluded
- Opportunistic vs. routine via EDGAR history (routine = same calendar month 3+ years)
- **Dorsey Wright = 5** momentum gate — applied by hand in the DW column
- Rolling 4-day filing window so late filers are never missed

## Files (each goes at its own path — no zip)
| Path in repo | What it is |
|------|------------|
| `sweep.py` | the engine |
| `requirements.txt` | one dependency (requests) |
| `.github/workflows/daily-sweep.yml` | runs it every morning at 4 AM EDT |
| `docs/index.html` | the dashboard GitHub Pages serves |
| `docs/.nojekyll` | tells Pages to serve the HTML as-is (empty file) |
| `state/seen_accessions.json` | remembers what it has already counted |

## Setup
1. Settings → Secrets and variables → Actions → **Variables** → add
   `SEC_USER_AGENT` = `Your Name your@email.com` (SEC requires this).
2. Settings → **Pages** → Deploy from a branch → `main` / `/docs`.
3. Actions → Daily Insider Sweep → **Run workflow** for the first run.

## Tuning
Edit the `env:` block in the workflow: `DOLLAR_FLOOR`, `LOOKBACK_DAYS`.
