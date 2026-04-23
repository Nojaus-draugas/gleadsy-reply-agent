# Foreign-language reply translation + approval workflow

**Status:** Design approved, ready for implementation plan
**Date:** 2026-04-23
**Target:** `gleadsy-reply-agent` (reply.gleadsy.com)

## Problem

Paulius leidžia cold outreach kampanijas užsienio kalbomis (FR, DE, EE, LV, EN ir kt.). Kai užsieniečiai prospect'ai atsako, reply agent'as negali:

1. **Paduoti Pauliui suprantamos versijos** to, ką parašė prospect'as (visi atsakymai tik originalia kalba)
2. **Leisti Pauliui peržiūrėti ir patvirtinti draftą** prieš siunčiant - dabartinis flow auto-send'ina draftą

Lietuvių kampanijos (gleadsy, ibjoist, puoskio-spauda) veikia gerai su auto-send'u - turi istorinius pavyzdžius, Paulius supranta draftus iš karto. Foreign kampanijos yra naujos, neturi few-shots ir Paulius negali patikrinti drafto be vertimo.

## Goals

- Foreign-language reply'ams būtinas Pauliaus approval prieš išsiunčiant
- Dashboard'e matyti lead'o žinutę + agent'o draftą **abiem kalbom** (LT + original) side-by-side
- Redaguoti draftą duodant **lietuviškas instrukcijas** → Claude perrašo original kalba
- LT kampanijos veikia kaip dabar (nulinis performance'o ar cost'ų pakitimas)
- Backward compatibility: senas YAML formatas, senas auto-send flow

## Non-goals

- Slack-native approval mygtukai (per-maža UI, sunku redaguoti mobile)
- Auto-send pagal quality score (net jei 10/10 - foreign vis vien laukia approval)
- Historical backfill (jau DB'e esančių foreign replies nevertiame retroaktyviai)
- Per-language stylometry profile (atiduota į Fazę 5, vėlesnis vystymasis)

---

## Arkitektūra

### Aukšto lygio flow (foreign language + approval_required=true)

```
Instantly webhook
    ↓
dedup / cooldown / takeover checks
    ↓
classify_reply (Haiku, LT or any)
    ↓
[NEW] detect_language(prospect_message, campaign_hint)
    ↓
[NEW] translate_to_lt(prospect_message)   # jei original != lt
    ↓
generate_reply(..., target_language=detected_lang)
    ↓
quality_review (kaip dabar)
    ↓
[NEW] translate_to_lt(agent_reply)        # jei original != lt
    ↓
log_interaction(approval_status="pending", was_sent=false)
    ↓
notify_slack_approval_pending()
    ↓
[LAUKIA žmogaus dashboard'e]
    ↓
POST /api/approve/{iid}
    ↓
send_reply() via Instantly API
    ↓
update approval_status="sent", was_sent=true
```

LT klientų flow (approval_required=false) nesikeičia.

---

## Komponentai

### 1. DB schema pakeitimai

Nauji migration'ai į `interactions` lentelę (append-only, jokio drop'o):

```sql
ALTER TABLE interactions ADD COLUMN original_language TEXT;         -- ISO code: "lt","fr","de","et","lv","en"
ALTER TABLE interactions ADD COLUMN prospect_message_lt TEXT;       -- LT vertimas (jei original != lt)
ALTER TABLE interactions ADD COLUMN agent_reply_lt TEXT;            -- LT vertimas agent'o drafto
ALTER TABLE interactions ADD COLUMN approval_status TEXT;           -- NULL|"pending"|"approved"|"rejected"|"sent_manually"|"sent"
ALTER TABLE interactions ADD COLUMN approved_at TIMESTAMP;
ALTER TABLE interactions ADD COLUMN approved_by TEXT;               -- "paulius"
ALTER TABLE interactions ADD COLUMN edit_history TEXT;              -- JSON array
ALTER TABLE interactions ADD COLUMN final_sent_text TEXT;           -- faktinis išsiųstas tekstas (jei edit'inta)

CREATE INDEX IF NOT EXISTS idx_interactions_approval ON interactions(approval_status);
```

**Semantika:**
- `approval_status=NULL` → auto-send flow (LT klientams be flag'o)
- `"pending"` → laukia Pauliaus
- `"approved"` → Paulius patvirtino, tarpinis state (sekundės iki send)
- `"sent"` → išsiųsta per Instantly API
- `"sent_manually"` → Paulius copy-pastino, pats pažymėjo kaip išsiųstą
- `"rejected"` → Paulius atmetė, nieko nesiųsta

`was_sent` lieka su ta pačia semantika: true jei faktiškai išsiųsta (sent arba sent_manually).

### 2. Kalbos nustatymas

**Primary: per-campaign YAML config.**

Client YAML schema pasikeičia - `campaigns` tampa dict'ų sąrašu:

```yaml
# Naujas formatas
campaigns:
  - id: "05318bc6-1e24-44f1-ad2a-18ed43b8456b"
    language: "lt"
    name: "Gleadsy LT - IT sektorius"
  - id: "a7b5c9d2-..."
    language: "fr"
    name: "Gleadsy FR - tech agentūros"
```

**Backward compatibility:** `client_loader.py` accept'ina ir seną formatą:

```yaml
campaigns:
  - "05318bc6-1e24-44f1-ad2a-18ed43b8456b"   # string = naudojam tone.language default
```

Jei entry yra string, language = `tone.language` (client-level default).

**Secondary: language detection fallback.**

Jei prospect'o žinutė akivaizdžiai kita kalba (pvz. kampanija FR, bet lead atsako EN):

- Naudojam `langdetect` lib (pip, offline, deterministinis, ~50ms)
- Jei confidence > 0.9 ir detected != campaign_hint → naudojam detected, log'inam warning
- Agent'as atsako ta kalba, kurią lead vartoja (user preference'as)

**Palaikomos kalbos (konstanta):** lt, en, fr, de, et, lv. Nežinoma kalba → fallback į `en` + Slack warning.

### 3. Translation modulis

Nauja failas: `core/translation.py`

```python
async def translate_to_lt(text: str, source_language: str) -> str:
    """Translate to Lithuanian. No-op if source is already 'lt'."""
    if source_language == "lt" or not text.strip():
        return text
    # Claude Haiku call su paprastu prompt'u:
    #   system: "Translate to Lithuanian. Return ONLY translation, no explanations."
    #   user: text
    ...

async def rewrite_draft(original_draft: str, lt_instruction: str,
                         target_language: str, client_config: dict) -> str:
    """Rewrite draft based on LT instruction. Returns new draft in target_language."""
    # Sonnet 4.6 (quality svarbi)
    # system: "Tu esi {client_name} atstovas. Tu jau parašei šį draftą {language} kalba:
    #          <draft>{original_draft}</draft>
    #          Vartotojas nori jį perrašyti pagal lietuviškas instrukcijas.
    #          Perrašyk TOJE PAČIOJE {language} kalboje, išlaikydamas toną ir stilių.
    #          Grąžink TIK naują draftą, be paaiškinimų."
    # user: "Instrukcijos: {lt_instruction}"
    ...
```

Modeliai: translate'ui - `claude-haiku-4-5-20251001`; rewrite'ui - `claude-sonnet-4-6`.

### 4. Pipeline integracija (`webhooks/instantly_webhook.py`)

Modifikuoji `_process_reply()`:

**Po classify, prieš reply branch:**
```python
original_language = detect_language(reply_text, campaign_language_hint)
prospect_message_lt = await translate_to_lt(reply_text, original_language)
```

**Po generate_reply + quality_review:**
```python
agent_reply_lt = await translate_to_lt(agent_reply, original_language)

if client_config.get("approval_required", False):
    iid = await log_interaction(db, {
        ...,
        "approval_status": "pending",
        "was_sent": False,
        "original_language": original_language,
        "prospect_message_lt": prospect_message_lt,
        "agent_reply_lt": agent_reply_lt,
    })
    await notify_approval_pending(iid, lead_email, client_id, classification,
                                    prospect_message_lt, agent_reply_lt)
    return {"status": "pending_approval", "interaction_id": iid}

# else: senas auto-send flow kaip dabar
```

**`generate_reply()` gauna `target_language` parametrą:**
- Prompt'as papildomas: "Reply in {target_language}." direktyva
- Few-shot bank'as filtruojamas `WHERE original_language = ? OR original_language IS NULL` (fallback į generic jei pavyzdžių nėra)

### 5. Dashboard UI

**Naujas endpoint: `GET /pending`**

Rodo visus `approval_status="pending"` draftus, sortuotus pagal senumą. Client filter dropdown. Auto-refresh 30s.

Kiekvienas draftas - kortelė su:
- Lead meta: email, kalba flag, klientas, kampanija, kategorija, quality score, confidence, laukimo laikas
- Lead žinutė: LT vertimas (collapsed "Rodyti originalą" toggle)
- Agent draftas: **side-by-side** LT preview | original (`grid-template-columns: 1fr 1fr`)
- Mygtukai: ✅ Siųsti per Instantly | 📋 Copy tekstą | ✏️ Edit | ❌ Atmesti | 🚫 Human takeover

**Header navigation (`/replies` puslapyje):**
Prideda "⏳ Laukia approval (N)" mygtuką, raudoną jei N > 0. Skaičius realtime iš SQL count.

**`/conversation/<lead>/<campaign>` papildymas:**
Pending draftai rodomi kaip „greyed out" agent žinutės su „⏳ Laukia approval" badge + „Eiti į approval" linku.

**Edit modal:**
- Dabartinis draftas (display-only)
- LT instruction textarea
- 🔄 Perrašyti su Claude → API call → naujas draftas (LT + original side-by-side)
- 💾 Išsaugoti ir uždaryti | 🔄 Bandyti vėl | ✅ Išsaugoti + Siųsti

### 6. API endpoint'ai

| Method | Path | Aprašymas |
|--------|------|-----------|
| GET | `/pending` | HTML puslapis su pending drafts sąrašu |
| POST | `/api/approve/{iid}` | Patvirtinti + siųsti per Instantly → status="sent" |
| POST | `/api/mark_sent/{iid}` | Pažymėti kaip manually išsiųstą → status="sent_manually" |
| POST | `/api/reject/{iid}` | Atmesti draftą → status="rejected" |
| POST | `/api/takeover/{iid}` | Reject + įrašyti į `human_takeovers` lentelę |
| POST | `/api/edit_draft/{iid}` | Rewrite with LT instruction; body: `{"lt_instruction": "..."}` |

Visi endpoint'ai autentikuoti per `_get_dashboard_session` cookie (egzistuojanti logika).

### 7. Slack notifikacijos

Naujas funkcija `core/slack_notifier.py`:

```python
async def notify_approval_pending(
    iid: int, lead_email: str, client_id: str, classification: str,
    quality_score: int, confidence: float,
    prospect_message_lt: str, agent_reply_lt: str, original_language: str,
) -> None:
    ...
```

Žinutės formatas:
```
⏳ Naujas draftas laukia approval

Klientas: gleadsy  |  Lead: pierre.dupont@acme.fr  🇫🇷 FR
Kategorija: INTERESTED  |  Quality: 8/10  |  Confidence: 92%

🗣️ Lead žinutė (LT vertimas):
> {pirmas ~3 sakiniai}

✍️ Agent'o draftas (LT preview):
> {pirmas ~3 sakiniai}...

👉 https://reply.gleadsy.com/pending#draft-{iid}
```

Emoji prefix pagal prioritetą: ⏳ normal, 🔥 INTERESTED arba confirmed time slot, ⚠️ quality < 7.

Re-edit'ų neping'inam (tik pirmą kartą, kai naujas draftas atkeliauja).

### 8. Client YAML schema

```yaml
client_id: "gleadsy-fr"
client_name: "Gleadsy (FR outreach)"

# NEW toggle
approval_required: true     # default false = senas auto-send

# NEW formatas (backward compatible - string entries tebeleidžiami)
campaigns:
  - id: "a7b5c9d2-..."
    language: "fr"
    name: "Gleadsy FR - tech agentūros"
  - id: "b8c6d3e4-..."
    language: "de"
    name: "Gleadsy DE - SaaS startups"

tone:
  language: "fr"            # default jei campaign neturi language
  addressing: "vous"
  sign_off: "Cordialement"
  sender_name: "Paulius"
  personality: |
    Professional, warm, to the point. ...
  max_reply_length_sentences: 5

# ... kiti laukai (company_description, faq, meeting, boundaries) be pakeitimų
```

---

## Edge cases ir sprendimai

### INTERESTED + time confirmation branch
Kai foreign lead patvirtina meeting slot'ą (parse_time_confirmation sėkmingas), auto-book'as IŠJUNGTAS jei `approval_required=true`. Draftas su patvirtinimo email (meet link placeholder'is) eina į pending queue. Paulius patvirtina dashboard'e → tada:
1. `create_meeting_event()` sukuriama
2. `{meet_link}` placeholder pakeičiamas realiu link'u
3. Siunčiama per Instantly API

**Rizika:** momentum loss jei Paulius delay'ina valandoms. **Mitigacija:** Slack notify gauna 🔥 prefix'ą.

### UNSUBSCRIBE / OUT_OF_OFFICE / UNCERTAIN
Jokio approval - šie auto-handl'inami kaip dabar (unsubscribe → blocklist, out-of-office → log, uncertain → escalation). Foreign kalba nekeičia.

### Quality fail
Quality review'as vyksta PRIEŠ approval queue. Jei `quality.passed=false`, eina į escalation šaką (Slack alert, ne approval). Nenorim spamint Pauliaus su žemos kokybės draftais.

### Cooldown + thread limit
Taikomi taip pat kaip dabar. Pending draftas skaitomas `get_thread_reply_count()` - kitaip lead atsakius dar kartą, papildomas draftas kurtųsi kol pirmas dar pending.

**Svarbu:** `get_thread_reply_count()` turi skaičiuoti TIK approved/sent replies, ne pending. Kitaip per ilgai užsibūnant pending queue'je, tolesni lead atsakymai blokuotųsi max_replies threshold'e. *(Pakeitimas funkcijoje: `WHERE was_sent=1 OR approval_status IN ('sent','sent_manually')`)*

### Few-shots per-language
Query modifikacija: `WHERE original_language = ? OR original_language IS NULL`. Naujoms kalboms be pavyzdžių - fallback į generic system prompt (be few-shots).

### Hallucination guard + quality review po edit'o
Po Claude rewrite (edit workflow) - rerun hallucination guard + quality review. Nesiblokuojam ant rezultatų, tik rodom score'ą dashboard'e. Edit'o originalus DB log'e + naujas.

---

## Kainų analizė

Per foreign webhook papildomi Claude call'ai:

| Call | Model | Kaina |
|------|-------|-------|
| Translate prospect → LT | Haiku 4.5 | $0.0024 |
| Translate draft → LT | Haiku 4.5 | $0.0024 |
| Language detection | langdetect (offline) | $0 |
| Edit rewrite (jei) | Sonnet 4.6 | $0.0096 |
| Post-edit re-translate | Haiku 4.5 | $0.0024 |

**Be edit:** ~$0.005/webhook. **Su edit:** ~$0.017/webhook.
50 foreign/dieną, 20% edit'inama → ~$0.37/d ≈ $11/mėn.

LT klientams - $0 extra.

---

## Rollout

**Fazė 1 - Backend + schema (1d):**
- DB migration'ai (`db/migrations.py` arba `db/database.py` MIGRATIONS list)
- `client_loader.py` - abu YAML formatai
- `core/language_detection.py`, `core/translation.py` - nauji moduliai
- `webhooks/instantly_webhook.py` - pipeline branch pagal `approval_required`
- Visi esami klientai: `approval_required: false` (default) → jokio pakeitimo

**Fazė 2 - Dashboard (1d):**
- `GET /pending` + HTML
- `/api/approve|reject|edit_draft|mark_sent|takeover` endpoint'ai
- Edit modal JS (vanilla JS, be framework'o - tęsiam esamą stilių)
- `/replies` header badge + link

**Fazė 3 - Slack notify (kelios h):**
- `core/slack_notifier.py::notify_approval_pending()`

**Fazė 4 - Pirmas foreign klientas:**
- Naujas YAML (pvz. `gleadsy-fr.yaml`) su `approval_required: true`
- Smoke test per gleadsy-self kampanijas
- Paleisti kampaniją, stebėti pirmą savaitę

**Fazė 5 (vėliau) - Auto-learn adaptacija:**
- Few-shot query `WHERE original_language = ?`
- `edit_history` → few-shots (thumbs_up post-edit, thumbs_down pre-edit)
- Per-language stylometry analyzer

---

## Testing

**Unit:**
- `tests/test_language_detection.py` - langdetect + Claude fallback
- `tests/test_translation.py` - mock Claude, prompt format + parsing
- `tests/test_edit_workflow.py` - edit_history JSON, Claude rewrite prompt
- `tests/test_approval_routing.py` - pipeline branch pagal flag
- `tests/test_yaml_backward_compat.py` - senas + naujas campaigns formatas

**Integration:**
- E2E FR webhook → pending → approve → send (Instantly mocked)
- E2E edit → save → send
- Cooldown / thread_count skaičiuoja tik sent, ne pending

**Manual smoke:**
- `approval_required: true` `gleadsy-self` klientui
- Išsiunčia sau FR test email į Instantly
- Stebi: Slack ping → `/pending` → edit modal → approve → Instantly send

---

## Open questions to revisit

- **Fazė 5 (auto-learn per-language):** kada pradėti? Po N pavyzdžių konkreti kalba, ar visada fallback į generic? Turbūt reikia `MIN_FEWSHOTS_PER_LANG=5` threshold'o.
- **Edit_history kaip mokymosi signalai:** ar pre-edit draft'ą žymėti `thumbs_down` automatiškai? Rizika: ne visi edit'ai reiškia, kad draftas buvo blogas - kartais tiesiog nori pridėti papildomą sakinį. Turbūt reikia „big edit" detektoriaus (similarity < 0.7).
- **Slack kanalo separacija:** ar foreign approval'us rodyti atskirame Slack kanale (`#gleadsy-approvals`) vs esamo? Priklauso nuo dienos volume'o - jei >20/d, verta atskirti.

Šie klausimai nekritiniai MVP'ui ir gali būti adresuoti post-launch.
