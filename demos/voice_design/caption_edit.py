"""Deterministic fallback edits for voice captions.

PhoneLLM sometimes returns the current caption unchanged for a simple request
such as "higher pitch", especially when the caption has no pitch word to edit.
This module recognises the common one-trait requests in the five demo languages
and applies the corresponding ladder edit to the caption directly, following the
caption format of the voice generator (see README).
"""

from __future__ import annotations

import re

PITCH_LADDER = ["very low", "low", "medium", "high", "very high"]
PACE_LADDER = ["slow", "deliberate", "brisk", "fast"]
QUIET_LADDER = ["soft-spoken", "hushed", "whispered"]
LOUD_LADDER = ["raised", "shouted"]
AGE_LADDER = ["Child", "Teenage", "Young adult", "Middle-aged", "Older adult", "Elderly"]

PITCH_RE = re.compile(r"\b(very low|very high|low|medium|high)(-pitched)?\b", re.IGNORECASE)
PACE_RE = re.compile(r"\b(slow|deliberate|brisk|fast)\b", re.IGNORECASE)
EFFORT_RE = re.compile(r"\b(whispered|hushed|soft-spoken|raised|shouted)\b", re.IGNORECASE)
AGE_RE = re.compile(
    r"\b(Child|Teenage|Young adult|Middle-aged|Older adult|Elderly)\b", re.IGNORECASE
)

INTENTS: list[tuple[str, re.Pattern[str]]] = [
    (
        "pitch_up",
        re.compile(
            r"\b(higher|high[- ]?pitch\w*|pitch (it )?up|raise the pitch|more high|"
            r"plus (haut|aigu\w*)|plus de hauteur|m[aá]s agud\w*|h[öo]her|mais agud\w*)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "pitch_down",
        re.compile(
            r"\b(deeper|lower|low[- ]?pitch\w*|pitch (it )?down|more (deep|bass)|"
            r"plus (grave|bas|profond\w*)|m[aá]s (grave|profund\w*)|tiefer|mais grave)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "faster",
        re.compile(
            r"\b(faster|quicker|speed (it )?up|more (quickly|energy)|plus (vite|rapide)|"
            r"m[aá]s r[aá]pid\w*|schneller|mais r[aá]pid\w*)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "slower",
        re.compile(
            r"\b(slower|slow (it )?down|more slowly|plus lent\w*|m[aá]s lent\w*|"
            r"langsamer|mais lent\w*|mais devagar)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "whisper",
        re.compile(
            r"\b(whisper\w*|chuchot\w*|murmur\w*|susurr\w*|fl[üu]ster\w*|sussurr\w*)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "quieter",
        re.compile(
            r"\b(softer|quieter|gentler|more (soft|quiet|gentle)|plus (doux|douce|"
            r"calme)|moins fort\w*|m[aá]s (suave|baj\w*)|leiser|sanfter|mais (suave|"
            r"baix\w*))\b",
            re.IGNORECASE,
        ),
    ),
    (
        "louder",
        re.compile(
            r"\b(louder|shout\w*|more (loud|forceful)|plus fort\w*|crie\w*|m[aá]s "
            r"fuerte|grit\w*|lauter|schrei\w*|mais alto|mais forte)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "older",
        re.compile(
            r"\b(older|more mature|plus (vieux|vieille|[aâ]g\w+)|m[aá]s (viej\w*|mayor)|"
            r"[äa]lter|mais velh\w*)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "younger",
        re.compile(
            r"\b(younger|more youthful|plus jeune|m[aá]s joven|j[üu]nger|mais jovem|"
            r"mais nov\w*)\b",
            re.IGNORECASE,
        ),
    ),
]


# Mood requests map to a pair of emotion adjectives for the emotion slot.
EMOTION_INTENTS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b(happ\w*|cheer\w*|joy\w*|joyeu\w*|content\w*|feliz|alegr\w*|fr[öo]hlich\w*|gl[üu]cklich\w*)\b", re.I), "Cheerful, playful"),
    (re.compile(r"\b(sad\w*|melanchol\w*|gloom\w*|triste\w*|traurig\w*)\b", re.I), "Sad, subdued"),
    (re.compile(r"\b(angr\w*|furious|mad|f[âa]ch\w*|col[èe]re|enojad\w*|enfadad\w*|w[üu]tend\w*|zangad\w*|raiv\w*)\b", re.I), "Angry, sharp"),
    (re.compile(r"\b(calm\w*|relax\w*|serene\w*|soothing|zen|tranquil\w*|apais\w*|ruhig\w*|gelassen)\b", re.I), "Calm, gentle"),
    (re.compile(r"\b([ée]nerg\w*|excit\w*|lively|dynamic|enthusias\w*|peppy|dynamique|entusias\w*|lebhaft|animad\w*)\b", re.I), "Energetic, enthusiastic"),
    (re.compile(r"\b(scar\w*|creep\w*|sinister|menac\w*|dark\w*|effrayant\w*|inqui[ée]tant\w*|sombre|siniestr\w*|d[üu]ster\w*|gruselig\w*|assustador\w*)\b", re.I), "Menacing, dark"),
    (re.compile(r"\b(friendl\w*|warm\w*|kind\w*|sympa\w*|chaleureu\w*|amable|c[áa]lid\w*|freundlich\w*|simp[áa]tic\w*|amig[áa]vel)\b", re.I), "Friendly, affectionate"),
    (re.compile(r"\b(serious\w*|stern|formal|grave|s[ée]rieu\w*|seri\w*|ernst\w*|s[ée]ri\w*)\b", re.I), "Serious, composed"),
    (re.compile(r"\b(playful|mischiev\w*|cheeky|taquin\w*|espi[èe]gle|travies\w*|verspielt|brincalh\w*)\b", re.I), "Playful, mischievous"),
    (re.compile(r"\b(tired|weary|sleepy|fatigu\w*|las\w*|cansad\w*|m[üu]de)\b", re.I), "Weary, subdued"),
]
EMOTION_REQUEST_RE = re.compile(
    r"\b(more|less|make (it|him|her|them)|sound\w*|plus|moins|rends?|m[áa]s|menos|hazl[oa]|"
    r"mehr|weniger|mach|mais|menos|deixa)\b",
    re.I,
)


def detect_emotion(utterance: str) -> str | None:
    if not utterance or not EMOTION_REQUEST_RE.search(utterance):
        return None
    for pattern, adjectives in EMOTION_INTENTS:
        if pattern.search(utterance):
            return adjectives
    return None


def _replace_emotion_slot(caption: str, adjectives: str) -> str:
    """Swap the emotion slot (the short comma-separated segment before the sound
    sentence) or insert one just before the sound sentence."""
    start, _ = _sound_sentence_span(caption)
    head = caption[:start].rstrip()
    segments = [s for s in re.split(r"(?<=\.)\s+", head) if s]
    for index in range(len(segments) - 1, -1, -1):
        segment = segments[index].rstrip(".")
        words = segment.split()
        looks_like_emotion = (
            "," in segment
            and 1 < len(words) <= 4
            and not re.search(r"\b(accent|accented|Masculine|Feminine|Neutral)\b", segment)
            and not AGE_RE.search(segment)
        )
        if looks_like_emotion:
            segments[index] = adjectives + "."
            break
    else:
        segments.append(adjectives + ".")
    rebuilt = " ".join(segments)
    tail = caption[start:]
    return (rebuilt + " " + tail).strip() if tail else rebuilt


# Accent requests. Native labels are per conversation language; a foreign accent
# becomes "<Language>-accented <ConversationLanguage>".
LANGUAGE_NAMES = {"en": "English", "fr": "French", "es": "Spanish", "de": "German", "pt": "Portuguese"}
NATIVE_ACCENTS: dict[str, list[tuple[re.Pattern[str], str]]] = {
    "en": [
        (re.compile(r"\b(texan|southern|deep south|louisiana|alabama|georgia)\b", re.I), "Southern American"),
        (re.compile(r"\b(new york|brooklyn|bronx)\b", re.I), "New York"),
        (re.compile(r"\b(californian?|valley girl|la\b|los angeles)\b", re.I), "Californian"),
        (re.compile(r"\b(canadian?|canada)\b", re.I), "Canadian"),
        (re.compile(r"\b(american|us|u\.s\.|usa|états[- ]unis|americain\w*|américain\w*|estadounidense|amerikanisch\w*|americano)\b", re.I), "General American"),
        (re.compile(r"\b(cockney|london(er)?)\b", re.I), "London"),
        (re.compile(r"\b(northern english|manchester|yorkshire|liverpool|scouse|geordie|newcastle)\b", re.I), "Northern English"),
        (re.compile(r"\b(scottish|scots|scotland|glasgow|edinburgh|écossais\w*|ecossais\w*|escoc[eé]s\w*|schottisch\w*)\b", re.I), "Scottish"),
        (re.compile(r"\b(welsh|wales|gallois\w*|gal[eé]s|walisisch\w*)\b", re.I), "Welsh"),
        (re.compile(r"\b(irish|ireland|dublin|irlandais\w*|irland[eé]s\w*|irisch\w*)\b", re.I), "Irish"),
        (re.compile(r"\b(australian?|aussie|australien\w*|australiano|australisch\w*)\b", re.I), "Australian"),
        (re.compile(r"\b(new zealand|kiwi)\b", re.I), "New Zealand"),
        (re.compile(r"\b(indian|india|indien\w*|indio|indisch\w*)\b", re.I), "Indian English"),
        (re.compile(r"\b(south african|afrikaans|sud[- ]africain\w*|sudafricano|südafrikanisch\w*)\b", re.I), "South African"),
        (re.compile(r"\b(nigerian|ghanaian|west african)\b", re.I), "West African English"),
        (re.compile(r"\b(british|english accent|rp|posh|bbc|queen'?s english|britannique|brit[aá]nico|britisch\w*)\b", re.I), "Standard British"),
    ],
    "fr": [
        (re.compile(r"\b(parisien\w*|parisian|paris)\b", re.I), "Parisian French"),
        (re.compile(r"\b(marseill\w*|du sud|southern|midi|toulous\w*|provenç\w*)\b", re.I), "Southern French"),
        (re.compile(r"\b(belge|belgian|bruxell\w*)\b", re.I), "Belgian French"),
        (re.compile(r"\b(suisse|swiss|romand\w*)\b", re.I), "Swiss Romand"),
        (re.compile(r"\b(qu[ée]b[ée]cois\w*|quebec|canadien\w*|montr[ée]al)\b", re.I), "Quebecois French"),
        (re.compile(r"\b(marocain\w*|moroccan)\b", re.I), "Moroccan French"),
        (re.compile(r"\b(alg[ée]rien\w*|algerian)\b", re.I), "Algerian French"),
        (re.compile(r"\b(tunisien\w*|tunisian)\b", re.I), "Tunisian French"),
        (re.compile(r"\b(maghr[ée]bin\w*|maghrebi)\b", re.I), "Maghrebi French"),
        (re.compile(r"\b(africain\w*|african|s[ée]n[ée]galais\w*|ivoirien\w*)\b", re.I), "West African French"),
        (re.compile(r"\b(antillais\w*|cr[ée]ole|caribbean|martiniqu\w*|guadeloup\w*)\b", re.I), "French Caribbean"),
    ],
    "es": [
        (re.compile(r"\b(castellan\w*|castilian|madrid|madrile[ñn]\w*|espa[ñn]ol de espa[ñn]a)\b", re.I), "Castilian Spanish"),
        (re.compile(r"\b(andalu\w*)\b", re.I), "Andalusian"),
        (re.compile(r"\b(canari\w*)\b", re.I), "Canarian"),
        (re.compile(r"\b(mexican\w*|m[ée]xic\w*)\b", re.I), "Mexican Spanish"),
        (re.compile(r"\b(centroamerican\w*|guatemal\w*|salvadore[ñn]\w*|hondure[ñn]\w*|costarricen\w*)\b", re.I), "Central American Spanish"),
        (re.compile(r"\b(caribe[ñn]\w*|cuban\w*|puertorrique[ñn]\w*|dominican\w*|caribbean)\b", re.I), "Caribbean Spanish"),
        (re.compile(r"\b(colombian\w*)\b", re.I), "Colombian Spanish"),
        (re.compile(r"\b(andin\w*|peruan\w*|bolivian\w*|ecuatorian\w*)\b", re.I), "Andean Spanish"),
        (re.compile(r"\b(chilen\w*)\b", re.I), "Chilean Spanish"),
        (re.compile(r"\b(argentin\w*|rioplatense|uruguay\w*|porte[ñn]\w*)\b", re.I), "Rioplatense Spanish"),
    ],
    "de": [
        (re.compile(r"\b(hochdeutsch|standard|neutral)\b", re.I), "Standard German (Hochdeutsch)"),
        (re.compile(r"\b(berlin\w*)\b", re.I), "Berlin"),
        (re.compile(r"\b(s[äa]chsisch\w*|sachsen|saxon)\b", re.I), "Saxon"),
        (re.compile(r"\b(k[öo]lsch|k[öo]ln\w*|rheinl[äa]nd\w*|cologne)\b", re.I), "Cologne/Rhineland"),
        (re.compile(r"\b(hessisch\w*|hessen|frankfurt\w*)\b", re.I), "Hessian"),
        (re.compile(r"\b(bayerisch\w*|bairisch\w*|bavarian|m[üu]nchn\w*)\b", re.I), "Bavarian"),
        (re.compile(r"\b(schw[äa]bisch\w*|swabian|stuttgart\w*)\b", re.I), "Swabian"),
        (re.compile(r"\b(norddeutsch\w*|hamburg\w*|northern)\b", re.I), "Northern German"),
        (re.compile(r"\b([öo]sterreich\w*|austrian|wien\w*|vienn\w*)\b", re.I), "Austrian German"),
        (re.compile(r"\b(schweizer\w*|schwiizer\w*|swiss|z[üu]rich\w*)\b", re.I), "Swiss German"),
    ],
    "pt": [
        (re.compile(r"\b(lisboa|lisbon|europe\w*|portugal|portugu[êe]s de portugal)\b", re.I), "European Portuguese (Lisbon)"),
        (re.compile(r"\b(porto|nortenh\w*|northern)\b", re.I), "Northern Portugal (Porto)"),
        (re.compile(r"\b(paulist\w*|s[ãa]o paulo)\b", re.I), "Brazilian Paulistano"),
        (re.compile(r"\b(carioca|rio)\b", re.I), "Brazilian Carioca"),
        (re.compile(r"\b(nordestin\w*|nordeste|bahia\w*|pernambuc\w*)\b", re.I), "Brazilian Nordestino"),
        (re.compile(r"\b(ga[úu]ch\w*|porto alegre)\b", re.I), "Brazilian Gaucho"),
        (re.compile(r"\b(mineir\w*|minas)\b", re.I), "Brazilian Mineiro"),
        (re.compile(r"\b(brasileir\w*|brazilian|brasil)\b", re.I), "General Brazilian Portuguese"),
        (re.compile(r"\b(african\w*|angolan\w*|mo[çc]ambican\w*)\b", re.I), "African Portuguese"),
    ],
}
FOREIGN_LANGUAGES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b(french|fran[çc]ais\w*|franc[eé]s\w*|franz[öo]sisch\w*|france)\b", re.I), "French"),
    (re.compile(r"\b(spanish|espagnol\w*|espa[ñn]ol\w*|spanisch\w*|espanhol\w*|spain)\b", re.I), "Spanish"),
    (re.compile(r"\b(german|allemand\w*|alem[áa]n\w*|deutsch\w*|alem[ãa]o|germany)\b", re.I), "German"),
    (re.compile(r"\b(italian\w*|italien\w*|italiano|italienisch\w*|italy)\b", re.I), "Italian"),
    (re.compile(r"\b(portuguese|portugais\w*|portugu[êe]s\w*|portugiesisch\w*|brazilian|br[ée]silien\w*)\b", re.I), "Portuguese"),
    (re.compile(r"\b(russian|russe|ruso|russisch\w*|russo)\b", re.I), "Russian"),
    (re.compile(r"\b(japanese|japonais\w*|japon[ée]s|japanisch\w*)\b", re.I), "Japanese"),
    (re.compile(r"\b(chinese|chinois\w*|chino|chinesisch\w*|chin[êe]s)\b", re.I), "Chinese"),
    (re.compile(r"\b(arabic|arabe|[áa]rabe|arabisch\w*)\b", re.I), "Arabic"),
    (re.compile(r"\b(english|anglais\w*|ingl[ée]s|englisch\w*|british|american)\b", re.I), "English"),
    (re.compile(r"\b(dutch|n[ée]erlandais\w*|holand[ée]s|niederl[äa]ndisch\w*)\b", re.I), "Dutch"),
    (re.compile(r"\b(polish|polonais\w*|polaco|polnisch\w*)\b", re.I), "Polish"),
    (re.compile(r"\b(turkish|turc|turco|t[üu]rkisch\w*)\b", re.I), "Turkish"),
    (re.compile(r"\b(indian|hindi|indien\w*)\b", re.I), "Indian"),
    (re.compile(r"\b(mexican\w*|mexicain\w*|mexikanisch\w*)\b", re.I), "Mexican Spanish"),
]
ACCENT_REQUEST_RE = re.compile(r"\b(accent\w*|acento|akzent|sotaque|sound\w* (like|more)|from)\b", re.I)
ACCENT_SEGMENT_RE = re.compile(r"\baccent\b|-accented\b", re.I)


def detect_accent(utterance: str, language: str = "en") -> str | None:
    """Return the caption accent label the utterance asks for, or None."""
    if not utterance or not ACCENT_REQUEST_RE.search(utterance):
        return None
    for pattern, label in NATIVE_ACCENTS.get(language, NATIVE_ACCENTS["en"]):
        if pattern.search(utterance):
            return f"{label} accent"
    target = LANGUAGE_NAMES.get(language, "English")
    for pattern, name in FOREIGN_LANGUAGES:
        if pattern.search(utterance) and name != target:
            return f"{name}-accented {target}"
    return None


def _replace_accent_slot(caption: str, accent: str) -> str:
    start, _ = _sound_sentence_span(caption)
    head = caption[:start].rstrip()
    tail = caption[start:]
    segments = [s for s in re.split(r"(?<=\.)\s+", head) if s]
    for index, segment in enumerate(segments):
        if ACCENT_SEGMENT_RE.search(segment):
            segments[index] = accent + "."
            break
    else:
        # Insert before the emotion slot when there is one, else last in the head.
        insert_at = len(segments)
        if segments:
            last = segments[-1].rstrip(".")
            if "," in last and 1 < len(last.split()) <= 4 and not AGE_RE.search(last) \
                    and not re.search(r"\b(Masculine|Feminine|Neutral)\b", last):
                insert_at = len(segments) - 1
        segments.insert(insert_at, accent + ".")
    rebuilt = " ".join(segments)
    return (rebuilt + " " + tail).strip() if tail else rebuilt


GENDER_INTENTS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b(female|woman|women|girl|feminine|lady|femme|f[ée]minin\w*|fille|mujer|femenin\w*|chica|frau|weiblich\w*|mulher|menina|feminina)\b", re.I), "Feminine"),
    (re.compile(r"\b(male|man|men|guy|masculine|boy|homme|masculin\w*|gar[çc]on|hombre|masculino|chico|mann|m[äa]nnlich\w*|junge|homem|menino|rapaz)\b", re.I), "Masculine"),
    (re.compile(r"\b(gender[- ]neutral|neutral gender|non[- ]binary|androgynous|neutre|androgyne|neutro|neutral|androgyn\w*)\b", re.I), "Gender Neutral"),
]
GENDER_SLOT_RE = re.compile(r"^(Masculine|Feminine|Gender Neutral)\b")
REMOVAL_RE = re.compile(r"\b(remove|drop|without|no more|get rid|enl[èe]ve|sans|retire|quita|sin|ohne|entfern\w*|tira|sem)\b", re.I)


def detect_gender(utterance: str) -> str | None:
    for pattern, label in GENDER_INTENTS:
        if pattern.search(utterance or ""):
            return label
    return None


def set_gender(caption: str, gender: str) -> str:
    """Replace or insert the gender slot at the start of the caption."""
    if GENDER_SLOT_RE.match(caption):
        return GENDER_SLOT_RE.sub(gender, caption, count=1)
    if AGE_RE.match(caption):
        return f"{gender}, {caption}"
    return f"{gender}. {caption}"


def detect_intent(utterance: str) -> str | None:
    """Return the first matching one-trait intent, or None."""
    for name, pattern in INTENTS:
        if pattern.search(utterance or ""):
            return name
    return None


def _step(ladder: list[str], current: str, delta: int) -> str:
    index = [w.lower() for w in ladder].index(current.lower())
    return ladder[max(0, min(len(ladder) - 1, index + delta))]


def _sound_sentence_span(caption: str) -> tuple[int, int]:
    """Locate the free "how it sounds" part: the first long segment or one with 'voice'."""
    position = 0
    for segment in re.split(r"(?<=\.)\s+", caption):
        if "voice" in segment.lower() or len(segment.split()) > 6:
            return position, position + len(segment)
        position += len(segment) + 1
    return len(caption), len(caption)


def _insert_in_sound_sentence(caption: str, prefix_word: str | None, suffix_word: str | None) -> str:
    start, end = _sound_sentence_span(caption)
    sentence = caption[start:end]
    if not sentence:
        sentence = f"A {prefix_word or 'medium'}-pitched voice."
        if suffix_word:
            sentence = sentence[:-1] + f", {suffix_word}."
        return (caption.rstrip() + " " + sentence).strip()
    if prefix_word:
        match = re.match(r"^(A|An|The)\s+", sentence)
        if match:
            sentence = f"A {prefix_word}, " + sentence[match.end():]
        else:
            sentence = f"A {prefix_word}-pitched voice. " + sentence
    if suffix_word:
        sentence = sentence.rstrip(". ") + f", {suffix_word}."
    return caption[:start] + sentence + caption[end:]


def already_satisfied(caption: str, utterance: str, language: str = "en") -> bool:
    """True when the utterance asks for a trait the caption already has."""
    if not caption or not utterance:
        return False
    accent = detect_accent(utterance, language)
    if accent and not REMOVAL_RE.search(utterance):
        return accent.lower() in caption.lower()
    gender = detect_gender(utterance)
    if gender:
        current = GENDER_SLOT_RE.match(caption)
        return bool(current and current.group(1) == gender)
    intent = detect_intent(utterance)
    if intent:
        return rewrite_caption(caption, utterance, language) is None
    adjectives = detect_emotion(utterance)
    if adjectives:
        return adjectives.lower() in caption.lower()
    return False


def rewrite_caption(caption: str, utterance: str, language: str = "en") -> str | None:
    """Apply the one-trait edit the utterance asks for; None when not applicable."""
    if not caption:
        return None
    accent = detect_accent(utterance, language)
    if accent and not REMOVAL_RE.search(utterance):
        return None if accent.lower() in caption.lower() else _replace_accent_slot(caption, accent)
    gender = detect_gender(utterance)
    if gender:
        current = GENDER_SLOT_RE.match(caption)
        if current and current.group(1) == gender:
            return None
        return set_gender(caption, gender)
    intent = detect_intent(utterance)
    if not intent:
        adjectives = detect_emotion(utterance)
        if adjectives and adjectives.lower() not in caption.lower():
            return _replace_emotion_slot(caption, adjectives)
        return None
    steps = 2 if re.search(r"\b(much|way|lot|beaucoup|mucho|viel|muito)\b", utterance, re.I) else 1

    if intent in ("pitch_up", "pitch_down"):
        delta = steps if intent == "pitch_up" else -steps
        match = PITCH_RE.search(caption)
        if match:
            new = _step(PITCH_LADDER, match.group(1), delta)
            if new.lower() == match.group(1).lower():
                # Already at the end of the ladder: add an unmistakable sound word.
                extra = "piercing" if delta > 0 else "booming"
                if extra in caption.lower():
                    return None
                return caption[: match.end()] + f", {extra}" + caption[match.end() :]
            return caption[: match.start(1)] + new + caption[match.end(1) :]
        word = "high" if delta > 0 else "low"
        if steps > 1:
            word = "very " + word
        return _insert_in_sound_sentence(caption, word, None)

    if intent in ("faster", "slower"):
        # "deliberate" is still slow-ish, so a "faster" request jumps to "brisk".
        faster = {"slow": "brisk", "deliberate": "brisk", "brisk": "fast", "fast": None}
        slower = {"fast": "deliberate", "brisk": "slow", "deliberate": "slow", "slow": None}
        table = faster if intent == "faster" else slower
        match = PACE_RE.search(caption)
        if match:
            new = table[match.group(1).lower()]
            if new is None:
                return None
            return caption[: match.start()] + new + caption[match.end() :]
        return _insert_in_sound_sentence(caption, None, "brisk" if intent == "faster" else "slow")

    if intent in ("whisper", "quieter", "louder"):
        match = EFFORT_RE.search(caption)
        current = match.group(1).lower() if match else None
        if intent == "whisper":
            new = "whispered"
        elif intent == "quieter":
            new = _step(QUIET_LADDER, current, 1) if current in QUIET_LADDER else "soft-spoken"
        else:
            new = _step(LOUD_LADDER, current, 1) if current in LOUD_LADDER else "raised"
        if current == new:
            return None
        if match:
            return caption[: match.start()] + new + caption[match.end() :]
        return _insert_in_sound_sentence(caption, None, new)

    if intent in ("older", "younger"):
        delta = steps if intent == "older" else -steps
        match = AGE_RE.search(caption)
        if match:
            new = _step(AGE_LADDER, match.group(1), delta)
            return None if new.lower() == match.group(1).lower() else (
                caption[: match.start()] + new + caption[match.end() :]
            )
        word = "Older adult" if delta > 0 else "Teenage"
        gender = re.match(r"^(Masculine|Feminine|Gender Neutral)\b", caption)
        if gender:
            return caption[: gender.end()] + f", {word}" + caption[gender.end() :]
        return f"{word}. " + caption

    return None


_EXPLICIT_EDIT_RE = re.compile(
    r"\b(?:"
    # English
    r"(?:reduce|lower|increase|raise|change|adjust)\s+(?:the\s+)?(?:age|pitch|pace|energy|speed|warmth|tone)\s+of\s+(?:the|this|that)\s+voice|"
    r"I\s+(?:still\s+)?(?:want|need|would\s+like)\s+(?:a\s+)?(?:younger|older|warmer|calmer|softer|deeper|lighter|brighter)\s+voice|"
    r"I\s+(?:want|need|would\s+like|['’]d\s+like)\s+(?:(?:the|this|that|your|same)\s+voice|it|her|him)\s+to\s+(?:be|sound|feel|become)\b|"
    r"(?:can|could|would)\s+you\s+(?:please\s+)?(?:make|change|adjust|modify|give|add|remove)|"
    r"(?:make|change|adjust|modify|turn|give|add|remove)\s+(?:it|this|that|the\s+voice|him|her|them)|"
    r"(?:same|this|that)\s+voice\b.{0,24}\b(?:but|with)|"
    # French, Spanish, German, Portuguese
    r"(?:rends?|rendre|change|modifie|ajuste)\s+(?:la|le|cette|ce|ça|plus|moins)|"
    r"(?:haz|hace|cambia|ajusta|modifica)\s*(?:la|lo|el|ella|voz|más|menos)?|"
    r"(?:mach|mache|ändert?|ändere|pass\s+an)\s+(?:sie|ihn|es|die\s+stimme)|"
    r"(?:faça|faz|deixa|mude|muda|ajuste)\s+(?:ela|ele|isso|a\s+voz|mais|menos)"
    r")\b",
    re.IGNORECASE,
)
_CONVERSATION_RE = re.compile(
    r"\b(?:tell\s+me|story|joke|explain|describe|read|translate|what|why|who|where|"
    r"when|how\s+many|do\s+people|say\s+that|parle-moi|histoire|raconte|explique|"
    r"cuéntame|historia|explica|erzähl|geschichte|erkläre|conte-me|história|explique)\b",
    re.IGNORECASE,
)
_VOICE_DESIGN_REQUEST_RE = re.compile(
    r"\b(?:want|need|create|design|generate|build|looking\s+for|would\s+like)\b"
    r".{0,160}\bvoice\b|"
    r"\bvoice\b.{0,160}\b(?:create|design|generate|build|accent|speaker)\b",
    re.IGNORECASE,
)
_SAME_VOICE_EDIT_RE = re.compile(
    r"\b(?:same\s+voice|m[êe]me\s+voi[xe]|misma\s+voz|gleiche\s+stimme|"
    r"mesma\s+voz)\b",
    re.IGNORECASE,
)


def is_conversation_request(utterance: str) -> bool:
    """Return whether speech is ordinary conversation, not a voice revision."""

    if not utterance or _VOICE_DESIGN_REQUEST_RE.search(utterance) or is_voice_edit_intent(utterance):
        return False
    return bool(_CONVERSATION_RE.search(utterance))


def is_voice_edit_intent(utterance: str) -> bool:
    """Recognize a requested edit independently of our finite caption vocabulary.

    Relative clauses ("someone who's lived a full life") are not questions.
    An informational lead-in before the command remains ordinary conversation.
    This routes to the model's tool, not speculative generation.
    """
    command = _EXPLICIT_EDIT_RE.search(utterance) or _SAME_VOICE_EDIT_RE.search(utterance)
    if command is None:
        return False
    prefix = re.split(r"[.!?]", utterance[:command.start()])[-1]
    if _CONVERSATION_RE.search(prefix):
        return False
    if re.search(r"\b(?:don't|do not|never|not to)\s*$", prefix, re.I):
        return False
    return True


def is_explicit_edit_request(
    caption: str,
    utterance: str,
    language: str = "en",
) -> bool:
    """Return whether a revision is safe to start before the model calls a tool.

    Prefetching has API side effects, so this intentionally accepts fewer phrases
    than :func:`rewrite_caption`. Long or conversational utterances wait for the
    model's explicit tool call instead of guessing from an isolated word such as
    "older", "woman", or "France".
    """

    if not caption or not utterance:
        return False
    rewritten = rewrite_caption(caption, utterance, language)
    if not rewritten or rewritten == caption:
        return False
    if _EXPLICIT_EDIT_RE.search(utterance):
        return True
    if _SAME_VOICE_EDIT_RE.search(utterance):
        return True
    if _CONVERSATION_RE.search(utterance):
        return False

    words = re.findall(r"\w+", utterance, re.UNICODE)
    return len(words) <= 7
