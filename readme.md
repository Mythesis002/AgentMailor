# AgentMailor: AI Call Agent Outreach Engine

Finds local businesses that lose customers to missed phone calls, scores how much they need an AI call agent,
and emails only the good fits with a short, honest, personalised pitch (who we are, what the agent does, a demo offer).

## How it picks businesses (fit score, minimum `SCORE_MIN`, default 6)

| Signal | Points |
|---|---|
| Phone-dependent category (dentist, clinic, vet, salon, hotel, real estate, car repair, home services = 3; restaurant, gym, lawyer, school, venue, lab, travel = 2) | 2-3 |
| No website at all (phone is the only front door) | +3 |
| Website has no live chat | +2 |
| Website has no online booking | +1 |
| Phone number is the main contact (tel link, no form) | +1 |
| Google listing shows closed days or early closing (after-hours missed calls) | +2 |
| 30+ reviews / 100+ reviews (busy phone line) | +1 / +2 |

Businesses without a phone number, chains, and anything below the minimum score are never emailed.
The email hook uses only a fact the bot actually measured (never invented).

## Setup

Repo **Secrets**: `EMAIL_USER`, `EMAIL_PASS` (Gmail app password), `GEMINI_API_KEY` (optional), `RAPIDAPI_KEY` (optional).
Repo **Variables** (all optional, defaults in `mailor.py`): `COMPANY_ADDRESS` (required to email US businesses),
`SENDER_NAME`, `COMPANY_NAME`, `COMPANY_PHONE`, `COMPANY_WEBSITE`, `COMPANY_WHO`, `PRODUCT_FEATURES` (pipe-separated list of what your agent really does), `SCORE_MIN`.

**Set these three Variables, they decide your reply rate** (the bot never invents them, so write only true lines):
- `DEMO_PHONE`: a number owners can call to hear the AI receptionist answer. Then the email's call to action is "call this and try it" instead of "reply to book a meeting".
- `PROOF_LINE`: one true sentence of proof, e.g. `It already answers calls for 6 clinics in Delhi.`
- `PRICE_LINE`: one true sentence about price, e.g. `Plans start at Rs 2,999 a month.`

Test first: Actions > run workflow with `dry_run = true`. Drafts are printed in the log and nothing is sent.

## Safety features
- Warm-up: 3 emails/day, then 5, 8, then `DAILY_LIMIT`.
- Auto-pause if the bounce rate is high.
- Replies like "no" / "unsubscribe" are detected and added to `suppression.txt` automatically.
- `List-Unsubscribe` header, postal address in the signature, and a one-line opt-out in every email.
- Canada is blocked by default; US leads are skipped unless `COMPANY_ADDRESS` is set; in the UK/EU only company-domain
  addresses are used (never gmail/yahoo etc.).

## Every email is unique and spam-checked
- Gemini writes each email from scratch with a random style, hook, feature mix and closing; it is told to avoid the wording of the last 5 emails.
- Before sending, every email (Gemini or template) must pass a local check: no spammy words, no links, no placeholders, sane length, normal capitalisation, subject not reused.
- Each email is compared with the last 40 sent (`recent_emails.json`); if it overlaps more than `SIM_MAX` (default 40%) it is rewritten.
- If Gemini is unavailable, a template assembled from thousands of part combinations is used instead (never one fixed text).
- On startup the bot warns if your sending domain has no SPF/DMARC record. Wording helps, but domain setup (SPF, DKIM, DMARC), warm-up and low bounce rate decide inbox placement.
