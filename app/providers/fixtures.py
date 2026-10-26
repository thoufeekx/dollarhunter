import re
from decimal import Decimal

from app.catalog import AMBIGUOUS, match_staple
from app.providers.base import Instruction, ParsedItem, ParsedList

NUMS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
        "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "dozen": 12}
GREETING = re.compile(r"^\s*(hi|hello|hey|start|yo)[\s!.]*$", re.I)
EXCLUDE = re.compile(r"\b(?:no|without)\s+([a-z ]+?)\s+this (?:week|time|order)\b|\bskip\s+([a-z ]+)", re.I)
REMOVE = re.compile(r"\b(?:remove|delete|drop|take out)\s+(?:the\s+)?([a-z ]+)", re.I)
NOTE = re.compile(r"\b(guests?\b[^.;,]*|having people[^.;,]*)", re.I)


class RuleListParser:
    def parse(self, text: str) -> ParsedList:
        out = ParsedList()
        t = text.replace("\n", ", ")
        for m in EXCLUDE.finditer(t):
            what = (m.group(1) or m.group(2)).strip().lower()
            out.instructions.append(Instruction(type="exclude", text=what))
            if m.group(2):
                out.removals.append(what)
        for m in REMOVE.finditer(t):
            out.removals.append(m.group(1).strip().lower())
        t = REMOVE.sub(" ", t)
        for m in NOTE.finditer(t):
            out.instructions.append(Instruction(type="note", text=m.group(1).strip().lower()))
        t = EXCLUDE.sub(" ", t)
        t = NOTE.sub(" ", t)
        for chunk in re.split(r"[,;.]|\band\b|\bplus\b|&", t, flags=re.I):
            chunk = chunk.strip().lower()
            if not chunk or GREETING.match(chunk):
                continue
            qty = None
            m = re.match(r"^(?:x\s*)?(\d+(?:\.\d+)?)\s*(?:x\s+)?(.*)$", chunk)
            if m:
                qty, chunk = Decimal(m.group(1)), m.group(2).strip()
            else:
                w = chunk.split(" ", 1)
                if w[0] in NUMS and len(w) > 1:
                    qty, chunk = Decimal(NUMS[w[0]]), w[1].strip()
            chunk = re.sub(r"^(?:of|packs? of|boxes? of)\s+", "", chunk)
            if not chunk:
                continue
            if chunk in AMBIGUOUS:
                out.items.append(ParsedItem(name=chunk, quantity=qty, question=AMBIGUOUS[chunk], confidence=0.3))
                continue
            sku = match_staple(chunk)
            out.items.append(ParsedItem(name=chunk, sku=sku, quantity=qty, confidence=0.9 if sku else 0.6))
        return out
