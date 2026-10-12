"""LLM provider abstraction.

Every provider has the same tiny interface:

    client.complete(system_prompt, user_message) -> str   # raw text, hopefully JSON

The triage pipeline (core/triage.py) does the JSON validation, so providers stay
simple. Choose a provider with the LLM_PROVIDER environment variable:

    mock       (default) – offline keyword logic, no API key needed
    anthropic  – Claude API            (needs ANTHROPIC_API_KEY)
    gemini     – Google Gemini API     (needs GEMINI_API_KEY, has a free tier)
    groq       – Groq API              (needs GROQ_API_KEY, has a free tier)

The real SDKs are imported only inside the class that needs them, so you only
have to `pip install` the one you actually use.
"""

from __future__ import annotations

import json
import os
import re
from abc import ABC, abstractmethod

from dotenv import load_dotenv

load_dotenv()


class LLMError(Exception):
    """Raised when a provider cannot return a usable answer."""


class LLMClient(ABC):
    name: str = "base"

    @abstractmethod
    def complete(self, system_prompt: str, user_message: str) -> str:
        """Send the prompt and return the model's raw text reply."""


# JSON Schema matching core.models.LLMTriageOutput. Used by providers that can
# force JSON output. Pydantic still validates the reply afterwards.
TRIAGE_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "detected_language": {"type": "string"},
        "english_translation": {"type": "string"},
        "chief_complaint": {"type": "string"},
        "symptoms": {"type": "array", "items": {"type": "string"}},
        "duration": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "self_reported_severity": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
        "red_flags_mentioned": {"type": "array", "items": {"type": "string"}},
        "suggested_urgency": {"type": "string", "enum": ["RED", "ORANGE", "YELLOW", "GREEN"]},
        "reasoning": {"type": "string"},
        "questions_for_nurse": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "detected_language", "english_translation", "chief_complaint", "symptoms",
        "duration", "self_reported_severity", "red_flags_mentioned",
        "suggested_urgency", "reasoning", "questions_for_nurse",
    ],
    "additionalProperties": False,
}


# ===========================================================================
# 1. MOCK provider – works fully offline for demos and tests
# ===========================================================================
# Very small keyword lists. This is NOT real AI – it only makes the demo realistic.
_LANGUAGE_MARKERS = {
    "isiZulu": {"ngiya", "kakhulu", "futhi", "izinsuku", "ubuhlungu", "isifuba", "ikhanda",
                "angikwazi", "ngikhwehlela", "ngijuluka", "kusukela", "izolo", "isisu",
                "ngiphefumula", "kanzima", "eziyishumi", "isisindo", "sehlile"},
    # Sepedi is listed before Sesotho: they share words like "ke", but Sepedi uses
    # "go"/"ga"/"kudu" where Sesotho uses "ho"/"ha"/"haholo".
    "Sepedi": {"ke", "go", "ga", "kudu", "gabotse", "palelwa", "bolawa", "mala", "hlogo",
               "sehuba", "maabane", "bošego", "bjo", "fifere", "gohlola", "nyaka", "opša",
               "tšwa", "hema", "dihlare", "swele", "fokola"},
    "Sesotho": {"ke", "ho", "haholo", "bohloko", "matsatsi", "ngwana", "feberu", "hlooho",
                "mokgohlane", "letho", "mme", "robetse", "sefuba", "mararo", "mabedi", "nwe"},
    "Afrikaans": {"ek", "het", "nie", "baie", "pyn", "seer", "vir", "kom", "net", "voel",
                  "goed", "dit", "daar", "met", "hoes", "koors", "pille", "bloeddruk",
                  "gebrand", "kookwater", "dae", "weke", "blase"},
    "English": {"i", "the", "and", "have", "is", "since", "pain", "feel", "for", "with",
                "it", "my", "am", "of", "to"},
}

# English symptom name -> keywords in any of the four languages (matched as substrings).
_SYMPTOM_KEYWORDS = {
    "chest pain": ["chest pain", "isifuba", "sefuba", "sehuba", "borspyn"],
    "difficulty breathing": ["breath", "phefumula", "ho hema", "go hema", "asem"],
    "cough": ["cough", "khwehlela", "kgohlela", "gohlola", "hoes"],
    "fever": ["fever", "temperature", "imfiva", "feberu", "fifere", "koors"],
    "headache": ["headache", "ikhanda", "hlooho", "hlogo", "hoofpyn"],
    "sweating": ["sweat", "juluka", "fufulelwa", "sweet"],
    "weight loss": ["weight", "isisindo", "gewig"],
    "bleeding": ["bleed", "blood", "opha", "igazi", "madi", "bloei"],
    "abdominal pain": ["stomach", "abdominal", "isisu", "mpa", "mala", "maag"],
    "burn": ["burn", "boiling", "shile", "tjhele", "swele", "gebrand", "kookwater"],
    "blisters": ["blister", "blase"],
    "flu-like symptoms": ["flu", "mkhuhlane", "mokgohlane", "griep"],
    "runny nose": ["runny nose", "neus loop", "ikhala"],
    "sore throat": ["sore throat", "throat", "keelseer", "umphimbo", "mometso"],
    "not drinking": ["not drinking", "ha a nwe", "ga a nwe", "akaphuzi", "drink nie"],
    "drowsiness": ["drowsy", "sleepy", "robetse haholo", "robala kudu", "lele kakhulu", "lomerig"],
    "wheezing": ["wheez"],
    "reduced fetal movement": ["moving less", "not moving", "ayinyakazi", "sisinyehe", "beweeg nie"],
    "repeat medication request": ["repeat", "medication", "pills", "herhaalvoorskrif",
                                  "pille", "amaphilisi", "dipilisi", "dihlare"],
}

_DANGER_SYMPTOMS = {"chest pain", "difficulty breathing", "bleeding", "not drinking",
                    "drowsiness", "reduced fetal movement"}
_MILD_SYMPTOMS = {"runny nose", "sore throat", "flu-like symptoms", "cough", "headache",
                  "repeat medication request"}

_DURATION_PATTERNS = [
    r"\bsince (?:this morning|last night|yesterday|this week|last week)\b",
    r"\b(?:\d+|a|an|one|two|three|four|five|six|seven|ten)\s+"
    r"(?:hours?|days?|weeks?|months?|years?|dae|dag|weke|maande)\b",
    r"\bizinsuku\s+\w+",
    r"\bmatsatsi\s+a\s+\w+",
]


class MockLLMClient(LLMClient):
    """Pretends to be an LLM using simple keyword rules. Fully offline."""

    name = "mock"

    def complete(self, system_prompt: str, user_message: str) -> str:
        text = _extract_tag(user_message, "patient_text")
        age_match = re.search(r"- Age: (\d+)", user_message)
        age = int(age_match.group(1)) if age_match else 30
        pregnant = "- Pregnant: Yes" in user_message
        lowered = text.lower()

        language = self._detect_language(lowered)
        symptoms = [name for name, words in _SYMPTOM_KEYWORDS.items()
                    if any(w in lowered for w in words)]
        severity = self._severity(lowered)
        urgency = self._urgency(symptoms, language, age, pregnant, severity)

        if language == "English":
            translation = text
        else:
            translation = ("[Mock mode – no real translation offline] Key symptoms detected: "
                           + (", ".join(symptoms) if symptoms else "none recognised"))

        result = {
            "detected_language": language,
            "english_translation": translation,
            "chief_complaint": symptoms[0] if symptoms else "Unclear – see original text",
            "symptoms": symptoms,
            "duration": self._duration(lowered),
            "self_reported_severity": severity,
            "red_flags_mentioned": [s for s in symptoms if s in _DANGER_SYMPTOMS],
            "suggested_urgency": urgency,
            "reasoning": (
                f"Mock AI: patient reports {', '.join(symptoms[:3]) or 'symptoms not recognised offline'}. "
                f"Suggested {urgency} using simple keyword logic."
            ),
            "questions_for_nurse": self._questions(symptoms, pregnant, age),
        }
        return json.dumps(result)

    @staticmethod
    def _detect_language(lowered: str) -> str:
        words = re.findall(r"\w+", lowered)
        scores = {lang: sum(w in markers for w in words)
                  for lang, markers in _LANGUAGE_MARKERS.items()}
        # isiZulu verbs often start with "ngi" (I ...)
        scores["isiZulu"] += sum(w.startswith("ngi") for w in words)
        best = max(scores, key=scores.get)
        return best if scores[best] > 0 else "English"

    @staticmethod
    def _severity(lowered: str) -> int | None:
        match = re.search(r"\b(\d{1,2})\s*(?:/|out of)\s*10\b", lowered)
        if match and 1 <= int(match.group(1)) <= 10:
            return int(match.group(1))
        return None

    @staticmethod
    def _duration(lowered: str) -> str | None:
        for pattern in _DURATION_PATTERNS:
            match = re.search(pattern, lowered)
            if match:
                return match.group(0)
        return None

    @staticmethod
    def _urgency(symptoms, language, age, pregnant, severity) -> str:
        found = set(symptoms)
        if found & _DANGER_SYMPTOMS:
            # The mock is deliberately less confident outside English, which shows
            # why the rules engine is needed as a safety net.
            urgency = "RED" if language == "English" else "ORANGE"
        elif found and found <= _MILD_SYMPTOMS and (severity or 0) <= 4:
            urgency = "GREEN"
        else:
            urgency = "YELLOW"

        if severity is not None and severity >= 8 and urgency in ("GREEN", "YELLOW"):
            urgency = "ORANGE"
        if (age < 5 or pregnant) and urgency == "GREEN":
            urgency = "YELLOW"
        return urgency

    @staticmethod
    def _questions(symptoms, pregnant, age) -> list[str]:
        questions = []
        if pregnant:
            questions.append("How many weeks pregnant is the patient?")
        if "fever" in symptoms:
            questions.append("Has the temperature been measured today?")
        if "cough" in symptoms:
            questions.append("Any blood when coughing, night sweats or weight loss?")
        if age < 5:
            questions.append("Is the child drinking, feeding and passing urine normally?")
        if not questions:
            questions.append("When did this start, and is it getting worse?")
        return questions[:3]


class BrokenMockClient(LLMClient):
    """Always returns invalid output. Used in tests and the 'simulate AI failure' demo."""

    name = "mock-broken"

    def complete(self, system_prompt: str, user_message: str) -> str:
        return "Sorry, I am not able to answer in JSON right now."


def _extract_tag(message: str, tag: str) -> str:
    match = re.search(rf"<{tag}>\s*(.*?)\s*</{tag}>", message, re.DOTALL)
    return match.group(1) if match else message


# ===========================================================================
# 2. ANTHROPIC (Claude) provider
# ===========================================================================
class AnthropicClient(LLMClient):
    name = "anthropic"

    def __init__(self):
        try:
            import anthropic  # imported here so mock mode works without the package
        except ImportError as e:
            raise LLMError("Run `pip install anthropic` to use LLM_PROVIDER=anthropic") from e

        if not os.getenv("ANTHROPIC_API_KEY"):
            raise LLMError("LLM_PROVIDER=anthropic but ANTHROPIC_API_KEY is not set in .env")
        self._anthropic = anthropic
        self.client = anthropic.Anthropic(max_retries=2, timeout=60.0)
        self.model = os.getenv("ANTHROPIC_MODEL", "claude-opus-5-5")

    def complete(self, system_prompt: str, user_message: str) -> str:
        try:
            response = self.client.beta.messages.create(
                model=self.model,
                max_tokens=4000,
                system=system_prompt,
                messages=[{"role": "user", "content": user_message}],
                output_config={
                    # This is a short structuring task, so low effort keeps it fast and cheap.
                    "effort": "low",
                    # Structured outputs: the reply is guaranteed to match this JSON schema.
                    "format": {"type": "json_schema", "schema": TRIAGE_JSON_SCHEMA},
                },
                # If a safety filter declines the request, the API retries on a
                # fallback model automatically instead of returning nothing.
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
        except self._anthropic.APIConnectionError as e:
            raise LLMError(f"Could not reach the Claude API: {e}") from e
        except self._anthropic.RateLimitError as e:
            raise LLMError("Claude API rate limit reached – try again shortly.") from e
        except self._anthropic.APIStatusError as e:
            raise LLMError(f"Claude API error {e.status_code}: {e.message}") from e

        if response.stop_reason == "refusal":
            raise LLMError("The model declined to process this text.")
        text = next((b.text for b in response.content if b.type == "text"), None)
        if text is None:
            raise LLMError("Claude returned no text.")
        return text


# ===========================================================================
# 3. GEMINI provider (free tier available)
# ===========================================================================
class GeminiClient(LLMClient):
    name = "gemini"

    def __init__(self):
        try:
            from google import genai  # pip install google-genai
        except ImportError as e:
            raise LLMError("Run `pip install google-genai` to use LLM_PROVIDER=gemini") from e

        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            raise LLMError("LLM_PROVIDER=gemini but GEMINI_API_KEY is not set in .env")
        self._genai = genai
        self.client = genai.Client(api_key=api_key)
        self.model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

    def complete(self, system_prompt: str, user_message: str) -> str:
        try:
            response = self.client.models.generate_content(
                model=self.model,
                contents=user_message,
                config=self._genai.types.GenerateContentConfig(
                    system_instruction=system_prompt,
                    response_mime_type="application/json",
                    temperature=0.1,
                ),
            )
        except Exception as e:  # the Gemini SDK has many error types; keep it simple
            raise LLMError(f"Gemini API error: {e}") from e
        if not response.text:
            raise LLMError("Gemini returned no text.")
        return response.text


# ===========================================================================
# 4. GROQ provider (free tier available)
# ===========================================================================
class GroqClient(LLMClient):
    name = "groq"

    def __init__(self):
        try:
            from groq import Groq  # pip install groq
        except ImportError as e:
            raise LLMError("Run `pip install groq` to use LLM_PROVIDER=groq") from e

        api_key = os.getenv("GROQ_API_KEY")
        if not api_key:
            raise LLMError("LLM_PROVIDER=groq but GROQ_API_KEY is not set in .env")
        self.client = Groq(api_key=api_key)
        self.model = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")

    def complete(self, system_prompt: str, user_message: str) -> str:
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_message},
                ],
                response_format={"type": "json_object"},
                temperature=0.1,
            )
        except Exception as e:
            raise LLMError(f"Groq API error: {e}") from e
        text = response.choices[0].message.content
        if not text:
            raise LLMError("Groq returned no text.")
        return text


# ===========================================================================
# Factory
# ===========================================================================
_PROVIDERS = {
    "mock": MockLLMClient,
    "anthropic": AnthropicClient,
    "gemini": GeminiClient,
    "groq": GroqClient,
}


def get_provider_name() -> str:
    return os.getenv("LLM_PROVIDER", "mock").strip().lower() or "mock"


def get_llm_client(provider: str | None = None) -> LLMClient:
    """Create the client chosen by LLM_PROVIDER (or the `provider` argument)."""
    provider = (provider or get_provider_name()).lower()
    if provider not in _PROVIDERS:
        raise LLMError(f"Unknown LLM_PROVIDER '{provider}'. Use one of: {', '.join(_PROVIDERS)}")
    return _PROVIDERS[provider]()
