#!/usr/bin/env python3.13
"""Génère data.json pour la page sorties-cher à partir de l'API DATAtourisme.

Usage : python3.13 ~/sorties-cher/build_data.py
Appelé chaque jeudi par la tâche planifiée digest-sorties-hebdo.
Fusionne extras.yaml (événements ajoutés à la main) s'il existe.

Temporalités :
- ephemere   : événement daté court (≤ 7 jours)
- saisonnier : période limitée (expo d'été, animation de saison)
- recurrent  : rendez-vous régulier (marchés, "tous les lundis", plage quasi annuelle)
- toujours   : lieux/activités permanents (châteaux, musées, kayak, montgolfière)
"""

import datetime
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.expanduser("~/.claude/skills/sorties/scripts/datatourisme.py")
WEEKS = 3

# Buckets d'usage : premier type qui matche gagne (ordre = priorité)
BUCKETS = [
    ("marches", "Marchés & terroir", {"Market", "SaleEvent", "FairOrShow", "BricABrac", "GarageSale"}),
    ("concerts", "Concerts & spectacles", {"Concert", "MusicEvent", "TheaterEvent", "ShowEvent",
                                           "OperaOrOperetta", "Recital", "DanceEvent", "CircusEvent", "Carnival"}),
    ("expos", "Expos & culture", {"Exhibition", "VisualArtsEvent", "Conference", "LocalAnimation",
                                  "CulturalEvent", "PilgrimageAndProcession", "ReligiousEvent"}),
    ("sport", "Sport & nature", {"SportsEvent", "SportsCompetition", "SportsDemonstration", "Rambling",
                                 "Rally", "BikeTrip", "HorseShow", "Game"}),
    ("patrimoine", "Châteaux & patrimoine", {"Castle", "Museum", "ReligiousSite", "Abbey", "Church",
                                             "RemarkableBuilding", "CityHeritage", "CulturalSite",
                                             "ArcheologicalSite", "TechnicalHeritage", "Cave", "TroglodyteVillage"}),
    ("nature", "Nature & loisirs", {"ParkAndGarden", "NaturalHeritage", "ZooAnimalPark", "ThemePark",
                                    "SportsAndLeisurePlace", "LeisureComplex", "Park", "NaturalSite",
                                    "PicnicArea", "ViewPoint", "WineCellar"}),
]

# Types de lieux permanents à cataloguer (temporalite "toujours")
PERMANENT_TYPES = ("Castle,Museum,ParkAndGarden,NaturalHeritage,ZooAnimalPark,ThemePark,"
                   "SportsAndLeisurePlace,Cave,TroglodyteVillage,Abbey,ArcheologicalSite,WineCellar")

JOURS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
RE_RECURRENT = re.compile(r"\b(tous les|chaque|hebdomadaire|toutes les semaines|1er \w+ du mois|"
                          r"premier \w+ du mois|mensuel)\b", re.I)


def bucket_of(types):
    tset = set(types)
    for key, _, members in BUCKETS:
        if tset & members:
            return key
    return "autres"


def ring_of(km):
    if km is None:
        return 60
    return 15 if km <= 13 else 30 if km <= 28 else 45 if km <= 45 else 60


def jours_of(text):
    """Extrait les jours de semaine mentionnés dans un texte ('tous les lundis...')."""
    found = []
    low = (text or "").lower()
    for i, j in enumerate(JOURS):
        if re.search(rf"\b{j}s?\b", low):
            found.append(i)  # 0 = lundi
    return found


def temporalite_of(du, au, label, desc):
    text = f"{label or ''} {desc or ''}"
    if not du:
        return "recurrent", []
    d1, d2 = datetime.date.fromisoformat(du), datetime.date.fromisoformat(au or du)
    span = (d2 - d1).days
    if RE_RECURRENT.search(text) or span > 240:
        return "recurrent", jours_of(text)
    if span <= 7:
        return "ephemere", []
    return "saisonnier", []


def run_query(args_list):
    res = subprocess.run(["python3.13", SCRIPT] + args_list, capture_output=True, text=True)
    if res.returncode != 0:
        sys.exit(f"Échec datatourisme.py {' '.join(args_list)} : {res.stderr}")
    return [json.loads(l) for l in res.stdout.splitlines() if l.strip()]


def load_extras():
    """extras.yaml : liste d'événements ajoutés à la main (affiches, bouche-à-oreille).
    Format minimal : label, du (AAAA-MM-JJ), ville ; optionnels : au, desc, url, km, bucket."""
    path = os.path.join(HERE, "extras.yaml")
    if not os.path.exists(path):
        return []
    try:
        import yaml
        entries = yaml.safe_load(open(path)) or []
    except ImportError:
        print("PyYAML absent — extras.yaml ignoré", file=sys.stderr)
        return []
    out = []
    for e in entries:
        e.setdefault("au", e.get("du"))
        e.setdefault("bucket", "autres")
        e["source"] = "manuel"
        out.append(e)
    return out


def main():
    today = datetime.date.today()
    seen, items = set(), []

    def add(o, temporalite=None, jours=None):
        key = (o.get("label", "").strip().lower(), (o.get("ville") or "").strip().lower())
        if key in seen or not o.get("label"):
            return
        seen.add(key)
        du, au = o.get("du"), o.get("au") or o.get("du")
        if temporalite is None:
            if au and au < today.isoformat():
                return
            temporalite, jours = temporalite_of(du, au, o.get("label"), o.get("desc"))
        items.append({
            "label": o.get("label"),
            "ville": (o.get("ville") or "").replace(" Val de Cher", ""),
            "km": o.get("km"),
            "ring": ring_of(o.get("km")),
            "du": du, "au": au,
            "temporalite": temporalite,
            "jours": jours or [],
            "bucket": bucket_of(o.get("types", [])),
            "enfants": "ChildrensEvent" in o.get("types", []),
            "desc": o.get("desc"),
            "url": o.get("url"),
            "photo": o.get("photo"),
        })

    # 1. Événements datés (3 semaines, rayon 1h)
    for o in run_query(["--ring", "60", "--endpoint", "entertainmentAndEvent",
                        "--weeks", str(WEEKS), "--page-size", "250", "--max-pages", "4"]):
        add(o)

    # 2. Lieux & activités permanents ("toujours")
    for o in run_query(["--ring", "60", "--endpoint", "placeOfInterest",
                        "--types", PERMANENT_TYPES, "--page-size", "250", "--max-pages", "2",
                        "--fields", "uuid,label,type,isLocatedAt.geo,isLocatedAt.address,"
                                    "hasDescription.shortDescription,hasContact,hasMainRepresentation"]):
        add(o, temporalite="toujours", jours=[])

    # 3. Ajouts manuels
    for e in load_extras():
        if (e.get("au") or "") >= today.isoformat():
            e["ring"] = ring_of(e.get("km"))
            e.setdefault("temporalite", "ephemere")
            e.setdefault("jours", [])
            e.setdefault("enfants", False)
            items.append(e)

    items.sort(key=lambda e: (e.get("du") or "9999", e.get("km") or 99))
    data = {
        "generated": today.isoformat(),
        "window_weeks": WEEKS,
        "buckets": [{"key": k, "label": l} for k, l, _ in BUCKETS] + [{"key": "autres", "label": "Visites & autres"}],
        "events": items,
    }
    out = os.path.join(HERE, "data.json")
    with open(out, "w") as f:
        json.dump(data, f, ensure_ascii=False)
    from collections import Counter
    c = Counter(e["temporalite"] for e in items)
    print(f"{out}: {len(items)} entrées ({dict(c)}), généré le {today}")


if __name__ == "__main__":
    main()
