"""Input and output safety checks.

Cheap, deterministic, run on every message before and after the LLM:
  - prompt injection and system-prompt extraction attempts
  - betting / match-fixing requests (betting is illegal in most Indian states; we never give tips)
  - abuse and hate (rivalry matches attract it)
  - PII is redacted before text reaches the model or the logs
"""

import re
import unicodedata
from dataclasses import dataclass

from ..observability.logging import redact

INJECTION = re.compile(
    r"(ignore|disregard|forget|override)\s+(all\s+|any\s+|the\s+|your\s+)?(previous|prior|above|earlier|system)\s+"
    r"(instructions?|prompts?|rules?|messages?)"
    r"|(reveal|show|print|repeat|leak|tell me)\s+(me\s+)?(your|the)\s+(system\s+)?(prompt|instructions|rules|context)"
    r"|\byou are now\b|\bact as (?!a (cricket|commentator))|\bdeveloper mode\b|\bjailbreak\b|\bDAN\b"
    r"|<\|?(im_start|im_end|system|endoftext)\|?>|\[/?INST\]|^\s*system\s*:",
    re.IGNORECASE | re.MULTILINE,
)
# Indirect extraction ("text above", "what you were told") and Hindi/Hinglish phrasing. Fans never need any of this.
INJECTION_2 = re.compile(
    r"\b(system prompt|hidden (rules|instructions|prompt)|your (rules|instructions|prompt|guidelines|configuration)|"
    r"(instructions|rules) (you were|you've been|you have been) given|(were|was) (you )?told before|text above|"
    r"(everything|anything|what) (you were|you've been) told|initial prompt|developer message|pretend (you|that you) have no|"
    r"(you have|you've got|with) no (rules|restrictions|filters)|unrestricted|without (any )?(rules|restrictions|filters)|i am (the |your )?(admin|developer|owner)|"
    r"nirdesh|nirdeshon|niyam batao|prompt batao|bhool jao|bhul jao|instructions batao|rules batao)\b", re.I)
# Look-alike letters from other scripts ("іgnore аll" written with Cyrillic і and а) map to Latin before checking.
CONFUSABLES = str.maketrans({"а": "a", "е": "e", "і": "i", "о": "o", "р": "p", "с": "c", "у": "y", "х": "x", "ѕ": "s", "ј": "j",
                             "ԁ": "d", "ɡ": "g", "һ": "h", "ӏ": "l", "ո": "n", "ս": "u", "ν": "v", "ο": "o", "ρ": "p", "α": "a",
                             "Α": "A", "Β": "B", "Ε": "E", "Ι": "I", "Κ": "K", "Μ": "M", "Ν": "N", "Ο": "O", "Ρ": "P", "Τ": "T",
                             "Х": "X", "А": "A", "В": "B", "Е": "E", "К": "K", "М": "M", "Н": "H", "О": "O", "Р": "P", "С": "C",
                             "Т": "T", "І": "I"})
LEET = str.maketrans({"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "@": "a", "$": "s", "!": "i", "|": "l"})

BETTING = re.compile(r"\b(bet|bets|betting|wager|odds|bookie|bookies|satta|sattebaaz|dream ?11|fantasy (team|xi|pick)|"
                     r"sure[- ]?shot|match[- ]?fix(ing)?|spot[- ]?fix(ing)?|toss prediction)\b", re.IGNORECASE)
ABUSE = re.compile(r"\b(f+u+c+k+\w*|motherf\w*|bitch\w*|bastard\w*|asshole\w*|c+u+n+t+|chutiy\w*|madarchod\w*|"
                   r"bhenchod\w*|behenchod\w*|randi|harami\w*|kutt[aei]\w*|gaand\w*)\b", re.IGNORECASE)
HATE = re.compile(r"\b(kill|hang|nuke|wipe out|exterminate)\s+(all\s+)?(the\s+)?"
                  r"(pakistanis?|indians?|muslims?|hindus?|sikhs?|christians?|bangladeshis?|afghans?)\b"
                  r"|\b(pakistanis?|indians?|muslims?|hindus?)\s+(are|r)\s+(dogs|pigs|terrorists|animals|vermin)\b",
                  re.IGNORECASE)
LEAK_MARKERS = re.compile(r"(TOOLS YOU CAN CALL|RULES YOU MUST FOLLOW|MATCH SNAPSHOT \(|REFERENCE NOTES \(|TOOL RESULTS \(authoritative)", re.IGNORECASE)


@dataclass
class Verdict:
    allowed: bool
    reason: str = ""
    reply: str = ""
    text: str = ""       # normalised and PII-redacted: what the model and logs see
    original: str = ""   # normalised, not redacted: for local checks only (never logged or sent to the model)


REPLIES = {
    "empty": "Ask me anything about the match: the score, a player, the chase, or a rule.",
    "too_long": "That's a long one. Please keep questions under {n} characters.",
    "injection": "I can only help with cricket questions about this match and IPL history.",
    "betting": "I don't give betting, fantasy or match-fixing advice. I can tell you the score, form and stats instead.",
    "abuse": "Let's keep it about the cricket. Ask me about the match and I'll help.",
    "hate": "I won't engage with that. Rivalries are about cricket, not people.",
}


def normalise(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = "".join(ch for ch in text if ch in "\n\t" or unicodedata.category(ch)[0] != "C")  # strip control chars
    return re.sub(r"[ \t]+", " ", text).strip()


def _variants(text: str) -> tuple[str, ...]:
    """The text as typed, plus de-obfuscated copies: look-alike letters, leetspeak, and s p a c e d letters joined."""
    plain = text.translate(CONFUSABLES)
    deleet = plain.translate(LEET)
    joined = re.sub(r"\b(?:\w ){3,}\w\b", lambda m: m.group(0).replace(" ", ""), deleet)
    return (text, plain, deleet, joined)


def check_input(raw: str, max_chars: int) -> Verdict:
    text = normalise(raw)
    if not text:
        return Verdict(False, "empty", REPLIES["empty"])
    if len(text) > max_chars:
        return Verdict(False, "too_long", REPLIES["too_long"].format(n=max_chars))
    # Check both the text and a de-leeted copy, so "1gn0re prev1ous instruct1ons" is caught too.
    variants = _variants(text)
    for reason, patterns in (("hate", (HATE,)), ("injection", (INJECTION, INJECTION_2)), ("betting", (BETTING,)), ("abuse", (ABUSE,))):
        if any(p.search(v) for p in patterns for v in variants):
            return Verdict(False, reason, REPLIES[reason])
    return Verdict(True, text=redact(text), original=text)


INTERNAL_NAMES = re.compile(r"\b(MATCH SNAPSHOT|REFERENCE NOTES|TOOL RESULTS|fan_question|system prompt|my (rules|instructions))\b", re.I)


def _shingles(text: str, n: int = 6) -> set[str]:
    words = re.findall(r"[a-z0-9]+", text.lower())
    return {" ".join(words[i:i + n]) for i in range(len(words) - n + 1)}


_protected: set[str] = set()


def protect(secret_text: str) -> None:
    """Registers text (the system prompt) that must never appear in an answer."""
    _protected.update(_shingles(secret_text))


def check_output(text: str) -> Verdict:
    # Any 6-word run copied from the system prompt is a leak, however the model was talked into it.
    if _protected and _shingles(text) & _protected:
        return Verdict(False, "leak", "I can only help with cricket questions about this match and IPL history.")
    if INTERNAL_NAMES.search(text):
        return Verdict(False, "leak", "I can only help with cricket questions about this match and IPL history.")
    if LEAK_MARKERS.search(text):
        return Verdict(False, "leak", "Sorry, I couldn't put that answer together. Please ask again.")
    if HATE.search(text) or ABUSE.search(text):
        return Verdict(False, "unsafe_output", "Sorry, I couldn't put that answer together. Please ask again.")
    return Verdict(True, text=text)
