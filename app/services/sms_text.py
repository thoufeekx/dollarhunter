import math
import unicodedata

from app.util import short

GSM7 = set("@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà")
GSM7_EXT = set("^{}\\[~]|€\f")


_ASCII = str.maketrans({"\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"', "\u2013": "-", "\u2014": "-",
                        "\u2026": "...", "\u00a0": " ", "\u2022": "-"})


def to_gsm(body: str) -> str:
    """Keep texts in the cheap GSM-7 alphabet: one stray curly quote, (R) sign or unsupported accent switches
    the whole message to UCS-2 and roughly triples the number of billed segments."""
    out = []
    for ch in body.translate(_ASCII):
        if ch in GSM7 or ch in GSM7_EXT:
            out.append(ch)
        elif ch in "\u00ae\u2122\u00a9":  # registered, trademark, copyright signs: drop
            continue
        else:  # "\u00ee" -> "i", "\u00f4" -> "o"; anything without an ASCII base becomes "?"
            base = unicodedata.normalize("NFKD", ch).encode("ascii", "ignore").decode()
            out.append(base or "?")
    return "".join(out)


def segments(body: str) -> tuple[str, int]:
    if all(c in GSM7 or c in GSM7_EXT for c in body):
        n = sum(2 if c in GSM7_EXT else 1 for c in body)
        return "GSM-7", 1 if n <= 160 else math.ceil(n / 153)
    n = sum(2 if ord(c) > 0xFFFF else 1 for c in body)
    return "UCS-2", 1 if n <= 70 else math.ceil(n / 67)


INTRO = ("Dollar Hunter (prototype): text what you need, we build a basket and show the full cost before you approve. "
         "STOP to opt out, HELP for help. Your postal code?")
WELCOME_READY = ("Dollar Hunter (prototype): we price your basket before you approve. STOP to opt out, HELP for "
                 "help. What do you need? Text a list, photo, PDF or voice note.")
ASK_LIST = "Thanks. What do you need? Text your list, or send a receipt photo, PDF or voice note."
OUT_OF_AREA = ("Thanks. Our demo covers postal prefixes L3T and L4J only, and nothing is really delivered. "
               "You can still try the prototype. What do you need?")
BAD_POSTAL = "We could not read that postal code. Please send it like L3T 1A1."
HELP = ("Dollar Hunter prototype: text a list, receipt photo, PDF or voice note. BASKET shows it, QUOTE prices it, "
        "BUDGET 40 sets a cap, CLEAR empties. STOP ends texts.")
STOP_ACK = "You are unsubscribed from Dollar Hunter texts. Reply START to resume."
START_ACK = "Texts resumed. Nothing is ordered unless you approve a quote. What do you need?"
PRICING = "No subscription. We keep 25% of verified savings. No verified saving, no fee."
EMPTY_BASKET = "Your basket is empty. Text what you need, like: 2 avocados, toothpaste."
CLEARED = "Basket cleared. Text what you need."
NO_QUOTE = "No open quote. Reply QUOTE to get prices for your basket."
STALE_QUOTE = "Your basket changed after that quote. Reply QUOTE for an updated one."
EXPIRED_QUOTE = "That quote expired. Reply QUOTE for fresh prices."
BAD_CODE = "That code does not match your latest quote. Check the code and reply YES followed by the 6 digits."
NEED_QTY = "How many of each: {names}? Reply like \"{example}\"."
NO_SUGGESTIONS = "There are no receipt items waiting. Text your list or send a receipt."
VOICE_LIMIT = "You have reached today's voice note limit. Please type your list instead."
VOICE_BAD_FORMAT = "Sorry, we cannot play that voice note format. Please type your list instead."
VOICE_FAILED = "Sorry, we could not hear that clearly. Please try again or type your list."
AGENT_LIMIT = "You have reached today's message limit. Please try again tomorrow."
AGENT_FALLBACK = "Sorry, I did not catch that. Text what you need, like: 2 bananas and toothpaste."
HEARD = "Heard: \"{text}\"."
OVER_BUDGET = "That quote is over your budget, so it cannot be approved. Remove items or reply BUDGET and an amount, then QUOTE."
BUDGET_SET = "Budget set to {amount}. Reply QUOTE for prices within it."
LOCKED_QUOTE = "Too many wrong codes, so that quote is cancelled. Reply QUOTE for a new one."
NEED_CODE = "To approve, reply YES followed by the 6-digit code from your quote."
RECEIPT_LIMIT = "You have reached today's receipt limit. Please type your list instead, or try again tomorrow."
RECEIPT_BAD_FILE = "Please send a JPG or PNG photo, or a PDF (not password protected, up to 10 pages)."
RECEIPT_DUPLICATE = "You already sent this receipt."
RECEIPT_FAILED = "Sorry, we could not read that receipt. Please try a clearer photo or type your list."
RECEIPT_NOT_RECEIPT = "That does not look like a grocery receipt. Please send a receipt photo or type your list."


def pick_list(lines, store: str = "") -> str:
    """Numbered receipt suggestions the user picks from with ADD 1 3 or ADD ALL."""
    items = "; ".join(f"{n}) {f'{l.quantity.normalize():f} ' if l.quantity is not None else ''}{l.name}"
                      for n, l in enumerate(lines[:12], 1))
    more = f" (+{len(lines) - 12} more)" if len(lines) > 12 else ""
    return f"On your receipt{store}: {items}{more}. Which do you need this time? Reply ADD 1 3, or ADD ALL."


def options_text(want: str, items: list[dict]) -> str:
    """Numbered real options for one item; prices and stores come straight from the flyer/online data."""
    def one(n, i):
        unit = f"/{i['unit'].lstrip('/').lower()}" if i.get("unit") and i["unit"].lower() not in ("each", "ea", "ea.") else ""
        was = f", was ${i['was']}" if i.get("was") else ""
        src = "" if i.get("kind", "flyer") == "flyer" else " online"
        return f"{n}) {short(i['name'], 40)} ${i['price']}{unit} @{i['store']}{src}{was}"
    return f"Cheapest {want} near you: " + "; ".join(one(n, i) for n, i in enumerate(items, 1)) + \
        ". Reply with a number to add it."
