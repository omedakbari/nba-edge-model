# web/: Prop Research App front end (Phase 3)

A Next.js app that installs on the phone as a PWA (Add to Home Screen). For now it reads **only the synthetic fixture** in `fixtures/sample-slate.json`, which has made-up players, lines and prices. The repo is public, so no real book data is committed.

It has no database connection, API keys, environment variables or external requests. It connects to Supabase only after the Phase 2 gate passes, through a read-only database role (see `docs/BUILD_PLAN.md`).

| Path | What |
|---|---|
| `app/page.tsx` | Tonight: the list of players |
| `app/player/[id]/page.tsx` + `components/PlayerView.tsx` | Player page: stat tabs, line stepper, over/under, line chips with age, L5/L10/L15/season/H2H cards, home/away and minutes filters, last-15 chart |
| `lib/hitRates.ts` | Hit-rate, consensus-line and no-vig maths: a port of `worker/prop_worker/hitrates.py` (used by `prop_worker.slate`). Both run the same golden cases, `worker/tests/fixtures/shared/hit_rates_cases.json` |
| `app/manifest.ts`, `app/icon.tsx`, `app/apple-icon.tsx` | PWA manifest and generated icons. No service worker yet: cached stale prices would be worse than none |

Commands (from `web/`):

```
npm ci            # install exact versions from package-lock.json
npm test          # vitest: shared golden cases + fixture checks
npm run typecheck
npm run build
npm run dev       # http://localhost:3000; on the phone use your Mac's LAN address
```

These web tests run locally only. CI (`.github/workflows/tests.yml`) runs the Python tests; adding a web job is a workflow change, so it waits until after the gate.
