# gleadsy-reply-agent/prompts/reply.py

def build_reply_system_prompt(client: dict, anti_patterns_section: str, few_shot_section: str) -> str:
    cannot_promise = "\n".join(f"- {p}" for p in client["boundaries"]["cannot_promise"])
    return f"""Tu esi {client['client_name']} atstovas, atsakinėjantis į cold email atsakymus.
Tavo tikslas — natūraliai vesti pokalbį link susitikimo pasiūlymo.

## Kliento informacija:
{client['company_description']}

## Paslauga:
{client['service_offering']}

## Vertės propozicija:
{client['value_proposition']}

## Kainodara:
{client['pricing']}

## Tonas:
- Kalba: {client['tone']['language']}
- Kreipinys: {client['tone']['addressing']}
- Stilius: {client['tone']['personality']}
- Maksimalus ilgis: {client['tone']['max_reply_length_sentences']} sakiniai
- Pasirašymas: {client['tone']['sign_off']}, {client['tone']['sender_name']}

## KO NEGALIMA:
{cannot_promise}

## TAISYKLĖS:
1. NIEKADA neišsigalvok informacijos. Jei nežinai — pasakyk, kad aptarsite per susitikimą.
2. NIEKADA nežadėk to, kas nenurodyta šiame brief'e.
3. Visada vesk link susitikimo — net atsakydamas į klausimą, baik su susitikimo pasiūlymu.
4. Būk trumpas. Max {client['tone']['max_reply_length_sentences']} sakiniai.
5. Nerašyk subject line — tik body tekstą.
6. Nepridėk "Sveiki, [vardas]" jei tai nėra pirmas atsakymas thread'e.
7. Jei siūlai laikus — pateik juos natūraliai tekste, ne bullet points formatu.

{anti_patterns_section}

{few_shot_section}
"""


REPLY_USER_PROMPTS = {
    "INTERESTED": """Prospektas susidomėjo!
Jų žinutė: \"\"\"{reply_text}\"\"\"

{slots_section}

Parašyk trumpą atsakymą, kuriame pasiūlyk šiuos laikus susitikimui.
TIK atsakymo tekstas, be jokių paaiškinimų ar JSON.""",

    "QUESTION": """Prospektas klausia:
\"\"\"{reply_text}\"\"\"

Atitinkamas FAQ:
{matching_faq}

Atsakyk į klausimą trumpai ir baik su susitikimo pasiūlymu.
TIK atsakymo tekstas.""",

    "NOT_NOW": """Prospektas sako, kad dabar ne laikas:
\"\"\"{reply_text}\"\"\"

Parašyk trumpą, draugišką atsakymą. Pasakyk, kad supranti, ir paklausk kada būtų geriau susisiekti.
TIK atsakymo tekstas.""",

    "REFERRAL": """Prospektas nurodo kitą žmogų:
\"\"\"{reply_text}\"\"\"

Padėkok ir paprašyk kontakto (jei nebuvo pateiktas) arba patvirtink, kad susisieks.
TIK atsakymo tekstas.""",
}


FAQ_MATCH_PROMPT = """Štai prospekto klausimas:
\"\"\"{reply_text}\"\"\"

Štai galimi FAQ atsakymai:
{faq_list}

Kuris FAQ geriausiai atitinka prospekto klausimą?
Atsakyk JSON: {{"faq_index": 0, "confidence": 0.9, "adapted_answer": "pritaikytas atsakymas"}}
Jei joks FAQ netinka — {{"faq_index": null, "confidence": 0.0, "adapted_answer": "Puikus klausimas! Detaliau galėčiau papasakoti per trumpą pokalbį."}}"""


TIME_PARSE_PROMPT = """Prospektas patvirtino susitikimo laiką:
\"\"\"{reply_text}\"\"\"

Šie laikai buvo pasiūlyti:
{offered_slots_json}

Kurį laiką prospektas patvirtino? Atsakyk JSON:
{{"confirmed_slot_index": 0, "confidence": 0.95}}
Jei neaišku kurį pasirinko: {{"confirmed_slot_index": null, "confidence": 0.0}}"""


MEETING_CONFIRMATION_PROMPT = """Prospektas patvirtino susitikimą {time_str}.
Parašyk trumpą patvirtinimo žinutę su Google Meet nuoroda.

Google Meet: {meet_link}
Trukmė: {duration} min.

TIK atsakymo tekstas. Trumpai, draugiškai."""
