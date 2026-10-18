"""Which language LUMA answers in.

THE RULE: the answer language is the one the student SELECTED in the interface. It does not depend on the language the question is
asked in. It changes only when the student
  * changes the selector, or
  * explicitly asks for another language ("explain this in Urdu", "speak German", "اردو میں سمجھائیں", "auf Deutsch bitte").

This module also holds the cheap, deterministic language/script check used by the guardrails (no model call, microseconds).
"""
import re

NAMES = {"en": "English", "ur": "Urdu", "ar": "Arabic", "fa": "Persian", "de": "German"}

# ------------------------------------------------------------------------------------------------ explicit language requests
# One entry per language: every way of naming it that a student would type or say (English, Roman Urdu, native script).
_ALIASES = {
    "en": r"english|inglish|angrezi|angrezi|انگریزی|انگلش|انگلیش|انگلیسی|الإنجليزية|الانجليزية|الانجليزي|الإنجليزي|englisch|अंग्रेज़ी|अंग्रेजी|इंग्लिश",
    "ur": r"urdu|اردو|الأردية|الاردية|أردو|उर्दू|اوردو",
    "ar": r"arabic|arabi|عربی|عربي|عربية|العربية|عربى|arabisch|अरबी",
    "fa": r"persian|farsi|parsi|فارسی|فارسي|الفارسية|پارسی|persisch",
    "de": r"german|deutsch|jarman|جرمن|جرمنی|ألمانية|الألمانية|المانية|آلمانی|المانی|deutsche|जर्मन",
}
_LANG_ANY = "|".join(f"(?P<{k}>{v})" for k, v in _ALIASES.items())

# "<verb> ... in <language>" and the common short forms. Prefix words that show the student is giving an instruction.
_VERB = (r"explain|tell|answer|reply|respond|speak|talk|teach|say|write|give|continue|switch|change|use|go|teach|lecture|read|describe|"
         r"help|discuss|repeat|summari[sz]e|"
         r"samjha\w*|bata\w*|bol\w*|baat|jawab|jawaab|"
         r"سمجھا\w*|بتا\w*|بول\w*|بات|جواب|پڑھا\w*|"
         r"erkl[aä]r\w*|sprich\w*|antworte\w*|sag\w*|"
         r"اشرح|تكلم|تحدث|أجب|اجب|"
         r"توضیح|صحبت|حرف|بگو\w*|جواب")
_PREP = r"in|into|to|using|with|auf|im|en|به\s+زبان|به|بزبان|بالـ|باللغة|بلغة|ب|میں|مین|mein|mai|me|में"

# Translation / lookup questions must NOT switch the teaching language.
_NOT_A_SWITCH = re.compile(r"(translate|translation|how (do|would) (you|i) say|how to say|what is the (\w+ )?word for|meaning of|mean in|means in|"
                           r"called in|ترجمہ|ترجمه|übersetze|übersetz|wie sagt man)", re.I)

L, R = r"(?<![A-Za-z])", r"(?![A-Za-z])"       # word edges for Latin words (\b misbehaves next to Devanagari / Arabic marks)
_VERB_RE = rf"{L}(?:{_VERB}){R}"
_PREP_RE = rf"{L}(?:{_PREP}){R}"
_ADJ = r"(?:(?:the|a|simple|easy|plain|basic|short|detailed|proper|clear|natural|spoken|اللغة|زبان)\s+)*"
_END = r"(?:\s*(?:please|plz|pls|bitte|پلیز|لطفا|لطفاً|من فضلك|kijiye|kijiyega|karo|kro))?[\s.!?؟۔,]*$"
_NOT_FOLLOWED = (r"(?!\s+(?:literature|grammar|class|classes|course|department|poetry|poem|novel|text|essay|composition|"
                 r"syllabus|subject|exam|language\s+course))")

_PATTERNS = [
    # an instruction verb, then "in / into / auf / به ... <language>"      explain this in Urdu / sprich auf Deutsch / به فارسی توضیح بده
    re.compile(rf"{_VERB_RE}[^.?!؟۔\n]{{0,60}}?{_PREP_RE}\s*{_ADJ}(?:{_LANG_ANY}){R}", re.I),
    # "به فارسی توضیح بده": the preposition and the language come first, the instruction verb after
    re.compile(rf"{_PREP_RE}\s*{_ADJ}(?:{_LANG_ANY}){R}[^.?!؟۔\n]{{0,40}}?{_VERB_RE}", re.I),
    # <language> followed by "mein / میں / please / bitte"                  urdu mein samjhao / اردو میں / English please
    re.compile(rf"(?:{_LANG_ANY}){R}\s*(?:{_PREP_RE}|{L}(?:please|plz|pls|bitte){R}|پلیز|لطفا|لطفاً|من فضلك)", re.I),
    # speak / talk / reply / answer / switch / go back (to) <language>      speak Urdu / switch to German / go back to English
    re.compile(rf"{L}(?:speak|talk|reply|answer|respond|switch|change|go|teach)(?:\s+(?:me|to me|it|this|that|back))?"
               rf"(?:\s+(?:in|to|into))?\s+{_ADJ}(?:{_LANG_ANY}){R}", re.I),
    # now / again / from now on / back to <language>
    re.compile(rf"{L}(?:now|again|back to|from now on|jetzt|ab jetzt|wieder|اب|ابھی|الآن|حالا){R}[^.?!؟۔\n]{{0,25}}?(?:{_LANG_ANY}){R}", re.I),
    # the message ends with "in <language>"                                 what is recursion in Urdu
    re.compile(rf"{_PREP_RE}\s*{_ADJ}(?:{_LANG_ANY}){R}{_NOT_FOLLOWED}{_END}", re.I),
    # nothing but the language name                                         "Urdu."
    re.compile(rf"^\s*(?:{_LANG_ANY}){R}\s*[.!؟?۔]?\s*$", re.I),
]


def explicit_request(text: str) -> str | None:
    """The language the student explicitly asked for in this message, or None. The LAST one mentioned wins ("not Urdu, English please")."""
    if not text or len(text) > 600:
        return None                     # long pastes are questions/code, not instructions
    if _NOT_A_SWITCH.search(text):
        return None
    best = None                          # (end position, language)
    for pat in _PATTERNS:
        for m in pat.finditer(text):
            lang = next((k for k in _ALIASES if m.groupdict().get(k)), None)
            if lang and (best is None or m.end() >= best[0]):
                best = (m.end(), lang)
    return best[1] if best else None


# ------------------------------------------------------------------------------------------------ script / language checks
_CODE_FENCE = re.compile(r"```.*?```", re.S)
_INLINE_CODE = re.compile(r"`[^`\n]*`")
_MATH = re.compile(r"\$\$.*?\$\$|\$[^$\n]{1,200}\$", re.S)
_URL = re.compile(r"https?://\S+")
_DEVANAGARI = re.compile(r"[ऀ-ॿ]")
_ARABIC_SCRIPT = re.compile(r"[؀-ۿݐ-ݿ]")
_LATIN = re.compile(r"[A-Za-zÀ-ɏ]")
_URDU_ONLY = re.compile(r"[ٹڈڑںےہھ]")                   # ٹ ڈ ڑ ں ے ہ ھ
_PERSIAN_ONLY = re.compile(r"[ژیک]")                                          # ژ ی ک (also Urdu: used with the words below)
_URDU_WORDS = frozenset("ہے ہیں کا کی کے میں اور یہ کو سے ہو تو پر نہیں ہوتا ہوتی ہوتے کیا آپ ایک جب".split())
_PERSIAN_WORDS = frozenset("است است. می‌شود می را برای این که یک های شود نیست چون".split())
_ARABIC_WORDS = frozenset("هذا هذه في من على إلى أن ما التي الذي هو هي كان لا مع عن".split())

_GERMAN_WORDS = frozenset("der die das den dem des und ist nicht ich du wir sie er es ein eine einen einem einer zu mit auf für von im am an wie was warum "
                          "wann wo wer welche welcher kann können bitte erkläre erklären erklär mir mich uns wird werden sind sein hat haben "
                          "oder aber auch noch nur dass wenn dann also kurz einfach beispiel bedeutet unterschied".split())
_ENGLISH_WORDS = frozenset("the and is are not you your we they it a an of to in on for with how what why when where who which can could please explain "
                           "me my us be been has have or but also only that if then so simply example means difference does do".split())


_DE_ONLY = _GERMAN_WORDS - _ENGLISH_WORDS - frozenset("was also am an so war die per sie mit in im den dem des".split())
_EN_ONLY = _ENGLISH_WORDS - _GERMAN_WORDS - frozenset("a an in on is do so".split())


def strip_non_prose(text: str) -> str:
    """Remove code, math, URLs and LaTeX commands: they do not count towards the language of the answer."""
    for pat in (_CODE_FENCE, _INLINE_CODE, _MATH, _URL):
        text = pat.sub(" ", text)
    return re.sub(r"\\[A-Za-z]+", " ", text)


def latin_language(text: str, default: str) -> str:
    """German or English for Latin-script text (default is used when the text is too short to tell)."""
    words = re.findall(r"[a-zäöüß]+", text.lower())
    de, en = sum(w in _GERMAN_WORDS for w in words), sum(w in _ENGLISH_WORDS for w in words)
    umlaut = bool(re.search(r"[äöüß]", text.lower()))
    if umlaut and de >= en:
        return "de"
    if de >= 2 and de > en:
        return "de"
    if en >= 2 and en > de:
        return "en"
    return default


def script_profile(text: str) -> dict:
    prose = strip_non_prose(text)
    ar, lat, dev = len(_ARABIC_SCRIPT.findall(prose)), len(_LATIN.findall(prose)), len(_DEVANAGARI.findall(prose))
    total = max(1, ar + lat + dev)
    return {"arabic": ar / total, "latin": lat / total, "devanagari": dev / total, "letters": ar + lat + dev}


def check_language(text: str, target: str, min_letters: int = 40) -> tuple[bool, str]:
    """(ok, reason). Deterministic check that `text` is written in `target`. Short text passes (nothing to judge yet).
    Technical terms, code, formulas and a few English words inside Urdu/Arabic/Persian are fine."""
    if target not in NAMES:
        return True, ""
    prof = script_profile(text)
    if prof["letters"] < min_letters:
        return True, ""
    ar, dev = prof["arabic"], prof["devanagari"]
    if dev > 0.05:
        return False, "contains Hindi/Devanagari script; the answer must be in " + NAMES[target] + (" script (Urdu, never Hindi)" if target == "ur" else "")
    prose = strip_non_prose(text)
    if target in ("ur", "ar", "fa"):
        if ar < 0.45:
            return False, ("the answer is mostly Latin letters (English or Roman transliteration) but must be written in "
                           f"{NAMES[target]} script" + (" (not Roman Urdu)" if target == "ur" else ""))
        words = set(re.findall(r"[؀-ۿ‌]+", prose))
        urdu_letters = len(_URDU_ONLY.findall(prose))
        if target == "ar" and (urdu_letters >= 4 and len(words & _URDU_WORDS) >= 2):
            return False, "this is Urdu, not Arabic"
        if target == "fa" and urdu_letters >= 4 and len(words & _URDU_WORDS) >= 2 and len(words & _PERSIAN_WORDS) == 0:
            return False, "this is Urdu, not Persian"
        if target == "ur" and len(words & _PERSIAN_WORDS) >= 3 and len(words & _URDU_WORDS) == 0:
            return False, "this is Persian, not Urdu"
        if target == "ur" and len(words & _ARABIC_WORDS) >= 4 and len(words & _URDU_WORDS) == 0:
            return False, "this is Arabic, not Urdu"
        return True, ""
    # en / de: Latin script expected
    if ar > 0.25:
        return False, f"the answer is mostly Arabic-script text but must be in {NAMES[target]}"
    words = re.findall(r"[a-zäöüß]+", prose.lower())
    if len(words) >= 12:
        # Only words that exist in ONE of the two languages count ("was", "also", "an", "war", "die" are both), and a lesson ABOUT Germany
        # naturally contains German names and terms: a wrong-language answer has many exclusive words of the other language and
        # (almost) none of the right one. This used to restart a correct English voice lesson on German history.
        de, en = sum(w in _DE_ONLY for w in words), sum(w in _EN_ONLY for w in words)
        if target == "en" and de >= 5 and en <= 1:
            return False, "the answer is in German but must be in English"
        if target == "de" and en >= 5 and de <= 1:
            return False, "the answer is in English but must be in German"
    return True, ""


def directive(language: str, strict: bool = False) -> str:
    """The instruction given to a text model so it writes in the right language (appended to its system prompt)."""
    name = NAMES.get(language, "English")
    s = (f"LANGUAGE (mandatory): write the entire answer in {name}, whatever language the student's question is written in. "
         "Code, formulas and standard technical terms may stay in English. ")
    if language == "ur":
        s += "Use Urdu script only (never Roman Urdu, never Hindi/Devanagari). "
    elif language == "ar":
        s += "Use Arabic script (Modern Standard Arabic). "
    elif language == "fa":
        s += "Use Persian script and Persian vocabulary (not Urdu or Arabic). "
    if strict:
        s += f"A previous attempt was in the wrong language: your first sentence must already be in {name}. "
    return s


