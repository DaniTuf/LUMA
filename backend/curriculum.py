"""What LUMA is allowed to teach.

Reads `curriculum.json` from the project root if it exists:

    {
      "university": "TUF",
      "programs": [
        {"name": "BS Computer Science", "courses": ["Programming Fundamentals", "Data Structures"]},
        {"name": "BBA", "courses": ["Principles of Marketing", "Financial Accounting"]}
      ],
      "extra_subjects": ["Islamic Studies", "English Composition"]
    }

Without the file, LUMA covers university-level academic subjects in general.
"""
import json
import os

_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "curriculum.json")
_MAX_PROMPT_CHARS = 6000

DEFAULT_SUBJECTS = [
    "Mathematics", "Physics", "Chemistry", "Biology", "Computer Science", "Engineering", "Medicine",
    "Business", "Economics", "Law", "Psychology", "History", "Literature", "Islamic Studies", "Education",
]


def _load() -> dict:
    try:
        with open(_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        return {}


_data = _load()
PROGRAMS = [p for p in _data.get("programs", []) if isinstance(p, dict) and p.get("name")]
EXTRA_SUBJECTS = [s for s in _data.get("extra_subjects", []) if isinstance(s, str) and s.strip()]
UNIVERSITY = str(_data.get("university") or "").strip()
HAS_CURRICULUM = bool(PROGRAMS or EXTRA_SUBJECTS)


def subject_suggestions() -> list[str]:
    """Names for the Subject / Course box: every course, then program names, then extras."""
    if not HAS_CURRICULUM:
        return DEFAULT_SUBJECTS
    seen, out = set(), []
    for p in PROGRAMS:
        for c in p.get("courses", []) or []:
            if isinstance(c, str) and c.strip() and c.strip().lower() not in seen:
                seen.add(c.strip().lower())
                out.append(c.strip())
    for name in [p["name"] for p in PROGRAMS] + EXTRA_SUBJECTS:
        if name.lower() not in seen:
            seen.add(name.lower())
            out.append(name)
    return out


def scope_text() -> str:
    """The 'what you may teach' sentence for the system prompt."""
    if not HAS_CURRICULUM:
        return (
            "You teach university-level academic subjects: sciences, mathematics, engineering, computer science, "
            "medicine and health, business and economics, law, social sciences, humanities, languages, "
            "arts and education. "
        )
    lines = []
    for p in PROGRAMS:
        courses = [c.strip() for c in (p.get("courses") or []) if isinstance(c, str) and c.strip()]
        lines.append(f"- {p['name']}" + (f": {', '.join(courses)}" if courses else ""))
    if EXTRA_SUBJECTS:
        lines.append("- Other subjects: " + ", ".join(EXTRA_SUBJECTS))
    listing = "\n".join(lines)[:_MAX_PROMPT_CHARS]
    who = f"{UNIVERSITY} " if UNIVERSITY else "the "
    return (
        f"You teach only the degree programs and courses of {who}university, listed here:\n{listing}\n"
        "You may also teach the foundations these courses depend on (for example school-level mathematics, "
        "science and English). A topic that belongs to none of these programs or their foundations is out of scope. "
    )
