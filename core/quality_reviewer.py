import json
import logging
from dataclasses import dataclass
from core.classifier import call_claude_with_retry, APIUnavailableError

logger = logging.getLogger(__name__)


@dataclass
class QualityResult:
    score: int  # 1-10
    passed: bool
    issues: list[str]
    summary: str


QUALITY_SYSTEM_PROMPT = """Tu esi email atsakymų kokybės tikrintojas. Tavo užduotis — įvertinti ar sugeneruotas atsakymas yra tinkamas siųsti lead'ui.

Vertink pagal šiuos kriterijus:
1. **Tonas** — ar profesionalus, draugiškas, ne per agresyvus?
2. **Relevantumas** — ar atsakymas atitinka tai, ką lead'as parašė?
3. **Tikslumas** — ar nėra prasimanytos informacijos, neteisingų pažadų?
4. **Ilgis** — ar ne per ilgas/trumpas? Email'as turi būti glaustas.
5. **CTA** — ar yra aiškus next step (jei reikia)?
6. **Kalba** — ar nėra gramatikos klaidų, ar natūraliai skamba?

Atsakyk JSON formatu:
{
    "score": <1-10>,
    "issues": ["issue1", "issue2"],
    "summary": "trumpas paaiškinimas"
}

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
            model="claude-sonnet-4-20250514",
            max_tokens=256,
            system=QUALITY_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
        )

        # Parse JSON
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            import re
            match = re.search(r'\{[^{}]*"score"[^{}]*\}', raw, re.DOTALL)
            if match:
                data = json.loads(match.group(0))
            else:
                raise

        score = int(data.get("score", 5))
        issues = data.get("issues", [])
        summary = data.get("summary", "")

        return QualityResult(
            score=score,
            passed=score >= min_score,
            issues=issues,
            summary=summary,
        )

    except APIUnavailableError as e:
        logger.error(f"Quality review failed — API unavailable: {e}")
        return QualityResult(score=0, passed=False, issues=["API unavailable — reply blocked"], summary=f"API error: {e}")

    except Exception as e:
        logger.error(f"Quality review failed: {e}")
        return QualityResult(score=0, passed=False, issues=["quality review failed — reply blocked"], summary=f"Error: {e}")
