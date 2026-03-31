# gleadsy-reply-agent/prompts/templates.py

def format_few_shots(examples: list[dict]) -> str:
    if not examples:
        return ""
    lines = ["## PAVYZDŽIAI (geri atsakymai):"]
    for i, ex in enumerate(examples, 1):
        lines.append(f"\nPavyzdys {i}:")
        lines.append(f"Prospektas: \"{ex['prospect_message']}\"")
        lines.append(f"Atsakymas: \"{ex['agent_reply']}\"")
    return "\n".join(lines)


def format_anti_patterns(patterns: list[dict]) -> str:
    if not patterns:
        return ""
    lines = ["## KO NEDARYTI (blogi pavyzdžiai):"]
    for i, p in enumerate(patterns, 1):
        lines.append(f"\nBlogas pavyzdys {i}:")
        lines.append(f"Prospektas: \"{p['prospect_message']}\"")
        lines.append(f"❌ Blogas: \"{p['bad_reply']}\"")
        lines.append(f"✅ Teisingas: \"{p['correct_reply']}\"")
        if p.get("feedback_note"):
            lines.append(f"Pastaba: {p['feedback_note']}")
    return "\n".join(lines)


def format_faq_list(faq: list[dict]) -> str:
    lines = []
    for i, item in enumerate(faq):
        lines.append(f"[{i}] Q: {item['question']}")
        lines.append(f"    A: {item['answer'].strip()}")
    return "\n".join(lines)


def format_slots_for_prompt(slots: list[dict]) -> str:
    if not slots:
        return "Laisvų laikų šiuo metu nėra — pasiūlyk prospektui pasirinkti jam patogų laiką."
    parts = []
    for s in slots:
        parts.append(f"{s['day_name']} ({s['date']}) {s['time']}")
    return "Galimi laikai susitikimui: " + ", arba ".join(parts)
