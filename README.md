# Missing Parts Monitor — Cloud (GitHub Actions → Fitch Board)

This is the missing-parts check, moved off your Mac and off Claude. It runs by
itself on GitHub's free servers, on a schedule, and instead of posting to Teams
it now feeds a dedicated **Missing Parts** page on the Fitch Board
(`fitch-board.pages.dev/missing-parts.html`, also linked from the board's
**Tools** menu).

The detection logic is unchanged. What changed: *where* it runs (the cloud, not
Claude) and *where it reports* (the board, not Teams). On the board page every
job short of parts shows up under three headings — **No PO / Partial / On
order** — and each one has a **shared comment box** anyone can edit to note when
the parts are expected. The hourly run never overwrites those comments, and a
job drops off the page automatically once its parts are sorted.

Once live, the hourly run uses **no Claude usage**.

There are two halves to set up: **A)** deploy the board update (one time), and
**B)** the scheduled job. About 25 minutes total.

> **Security:** the GitHub repo for the job must be **Private**. Credentials
> only ever go in GitHub's encrypted *Secrets* screen — never in the code. The
> `.gitignore` here already blocks `.env`-type files.

---

## Part A — Deploy the board update

The board changes (the new Missing Parts page, its API, and a new table) live in
your existing `fitch-board` project. They need to go live before the job can
post to them.

1. **Publish the code.** Push the `fitch-board` folder changes the same way you
   normally update the board (it auto-deploys on push). The new/changed files
   are: `public/missing-parts.html`, `functions/api/missing-parts/*`,
   `public/index.html` (the new Tools link), and `schema.sql`.
2. **Create the new table** (one time) by running, from inside the `fitch-board`
   folder:
   ```
   npx wrangler d1 execute fitch-board --remote --file=migrations/2026-06-16_missing_parts.sql
   ```
3. That's it — the board already has its `BOARD_API_KEY` secret (the same key
   the board's other automations use), so nothing else to configure there.

I'll walk you through this part live — it's the only bit that touches Cloudflare.

## Part B — The scheduled job

### 1. Create a private GitHub repository
1. Go to <https://github.com/new>.
2. Name it `missing-parts-cloud`. Set it to **Private**. Create it.

### 2. Upload the job files
1. On the empty repo page, click **"uploading an existing file."**
2. Drag in everything **inside this `missing-parts-cloud` folder** —
   `monitor.py`, `bc_api.py`, `requirements.txt`, `.gitignore`, `README.md`,
   **and the `.github` folder** (it holds the schedule).
3. **Commit changes.**

### 3. Add the secrets
In the repo: **Settings → Secrets and variables → Actions → New repository
secret.** Add these five:

| Secret name | Value |
|---|---|
| `BC_TENANT_ID` | the `BC_TENANT_ID=` line in `OData Integration/.env` |
| `BC_CLIENT_ID` | the `BC_CLIENT_ID=` line in the same `.env` |
| `BC_CLIENT_SECRET` | the `BC_CLIENT_SECRET=` line in the same `.env` |
| `BOARD_URL` | `https://fitch-board.pages.dev` |
| `BOARD_API_KEY` | the board API key — from `fitch-board/.dev.vars` (`BOARD_API_KEY=…`) |

Copy only the part **after the `=`**, no quotes, no spaces.

### 4. Test it
1. In the repo open the **Actions** tab → enable workflows if asked.
2. Click **Missing Parts Monitor** → **Run workflow**.
3. **Safe check first:** tick **Dry run**, Run. Open the run → `run` job → expand
   the log. You want `Target jobsheets: N …` and no errors (this proves it
   reached Business Central; it writes nothing and posts nothing).
4. **Real run:** Run workflow again, Dry run **unticked**. Then open
   **fitch-board.pages.dev/missing-parts.html** (or Tools → Missing Parts) — the
   jobs short of parts should be listed.

### 5. Switch off the old Claude trigger
Once the board page looks right, tell me and I'll disable the old hourly Claude
scheduled task so the work isn't done twice.

---

## Good to know

- **Schedule:** hourly, ~07:00–18:00 UK, Mon–Sat (the `cron` line in
  `.github/workflows/missing-parts.yml`).
- **Self-clearing:** the job sends the full current picture every run, so a job
  whose parts arrive simply disappears from the page next run. Comments are
  preserved for jobs that are still waiting.
- **Comments:** anyone logged into the board can edit the "expected arrival"
  box; it shows who updated it and when. The hourly sync never touches it.
- **Cost:** free (well within GitHub's private-repo Actions allowance).
- **If a run fails,** GitHub emails you; the Actions log shows why (almost always
  a mistyped secret). A failed board post does **not** affect the Business
  Central updates — those still happen.
- **The BC credentials are app-only** (a machine login), so the job runs
  unattended with no human sign-in. That's why this works in the cloud.
