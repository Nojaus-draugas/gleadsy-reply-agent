# Smoke Test Checklist - Foreign Reply Approval Flow

## Setup (one-time)

1. Pick a small, controlled FR Instantly campaign UUID (one you own, e.g. test leads only)
2. Rename `clients/_gleadsy-fr-smoke.yaml` to `clients/gleadsy-fr-smoke.yaml`
3. Replace `REPLACE-WITH-REAL-FR-CAMPAIGN-UUID` with the real campaign UUID
4. Deploy: `./deploy.sh` or equivalent
5. Verify boot logs: `Klientai: gleadsy, ibjoist, puoskio-spauda, gleadsy-fr-smoke`

## Test 1: New draft enters pending queue

1. Send yourself a test email from an alias to that FR campaign, replying in French:
   > "Bonjour, je suis interesse. Quels sont vos prix?"
2. Within 30s, expect Slack notification with uptime prefix, FR flag, LT preview
3. Open `https://reply.gleadsy.com/pending`
4. Verify: draft card shows lead email, FR flag, LT+FR side-by-side
5. Verify `/replies` header shows "Laukia approval (1)" in red

## Test 2: Edit workflow

1. Click edit on the draft
2. Enter LT instruction: "Pridej klausima, ar jie jau bandė cold outreach"
3. Click regenerate with Claude
4. Verify new FR draft contains a question about cold outreach
5. Verify LT preview also updated

## Test 3: Approval sends via Instantly

1. Click send via Instantly
2. Verify response 200, draft disappears from pending queue
3. Check Instantly UI - email actually sent?
4. Check `/replies` - draft now shows "sent" with was_sent=1, approval_status=sent
5. Check `/conversation/<lead_email>/<campaign_id>` - full thread visible

## Test 4: Reject does not send

1. Send another FR test email
2. On `/pending`, click reject
3. Verify draft disappears, approval_status=rejected, was_sent=0
4. Verify Instantly did NOT send anything

## Test 5: LT auto-send still works

1. Send LT test email to gleadsy client's LT campaign
2. Verify: auto-sent without pending queue, appears in /replies with was_sent=1
3. Slack notification uses old "notify_reply_sent" format (not approval_pending)

## Cleanup

- After smoke test, either leave the smoke YAML in place or remove it
- Document smoke test result + timestamp in a followup commit message
