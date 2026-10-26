STORE = "No Frills Thornhill"
SERVICE_PREFIXES = {"L3T", "L4J"}

STAPLES = {
    "toilet_paper": {"name": "toilet paper", "aliases": ["toilet paper", "toilet roll", "toilet rolls", "tp", "cottonelle"], "unit": "pack"},
    "toothpaste": {"name": "toothpaste", "aliases": ["toothpaste"], "unit": "tube"},
    "bananas": {"name": "bananas", "aliases": ["banana", "bananas"], "unit": "bunch"},
    "avocados": {"name": "avocados", "aliases": ["avocado", "avocados"], "unit": "each"},
    "dish_soap": {"name": "dish soap", "aliases": ["dish soap", "dishsoap", "dish detergent"], "unit": "bottle"},
    "paper_towels": {"name": "paper towels", "aliases": ["paper towel", "paper towels"], "unit": "pack"},
}

AMBIGUOUS = {"fruit": "Which fruit do you want?", "fruits": "Which fruit do you want?",
             "veggies": "Which vegetables do you want?", "vegetables": "Which vegetables do you want?",
             "snacks": "Which snacks do you want?", "drinks": "Which drinks do you want?"}


def match_staple(text: str):
    t = text.strip().lower()
    best = None
    for sku, d in STAPLES.items():
        for a in d["aliases"]:
            if a == t or (a in t.split() if " " not in a else a in t):
                if best is None or len(a) > best[1]:
                    best = (sku, len(a))
    return best[0] if best else None

# Demo fixture prices (CAD) for STORE: sku -> (product, brand, pack size, regular, sale or None)
FIXTURE_PRICES = {
    "toilet_paper": ("Cottonelle Clean Care toilet paper", "Cottonelle", "12 double rolls", "13.99", "9.99"),
    "toothpaste": ("Colgate Cavity Protection toothpaste", "Colgate", "120 ml", "3.49", "2.50"),
    "bananas": ("Bananas", None, "bunch, about 1.2 kg", "1.89", None),
    "avocados": ("Hass avocado", None, "each", "1.99", "1.25"),
    "dish_soap": ("Dawn Ultra dish soap", "Dawn", "709 ml", "4.99", "3.97"),
    "paper_towels": ("Bounty paper towels", "Bounty", "6 rolls", "12.99", "9.99"),
}
