"""
Clue definitions and deterministic answer validation.

Game truth lives here — the LLM never decides correctness.
Add new clues to the CLUES dict; the backend will automatically
expose them as voice sessions.
"""

CLUES = {
    "note": {
        "name": "Strange Note",
        "question": (
            'The note reads: "The numbers are alive. '
            'Which department knows the truth?"'
        ),
        "accepted_answers": [
            "macrodata refinement",
            "macrodata",
            "mdr",
        ],
        "truth_fragment": (
            "The numbers whisper in the Macrodata Refinement department. "
            "They are not what they seem."
        ),
    },
    "painting": {
        "name": "Kier's Portrait",
        "question": (
            "The founder watches over all. What is the name of "
            "the procedure that splits the mind in two?"
        ),
        "accepted_answers": [
            "severance procedure",
            "the severance procedure",
            "severance",
        ],
        "truth_fragment": (
            "Kier dreamed of a world without sorrow — a clean cut "
            "between who you are and who they need you to be."
        ),
    },
    "book": {
        "name": "Ricken's Book",
        "question": (
            "This forbidden book changed everything. "
            'Who is the author of "The You You Are"?'
        ),
        "accepted_answers": [
            "ricken hale",
            "ricken",
            "hale",
        ],
        "truth_fragment": (
            '"Every person is a door, and every door leads somewhere." '
            "The smuggled book that opened innies' eyes to the outside world."
        ),
    },
}


MAX_ANSWER_LENGTH = 1000


def validate_answer(clue_id: str, answer: str) -> tuple[bool, str | None]:
    """
    Validate a player's answer against accepted answers.
    Returns (correct, truth_fragment_or_none).
    """
    clue = CLUES.get(clue_id)
    if not clue or not answer:
        return False, None

    if len(answer) > MAX_ANSWER_LENGTH:
        return False, None

    normalized = answer.strip().lower()
    for accepted in clue["accepted_answers"]:
        if accepted in normalized:
            return True, clue["truth_fragment"]

    return False, None
