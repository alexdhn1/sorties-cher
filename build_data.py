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
import math
import os
import re
import statistics
import subprocess
import sys
import unicodedata
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.expanduser("~/.claude/skills/sorties/scripts/datatourisme.py")
WEEKS = 3

# Buckets d'usage : premier type qui matche gagne (ordre = priorité).
# sport AVANT expos (sinon les SportsEvent+CulturalEvent tombaient en expos) ;
# CulturalEvent (fourre-tout DATAtourisme) traité en repli, pas en membre.
BUCKETS = [
    ("marches", "Marchés & terroir", {"Market", "SaleEvent", "FairOrShow", "BricABrac", "GarageSale"}),
    ("concerts", "Concerts & spectacles", {"Concert", "MusicEvent", "TheaterEvent", "ShowEvent", "Festival",
                                           "OperaOrOperetta", "Recital", "DanceEvent", "CircusEvent", "Carnival"}),
    ("sport", "Sport & rando", {"SportsEvent", "SportsCompetition", "SportsDemonstration", "Rambling",
                                "Rally", "BikeTrip", "HorseShow", "Game"}),
    ("expos", "Expos & culture", {"Exhibition", "ExhibitionEvent", "VisualArtsEvent", "Conference",
                                  "LocalAnimation", "PilgrimageAndProcession", "ReligiousEvent"}),
    ("ateliers", "Ateliers & découvertes", set()),  # bucket purement lexical
    ("patrimoine", "Châteaux & patrimoine", {"Castle", "Museum", "ReligiousSite", "Abbey", "Church",
                                             "RemarkableBuilding", "CityHeritage", "CulturalSite",
                                             "ArcheologicalSite", "TechnicalHeritage", "Cave", "TroglodyteVillage"}),
    ("degust", "Vins & dégustations", {"WineCellar"}),
    ("nature", "Nature & jardins", {"ParkAndGarden", "NaturalHeritage", "Park", "NaturalSite",
                                    "PicnicArea", "ViewPoint"}),
    ("loisirs", "Loisirs & sensations", {"ZooAnimalPark", "ThemePark", "SportsAndLeisurePlace", "LeisureComplex"}),
]

# Repli lexical pour les ~250 entrées sans aucun type exploitable (Event nu).
# Ordre significatif : « marché » (accent) avant les motifs de balade/marche.
LEX_BUCKETS = [
    ("marches", r"marché|brocante|foire|vide[- ]grenier|braderie|producteurs?\b"),
    ("degust", r"dégustation|[oœ]nolog|vigneron|vignoble|brunch|\bcave\b|\bvins?\b"),
    ("concerts", r"concert|spectacle|théâtre|festival|cinéma|\bbal\b|musique|scène ouverte|danse"),
    ("sport", r"randonnée|\brando\b|balade|marche (nocturne|gourmande)|vélo|canoë|kayak|trail|course|tournoi"
              r"|pétanque|yoga|qi ?gong|réflexolog|sophrolog|bien[- ]être"),
    ("nature", r"castor|rapaces?|libellules?|papillons?|oiseaux?|faune|flore|réserve naturelle"
               r"|étoiles?|éclipse|astronom|observatoire"),
    ("ateliers", r"atelier|stage|initiation|démonstration|portes ouvertes|\bcours\b"),
    ("expos", r"\bexpo|musée|conférence|lecture|dédicace"),
    ("patrimoine", r"visite|château|patrimoine|église|abbaye|donjon|monument|escape|enquête|chasse au trésor"),
]

# Déchets identifiés : bornes de recharge, immobilier, annulés/reportés
REJECT_TYPES = {"ElectricVehicleChargingPoint", "NonHousingRealEstateRental"}
RE_REJECT = re.compile(r"^\s*(annul[ée]|report[ée])|borne de (re)?charge|coworking|coliwork", re.I)
# Équipements municipaux et commerces qui ne sont pas des sorties — appliqué UNIQUEMENT
# à la passe placeOfInterest pour ne pas tuer un vrai événement (« Fête de la pêche »…).
RE_REJECT_PERM = re.compile(
    r"ponton de p[êe]che|plan d['’]eau|[ée]tang (communal|de|des|du)|p[êe]che (au|aux|à|en|sur)"
    r"|parcours de p[êe]che|city ?stade|aire de jeux|aire de (pique|pic)[- ]?nique"
    r"|piscine (municipale|communautaire|intercommunale)|centre (aquatique|aqualudique)|espace aquatique"
    r"|boulodrome|terrain multisport|r[ée]paration|\bgarage\b|(location|vente) de v[ée]los?"
    r"|accueil v[ée]lo|point info|bateau vip|privatisation|s[ée]minaires?\b", re.I)
# Signaux FORTS uniquement, évalués sur le texte réellement affiché (label + desc clippée) :
# l'ancienne version marquait 27 % du corpus via des formules commerciales (« entre amis ou en famille »).
RE_ENFANTS = re.compile(r"jeune public|d[èé]s \d+ ans|à partir de \d+ ans|pour les enfants"
                        r"|ateliers? enfants?|\bfamilial|en famille", re.I)
RE_RESERV = re.compile(r"sur r[ée]servation|r[ée]servation (obligatoire|conseill|indispensable|recommand)"
                       r"|inscription obligatoire|places limit[ée]es", re.I)

# Types de lieux permanents à cataloguer (temporalite "toujours")
PERMANENT_TYPES = ("Castle,Museum,ParkAndGarden,NaturalHeritage,ZooAnimalPark,ThemePark,"
                   "SportsAndLeisurePlace,Cave,TroglodyteVillage,Abbey,ArcheologicalSite,WineCellar")

# Repli ville→km quand aucune entrée DATAtourisme ne donne la distance (communes
# proches des sources mairies) : haversine depuis la maison, clés normalisées norm_key.
MAISON = (47.3075, 1.1413)
COMMUNES_GPS = {
    "saintgeorgessurcher": (47.3086, 1.1256), "montrichard": (47.3431, 1.1867),
    "faverollessurcher": (47.3260, 1.1859), "bourre": (47.3475, 1.2186),
    "chissayentouraine": (47.3383, 1.1329), "saintjuliendechedon": (47.3253, 1.2233),
    "ange": (47.3157, 1.2419), "pouille": (47.3103, 1.3006), "thesee": (47.3236, 1.3084),
    "monthousurcher": (47.3487, 1.2848), "pontlevoy": (47.3903, 1.2547),
    "chenonceaux": (47.3336, 1.0654), "chisseaux": (47.3390, 1.0800),
    "civraydetouraine": (47.3320, 1.0500), "blere": (47.3268, 0.9910),
    "cerelaronde": (47.2620, 1.0910), "valliereslesgrandes": (47.4031, 1.1153),
    "saintaignan": (47.2690, 1.3760),
}
# Nom d'affichage par clé normalisée — sert à résoudre la commune réelle depuis le
# LOCATION iCal (« Stade de foot de Bourré » ⇒ Bourré, pas Montrichard).
COMMUNES_NOMS = {
    "saintgeorgessurcher": "Saint-Georges-sur-Cher", "montrichard": "Montrichard",
    "faverollessurcher": "Faverolles-sur-Cher", "bourre": "Bourré",
    "chissayentouraine": "Chissay-en-Touraine", "saintjuliendechedon": "Saint-Julien-de-Chédon",
    "ange": "Angé", "pouille": "Pouillé", "thesee": "Thésée",
    "monthousurcher": "Monthou-sur-Cher", "pontlevoy": "Pontlevoy",
    "chenonceaux": "Chenonceaux", "chisseaux": "Chisseaux",
    "civraydetouraine": "Civray-de-Touraine", "blere": "Bléré",
    "cerelaronde": "Céré-la-Ronde", "valliereslesgrandes": "Vallières-les-Grandes",
    "saintaignan": "Saint-Aignan",
}
# Correctif « vol d'oiseau → minutes de route » : communes séparées de la maison par
# le Cher ou la forêt (détour obligé) ⇒ ≥ 20 min de route malgré ≤ 13 km haversine.
RING_MIN = {"cerelaronde": 30, "blere": 30, "monthousurcher": 30,
            "pouille": 30, "thesee": 30, "pontlevoy": 30}

JOURS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
RE_RECURRENT = re.compile(r"\b(tous les|chaque|hebdomadaire|toutes les semaines|1er \w+ du mois|"
                          r"premier \w+ du mois|mensuel)\b", re.I)


def bucket_of(types, label="", desc=""):
    tset = set(types)
    for key, _, members in BUCKETS:
        if tset & members:
            return key
    text = f"{label or ''} {desc or ''}".lower()
    for key, pat in LEX_BUCKETS:
        if re.search(pat, text):
            return key
    if "CulturalEvent" in tset:
        return "expos"
    return "autres"


def commune_km(ville):
    gps = COMMUNES_GPS.get(norm_key((ville or "").replace(" Val de Cher", "")))
    if not gps:
        return None
    la1, lo1, la2, lo2 = map(math.radians, (*MAISON, *gps))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return round(2 * 6371 * math.asin(math.sqrt(h)), 1)


def ring_of(km):
    if km is None:
        return 60
    return 15 if km <= 13 else 30 if km <= 28 else 45 if km <= 45 else 60


def norm_key(s):
    """Clé de dédup : NFD sans accents, minuscules, raisons sociales et ponctuation écrasées."""
    s = unicodedata.normalize("NFD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    s = re.sub(r"\b(sarl|sas|eurl)\b", "", s)
    return re.sub(r"[^a-z0-9]+", "", s)


def norm_spaced(s):
    """Comme norm_key mais mots séparés par des espaces (recherche à frontière de mot)."""
    s = unicodedata.normalize("NFD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


# motifs « nom de commune » pré-normalisés, à frontière de mot
_COMMUNES_PAT = {k: f" {norm_spaced(nom)} " for k, nom in COMMUNES_NOMS.items()}


def ville_from_lieu(lieu, ville):
    """Commune détectée dans le LOCATION iCal, prioritaire sur la commune de la source :
    « Centre Socio Culturel Bourré » publié par Montrichard est à Bourré (~3 km plus loin)."""
    if not lieu:
        return None
    hay = f" {norm_spaced(lieu)} "
    nv = norm_key(ville)
    for key, pat in _COMMUNES_PAT.items():
        if key != nv and pat in hay:
            return COMMUNES_NOMS[key]
    return None


STOPWORDS = {"les", "des", "une", "aux", "sur", "dans", "pour", "avec", "par",
             "chez", "est", "ses", "son", "the", "and"}


def label_tokens(s):
    """Tokens significatifs d'un libellé (quasi-doublons : « Concert Rock on the Dock »
    vs « Rock on the dock débarque à Montrichard »)."""
    return {t for t in norm_spaced(s).split() if len(t) >= 3 and t not in STOPWORDS}


def jours_occurrences(periodes):
    """Jours de semaine dominants dérivés des occurrences datées (chacune ≤ 2 jours).
    ≥ 3 occurrences, jours couvrant ≥ 15 % des dates (min 2), et ≤ 3 jours distincts."""
    days = []
    for p in periodes:
        d1 = datetime.date.fromisoformat(p["du"])
        d2 = datetime.date.fromisoformat(p.get("au") or p["du"])
        if (d2 - d1).days > 2:
            return []
        while d1 <= d2:
            days.append(d1.weekday())
            d1 += datetime.timedelta(days=1)
    if len(periodes) < 3 or not days:
        return []
    cnt = Counter(days)
    keep = sorted(j for j, n in cnt.items() if n >= 2 and n >= 0.15 * len(days))
    return keep if 0 < len(keep) <= 3 else []


def jours_of(text):
    """Extrait les jours de semaine mentionnés dans un texte ('tous les lundis...')."""
    low = (text or "").lower()
    if re.search(r"tous les jours|7 ?j/7", low):
        return list(range(7))
    found = set()
    for i, j in enumerate(JOURS):
        if re.search(rf"\b{j}s?\b", low):
            found.add(i)  # 0 = lundi
    m = re.search(rf"du ({'|'.join(JOURS)}) au ({'|'.join(JOURS)})", low)
    if m:
        a, b = JOURS.index(m.group(1)), JOURS.index(m.group(2))
        found.update(range(a, b + 1) if a <= b else list(range(a, 7)) + list(range(0, b + 1)))
    # contexte négatif : « sauf le mardi », « fermé le lundi », « relâche le jeudi »
    for neg in re.finditer(r"(sauf|fermé[es]?|relâche)[^.!;]{0,50}", low):
        for i, j in enumerate(JOURS):
            if re.search(rf"\b{j}s?\b", neg.group(0)):
                found.discard(i)
    return sorted(found)


def temporalite_of(du, au, label, desc, jours_struct=None):
    """jours_struct = jours d'ouverture structurés (openingHoursSpecification),
    prioritaires sur la regex texte. Ordre des règles :
    1. span ≤ 7 → ephemere, toujours (un concert daté qui dit « chaque été » reste ponctuel)
    2. span > 240 → recurrent seulement si un rythme est détecté, sinon toujours
    3. sinon regex récurrence → recurrent, sinon saisonnier."""
    text = f"{label or ''} {desc or ''}"
    if not du:
        return "recurrent", (jours_struct or jours_of(text))
    d1, d2 = datetime.date.fromisoformat(du), datetime.date.fromisoformat(au or du)
    span = (d2 - d1).days
    if span <= 7:
        return "ephemere", []
    jours = jours_struct or jours_of(text)
    if span > 240:
        if jours or RE_RECURRENT.search(text):
            return "recurrent", jours
        return "toujours", []
    if RE_RECURRENT.search(text):
        return "recurrent", jours
    return "saisonnier", []


def clip(s, lim=280):
    """Troncature propre : fin de phrase avant lim, sinon dernier espace + ellipse."""
    s = re.sub(r"\s+", " ", s or "").strip()
    if len(s) <= lim:
        return s or None
    cut = s[:lim]
    p = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "), cut.rfind("… "))
    if p >= 120:
        return cut[:p + 1]
    return cut[:max(cut.rfind(" "), lim - 20)] + "…"


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


def load_sources_events():
    """Événements structurés des sources locales : sources/collected.json (iCal/RSS
    de fetch_sources.py) et sources/scraped.json (validés par la tâche hebdo).
    Même format que data.json + champ "source" ("mairie:<commune>", "facebook:<page>")."""
    out = []
    for fname in ("collected.json", "scraped.json"):
        path = os.path.join(HERE, "sources", fname)
        if not os.path.exists(path):
            continue
        try:
            with open(path) as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            print(f"sources/{fname} illisible ({e}) — ignoré", file=sys.stderr)
            continue
        if isinstance(data, dict) and data.get("errors"):
            for err in data["errors"]:
                sup = f" ({err['reprise_cache']})" if err.get("reprise_cache") else ""
                print(f"⚠ sources/{fname}: collecte dégradée — {err.get('source', '?')}: "
                      f"{err.get('erreur', '?')}{sup}", file=sys.stderr)
        events = data.get("events", []) if isinstance(data, dict) else data
        for ev in events or []:
            if isinstance(ev, dict) and ev.get("label"):
                out.append(ev)
    return out


def main():
    today = datetime.date.today()
    seen, items, rejected = set(), [], [0]

    def add(o, temporalite=None, jours=None, permanent=False, source=None):
        label = (o.get("label") or "").strip()
        ville = ((o.get("ville") or "").replace(" Val de Cher", "")).strip()
        # scories : caractères invisibles, espaces doublés, guillemets encadrants, raisons sociales
        label = re.sub("[\u200b\u200e\u200f\u2060\ufeff]", "", label)
        label = re.sub(r"\s+", " ", label).strip().strip('"“”«» ')
        label = re.sub(r"\s+(sarl|sas|eurl)\.?$", "", label, flags=re.I).strip()
        if not label:
            return
        if (REJECT_TYPES & set(o.get("types", [])) or RE_REJECT.search(label)
                or (permanent and RE_REJECT_PERM.search(label))):
            rejected[0] += 1
            return
        # dédup : uuid (stable), puis label normalisé × ville normalisée, et label × km arrondi
        # (attrape variantes de ponctuation et doublons inter-communes ; les événements datés
        # passent en premier, donc en collision la version datée gagne)
        nl = norm_key(label)
        keys = {(nl, norm_key(ville))}
        if o.get("km") is not None:
            keys.add((nl, round(o["km"])))
        if o.get("uuid"):
            keys.add(o["uuid"])
        if keys & seen:
            return
        seen.update(keys)
        if label.isupper():
            label = label.capitalize()
        du, au = o.get("du"), o.get("au") or o.get("du")
        # filtre anti-passé : s'applique AUSSI aux sources locales qui déclarent leur
        # propre temporalite (sinon un scraped.json périmé republierait l'événement)
        if temporalite != "toujours" and au and au < today.isoformat():
            return
        # occurrences futures, dédupliquées sur (du, au) — la source répète souvent la même période
        periodes_fut, seen_p = [], set()
        for p in o.get("periodes") or []:
            pau = p.get("au") or p["du"]
            if pau < today.isoformat() or (p["du"], pau) in seen_p:
                continue
            seen_p.add((p["du"], pau))
            periodes_fut.append({"du": p["du"], "au": pau})
        if temporalite is None:
            # temporalité calculée sur l'ENVELOPPE des occurrences, pas la seule période courante
            env_du, env_au = du, au
            if periodes_fut:
                env_du = min(p["du"] for p in periodes_fut)
                env_au = max(p["au"] for p in periodes_fut)
            temporalite, jours = temporalite_of(env_du, env_au, label, o.get("desc"), o.get("jours_ouv"))
            if temporalite != "toujours" and len(periodes_fut) >= 3 and env_du:
                span_env = (datetime.date.fromisoformat(env_au)
                            - datetime.date.fromisoformat(env_du)).days
                if span_env > 21:
                    if span_env / len(periodes_fut) <= 14:
                        # rythme ~hebdomadaire ou mieux → vrai rendez-vous régulier
                        temporalite = "recurrent"
                    elif all((datetime.date.fromisoformat(p["au"])
                              - datetime.date.fromisoformat(p["du"])).days <= 3
                             for p in periodes_fut):
                        # dates courtes ESPACÉES (collecte de sang mensuelle…) :
                        # occurrences ponctuelles, pas « chaque semaine »
                        temporalite = "ephemere"
            if temporalite == "recurrent" and not jours:
                jours = jours_occurrences(periodes_fut)
        desc = clip(o.get("desc"))
        if desc and desc.lower().startswith(label.lower()):  # desc qui répète le titre
            rest = desc[len(label):].lstrip(" .:-–—!")
            if rest and rest[0].islower():  # le titre coupe une phrase → sauter la phrase entière
                p = rest.find(". ")
                rest = rest[p + 2:] if p != -1 else ""
            desc = rest or None
        if desc and len(desc) < 25:
            desc = None
        periodes = periodes_fut[:24] if len(periodes_fut) > 1 else None
        prix = o.get("prix")
        if prix and prix != "gratuit":
            m = re.match(r"^(\d+(?:,\d+)?) €$", prix)
            if m:
                v = float(m.group(1).replace(",", "."))
                # au-delà de ~80 € c'est un tarif groupe/privatisation : trompeur sans contexte
                prix = None if v > 80 else f"dès {prix}"
        photo = o.get("photo")
        if photo and not re.search(r"\.(jpe?g|png|webp|gif)(\?|$)", photo, re.I):
            photo = None  # URL tronquée (« …/upload/ ») → image morte
        item = {
            "label": label,
            "ville": ville,
            "km": o.get("km"),
            # ring : minutes de route réelles ≥ estimation vol d'oiseau (RING_MIN)
            "ring": max(ring_of(o.get("km")), RING_MIN.get(norm_key(ville), 0)),
            "du": du, "au": au,
            "periodes": periodes,
            "temporalite": temporalite,
            "jours": jours or [],
            "bucket": bucket_of(o.get("types", []), label, o.get("desc")),
            "enfants": ("ChildrensEvent" in o.get("types", [])
                        or bool(RE_ENFANTS.search(f"{label} {desc or ''}"))),
            "reserv": bool(RE_RESERV.search(o.get("desc") or "")),
            "desc": desc,
            "pratique": " · ".join(x for x in (o.get("h"), prix) if x) or None,
            "url": o.get("url"),
            "photo": photo,
            "source": source,
        }
        # ne pas émettre les champs vides (jours/enfants gérés côté JS) — ~50 Ko économisés
        items.append({k: v for k, v in item.items()
                      if not (v is None or v == "" or v == [] or v is False)})

    # 1. Événements datés (3 semaines, rayon 1h)
    for o in run_query(["--ring", "60", "--endpoint", "entertainmentAndEvent",
                        "--weeks", str(WEEKS), "--page-size", "250", "--max-pages", "4"]):
        add(o)

    # 2. Lieux & activités permanents ("toujours") — rayon 45 : au-delà, un lieu permanent
    # n'est plus « à moins d'une heure » réelle (Brenne, Poitou) et gonfle data.json pour rien.
    # Les ÉVÉNEMENTS lointains, eux, restent à 60 (un festival vaut le déplacement).
    for o in run_query(["--ring", "45", "--endpoint", "placeOfInterest",
                        "--types", PERMANENT_TYPES, "--page-size", "250", "--max-pages", "5",
                        "--fields", "uuid,label,type,isLocatedAt.geo,isLocatedAt.address,"
                                    "isLocatedAt.openingHoursSpecification,hasDescription,"
                                    "hasContact,hasMainRepresentation,offers"]):
        add(o, temporalite="toujours", jours=[], permanent=True)

    # 3. Sources locales (mairies via fetch_sources.py, Facebook validé par la tâche hebdo).
    # Passées APRÈS DATAtourisme : en cas de doublon (norm label × ville), DATAtourisme
    # gagne — ses fiches sont plus riches (photo, prix, géoloc précise).
    # km : médiane DATAtourisme par ville normalisée, repli haversine COMMUNES_GPS.
    km_by_ville = {}
    for e in items:
        if e.get("km") is not None and e.get("ville"):
            km_by_ville.setdefault(norm_key(e["ville"]), []).append(e["km"])
    km_by_ville = {v: round(statistics.median(kms), 1) for v, kms in km_by_ville.items()}
    # Même label × ville à plusieurs dates (« Collecte de sang » mensuelle…) : fusion
    # en une entrée à periodes multiples AVANT add(), sinon la dédup ne garde que la 1re.
    grouped = {}
    for o in load_sources_events():
        key = (norm_key(o.get("label") or ""),
               norm_key((o.get("ville") or "").replace(" Val de Cher", "")))
        g = grouped.get(key)
        if g and g.get("du") and o.get("du"):
            g.setdefault("periodes", [{"du": g["du"], "au": g.get("au") or g["du"]}])
            g["periodes"].append({"du": o["du"], "au": o.get("au") or o["du"]})
            g["du"] = min(p["du"] for p in g["periodes"])
            g["au"] = max(p["au"] for p in g["periodes"])
            # champs propres à UNE occurrence (« Mercredi 2 septembre ⏰ 15h ») :
            # contradictoires une fois fusionnés → supprimés s'ils diffèrent
            for f in ("desc", "h"):
                if g.get(f) != o.get(f):
                    g.pop(f, None)
        else:
            grouped[key] = o
    n_src = 0
    for o in grouped.values():
        ville = (o.get("ville") or "").replace(" Val de Cher", "").strip()
        if norm_key(ville) == "interco":
            ville = ""  # pas une commune : ni « interco » affiché, ni km inventé
        # commune réelle depuis le LOCATION iCal (communes déléguées : Bourré…)
        v2 = ville_from_lieu(o.get("lieu"), ville)
        if v2:
            ville = v2
        o["ville"] = ville
        if o.get("km") is None and ville:
            o["km"] = km_by_ville.get(norm_key(ville))
            if o["km"] is None:
                o["km"] = commune_km(ville)
        # quasi-doublons intra-source ou mairie↔DATAtourisme : même ville, dates qui se
        # recouvrent, libellés partageant l'essentiel de leurs tokens → on garde le 1er
        t1 = label_tokens(o.get("label")) - label_tokens(ville)
        du1, au1 = o.get("du"), o.get("au") or o.get("du")
        dup = None
        if t1 and du1:
            nv = norm_key(ville)
            for e in items:
                if not e.get("du") or norm_key(e.get("ville") or "") != nv:
                    continue
                if (e.get("au") or e["du"]) < du1 or e["du"] > au1:
                    continue
                t2 = label_tokens(e["label"]) - label_tokens(ville)
                inter = t1 & t2
                if len(inter) >= 2 and len(inter) >= 0.6 * min(len(t1), len(t2)):
                    dup = e["label"]
                    break
        if dup:
            print(f"quasi-doublon ignoré : « {o.get('label')} » ≈ « {dup} »", file=sys.stderr)
            continue
        before = len(items)
        add(o, temporalite=o.get("temporalite"), jours=o.get("jours"),
            source=str(o.get("source") or "manuel"))
        n_src += len(items) - before
    print(f"sources locales : {n_src} événements ajoutés", file=sys.stderr)

    # 4. Ajouts manuels
    for e in load_extras():
        if (e.get("au") or "") >= today.isoformat():
            e["ring"] = ring_of(e.get("km"))
            e.setdefault("temporalite", "ephemere")
            e.setdefault("jours", [])
            e.setdefault("enfants", False)
            items.append(e)

    # Descriptions boilerplate (texte d'office de tourisme dupliqué) : vidées si vues ≥ 3 fois
    cnt = Counter(e["desc"][:120] for e in items if e.get("desc"))
    for e in items:
        if e.get("desc") and cnt[e["desc"][:120]] >= 3:
            del e["desc"]

    items.sort(key=lambda e: (e.get("du") or "9999", e.get("km") or 99))
    # horizon réel de l'agenda : les sources locales (iCal mairie) portent des dates
    # bien au-delà des WEEKS semaines DATAtourisme — la vue Agenda doit les couvrir
    horizon_end = today + datetime.timedelta(days=WEEKS * 7)
    for e in items:
        if e.get("temporalite") == "toujours":
            continue
        fins = [p["au"] for p in e.get("periodes") or []]
        if not fins and e.get("temporalite") == "ephemere" and e.get("au"):
            fins = [e["au"]]
        for fin in fins:
            d = datetime.date.fromisoformat(fin)
            if d > horizon_end:
                horizon_end = d
    data = {
        "generated": today.isoformat(),
        "window_weeks": WEEKS,
        "horizon_jours": min((horizon_end - today).days + 1, 180),
        "buckets": [{"key": k, "label": l} for k, l, _ in BUCKETS] + [{"key": "autres", "label": "Visites & autres"}],
        "events": items,
    }
    out = os.path.join(HERE, "data.json")
    with open(out, "w") as f:
        json.dump(data, f, ensure_ascii=False)
    c = Counter(e["temporalite"] for e in items)
    print(f"{out}: {len(items)} entrées ({dict(c)}), {rejected[0]} rejetées, généré le {today}")


if __name__ == "__main__":
    main()
