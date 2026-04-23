import json
import logging
from dataclasses import dataclass
import config
from core.classifier import call_claude_with_retry, APIUnavailableError

logger = logging.getLogger(__name__)


@dataclass
class QualityResult:
    score: int  # 1-10
    passed: bool
    issues: list[str]
    summary: str
    improvement_suggestion: str = ""  # Konkretus pasiulymas ka patobulinti (jei score < 8)


QUALITY_SYSTEM_PROMPT = """Tu esi email atsakymų kokybės tikrintojas. Tavo užduotis - įvertinti ar sugeneruotas atsakymas yra tinkamas siųsti lead'ui.

Vertink pagal šiuos kriterijus:
1. **Tonas** - ar profesionalus, draugiškas, ne per agresyvus?
2. **Relevantumas** - ar atsakymas atitinka tai, ką lead'as parašė?
3. **Tikslumas** - ar nėra prasimanytos informacijos, neteisingų pažadų?
4. **Ilgis** - ar ne per ilgas/trumpas? Email'as turi būti glaustas.
5. **CTA** - ar yra aiškus next step (jei reikia)?
6. **Kalba** - ar nėra gramatikos klaidų, ar natūraliai skamba?
7. **Brūkšniai** - ar naudojami TIK trumpi brūkšniai `-`? (em-dash `—` DRAUDŽIAMAS)

Atsakyk JSON formatu:
{
    "score": <1-10>,
    "issues": ["issue1", "issue2"],
    "summary": "trumpas paaiškinimas",
    "improvement_suggestion": "konkretus pasiulymas ka PATOBULINTI - jei score < 8 butina, jei 8+ palik tuscia"
}

**improvement_suggestion instrukcijos** (tai svarbiausia dalis jei score < 8):
- Konkretus pasiulymas kaip perrasyti atsakyma (1-3 sakiniai)
- Pvz.: "Pakeisk 'Puiku, dziaugiuosi!' i neutralesne pradzia 'Aciu uz atsakyma'. Pasalink paskutini kvalifikavimo klausima - OBJECTION kelias turi baigtis tik invite'u."
- Arba: "Trumpink - is 6 sakiniu palik 3. Ismesk antra paragrafa apie garantija (kartojasi)."
- Arba: "Kainos yra 5,50-10,90 €/m (brief'e), bet tu parasei 'nuo 5 €' - pataisyk i tiksli skaiciu."
- NEKartok 'score' reiksmes cia - tik tiesiogines rekomendacijos

Score reikšmės:
- 8-10: puiku, galima siųsti
- 6-7: priimtina, bet galėtų būti geriau
- 1-5: per prastas, reikia žmogaus peržiūros"""


async def review_quality(
    prospect_message: str,
    classification: str,
    generated_reply: str,
    client_name: str,
    min_score: int = 7,
) -> QualityResult:
    """Review quality of a generated reply. Returns score and pass/fail."""
    user_prompt = f"""Įvertink šį sugeneruotą email atsakymą:

**Klientas:** {client_name}
**Klasifikacija:** {classification}

**Lead'o žinutė:**
{prospect_message}

**Sugeneruotas atsakymas:**
{generated_reply}

Ar šis atsakymas tinkamas siųsti? Įvertink JSON formatu."""

    try:
        raw = await call_claude_with_retry(
            model=config.QUALITY_MODEL,
            max_tokens=512,  # improvement_suggestion gali būti ilgas
            system=QUALITY_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
            cache_system=True,
            purpose="quality_review",
        )

        # Parse JSON - bandyti kelis būdus (markdown code blocks, trailing text)
        import re
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            # Markdown code block
            m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
            if m:
                data = json.loads(m.group(1))
            else:
                # Any JSON object with "score" key (multi-line greedy)
                m = re.search(r"\{[\s\S]*?\"score\"[\s\S]*?\}", raw)
                if m:
                    try:
                        data = json.loads(m.group(0))
                    except json.JSONDecodeError:
                        raise
                else:
                    logger.warning(f"Quality review returned non-JSON: {raw[:200]}")
                    raise

        score = int(data.get("score", 5))
        issues = data.get("issues", [])
        summary = data.get("summary", "")
        improvement = data.get("improvement_suggestion", "") or ""

        return QualityResult(
            score=score,
            passed=score >= min_score,
            issues=issues,
            summary=summary,
            improvement_suggestion=improvement,
        )

    except APIUnavailableError as e:
        logger.error(f"Quality review failed - API unavailable: {e}")
        return QualityResult(score=0, passed=False, issues=["API unavailable - reply blocked"], summary=f"API error: {e}", improvement_suggestion="")

    except Exception as e:
        logger.error(f"Quality review failed: {e}")
        return QualityResult(score=0, passed=False, issues=["quality review failed - reply blocked"], summary=f"Error: {e}", improvement_suggestion="")
