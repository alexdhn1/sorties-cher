#!/usr/bin/env python3.13
"""Collecte des sources municipales (sources/mairies.yaml) → sources/collected.json.

Usage : python3.13 ~/sorties-cher/fetch_sources.py
Appelé avant build_data.py (tâche hebdo digest-sorties-hebdo).

Trois modes de récolte :
- ical (champ 'ical' du yaml, ex. Montrichard) : parse DTSTART/DTEND/SUMMARY/... →
  événements STRUCTURÉS, format identique à data.json + "source": "mairie:<commune>".
- rss / rss-js : flux d'actualités (intercos, Céré) → items "a_extraire" (titre, lien,
  date de publication, résumé). AUCUNE date d'événement inventée : l'extraction
  sémantique est faite par la tâche hebdo Claude.
- html : snapshot texte brut (liens conservés) dans sources/cache/<slug>.txt,
  tronqué à ~40 000 caractères (troncature signalée). Extraction laissée à Claude.

Une source qui échoue est loggée, consignée dans collected.json ("errors", "ok")
et sa récolte du run précédent est reprise (événements iCal, items RSS) pour
qu'une panne passagère ne vide jamais la page. Un snapshot html périmé reste sur
disque mais n'est plus listé dans "snapshots".
"""

import datetime
import html
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
SOURCES = os.path.join(HERE, "sources")
CACHE = os.path.join(SOURCES, "cache")
UA = "sorties-cher/1.0 usage personnel"
TIMEOUT = 15
SNAPSHOT_MAX = 40000
DELAY = 1.0
RSS_MAX_AGE = 90        # jours : au-delà, un item RSS n'est plus "à extraire"
RRULE_MAX_OCC = 60      # bornes d'expansion RRULE
RRULE_HORIZON = 365


def log(msg):
    print(msg, file=sys.stderr)


def slugify(s):
    s = unicodedata.normalize("NFD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        raw = r.read()
        ctype = r.headers.get("Content-Type", "")
    m = re.search(r"charset=([\w-]+)", ctype)
    enc = m.group(1) if m else "utf-8"
    try:
        return raw.decode(enc, errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


# ---------------------------------------------------------------- extraction texte HTML

class TextExtractor(HTMLParser):
    """Texte brut d'une page : blocs sur des lignes, liens conservés en 'texte (url)'."""
    BLOCK = {"p", "div", "li", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "br",
             "section", "article", "header", "footer", "ul", "ol", "table"}
    SKIP = {"script", "style", "noscript", "svg", "iframe"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out, self._skip, self._href = [], 0, None

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1
        elif tag == "a":
            href = dict(attrs).get("href") or ""
            self._href = href if href.startswith("http") else None
        elif tag in self.BLOCK:
            self.out.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self._skip = max(0, self._skip - 1)
        elif tag == "a":
            if self._href:
                self.out.append(f" ({self._href})")
            self._href = None
        elif tag in self.BLOCK:
            self.out.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.out.append(data)


def html_to_text(page):
    p = TextExtractor()
    p.feed(page)
    text = "".join(p.out)
    lines = [re.sub(r"[ \t]+", " ", l).strip() for l in text.split("\n")]
    text = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def strip_tags(s):
    """Version inline (résumés RSS, descriptions iCal HTML)."""
    s = re.sub(r"<br ?/?>|</p>", "\n", s or "", flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


# ---------------------------------------------------------------- iCal

def ical_unfold(text):
    """Déplie les lignes continuées (RFC 5545 : continuation = espace/tab en tête)."""
    lines = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        else:
            lines.append(raw)
    return lines


def ical_unescape(s):
    return (s.replace("\\n", "\n").replace("\\N", "\n")
             .replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\"))


def ical_dt(params, value):
    """→ (date ISO, heure affichable 'HHh[MM]' ou None, heure 'HH:MM' ou None, is_date_only).
    Gère TZID=<zone> et le suffixe UTC 'Z' (conversion en Europe/Paris)."""
    value = value.strip()
    m = re.match(r"^(\d{4})(\d{2})(\d{2})(?:T(\d{2})(\d{2})(\d{2})?)?(Z?)", value)
    if not m:
        return None, None, None, True
    date = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    # VALUE=DATE strict : ne pas matcher VALUE=DATE-TIME
    if m.group(4) is None or re.search(r"VALUE=DATE(?!-TIME)", params.upper()):
        return date, None, None, True
    hh, mm = int(m.group(4)), int(m.group(5))
    tzm = re.search(r"TZID=([^;:]+)", params, re.I)
    tzid = "UTC" if m.group(7) == "Z" else (tzm.group(1).strip() if tzm else None)
    if tzid and tzid not in ("Europe/Paris",):
        try:
            dt = datetime.datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)),
                                   hh, mm, tzinfo=ZoneInfo(tzid)).astimezone(ZoneInfo("Europe/Paris"))
            date, hh, mm = dt.date().isoformat(), dt.hour, dt.minute
        except (KeyError, ValueError):
            pass  # zone inconnue → heure laissée telle quelle
    heure = "minuit" if hh == 0 and mm == 0 else f"{hh}h" + (f"{mm:02d}" if mm else "")
    return date, heure, f"{hh:02d}:{mm:02d}", False


def rrule_expand(du, au, rule, exdates=frozenset()):
    """Occurrences (du, au) d'une RRULE simple : FREQ DAILY/WEEKLY/MONTHLY/YEARLY,
    INTERVAL, COUNT, UNTIL, BYDAY (hebdo), moins les EXDATE.
    Bornées à RRULE_MAX_OCC occurrences / RRULE_HORIZON jours."""
    parts = dict(p.split("=", 1) for p in rule.split(";") if "=" in p)
    freq = parts.get("FREQ", "").upper()
    if freq not in ("DAILY", "WEEKLY", "MONTHLY", "YEARLY"):
        return [(du, au)]
    try:
        interval = max(1, int(parts.get("INTERVAL", 1)))
    except ValueError:
        interval = 1
    count = None
    if parts.get("COUNT", "").isdigit():
        count = int(parts["COUNT"])
    until = None
    mu = re.match(r"(\d{4})(\d{2})(\d{2})", parts.get("UNTIL", ""))
    if mu:
        until = f"{mu.group(1)}-{mu.group(2)}-{mu.group(3)}"
    d1 = datetime.date.fromisoformat(du)
    dur = (datetime.date.fromisoformat(au) - d1).days
    limit = d1 + datetime.timedelta(days=RRULE_HORIZON)
    wd = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}
    bydays = sorted({wd[b[-2:]] for b in parts.get("BYDAY", "").split(",") if b[-2:] in wd})

    def candidates():
        if freq == "WEEKLY" and bydays:
            week0 = d1 - datetime.timedelta(days=d1.weekday())
            w = 0
            while True:
                base = week0 + datetime.timedelta(weeks=w * interval)
                for j in bydays:
                    d = base + datetime.timedelta(days=j)
                    if d >= d1:
                        yield d
                w += 1
        elif freq == "DAILY":
            i = 0
            while True:
                yield d1 + datetime.timedelta(days=i * interval)
                i += 1
        elif freq == "WEEKLY":
            i = 0
            while True:
                yield d1 + datetime.timedelta(weeks=i * interval)
                i += 1
        elif freq == "MONTHLY":
            i = 0
            while True:
                mo = d1.month - 1 + i * interval
                i += 1
                try:
                    yield datetime.date(d1.year + mo // 12, mo % 12 + 1, d1.day)
                except ValueError:
                    continue  # 31 dans un mois court : occurrence sautée
        else:  # YEARLY
            i = 0
            while True:
                try:
                    yield d1.replace(year=d1.year + i * interval)
                except ValueError:
                    pass  # 29 février
                i += 1

    out, n = [], 0
    for d in candidates():
        iso = d.isoformat()
        if (until and iso > until) or d > limit:
            break
        n += 1
        if iso not in exdates:
            out.append((iso, (d + datetime.timedelta(days=dur)).isoformat()))
        if (count and n >= count) or n >= RRULE_MAX_OCC:
            break
    return out or [(du, au)]


def parse_ical(text, commune):
    events, cur = [], None
    for line in ical_unfold(text):
        if line == "BEGIN:VEVENT":
            cur = {"_EXDATES": []}
        elif line == "END:VEVENT":
            if cur is not None and cur.get("SUMMARY") and cur.get("DTSTART"):
                events.append(cur)
            cur = None
        elif cur is not None and ":" in line:
            key, val = line.split(":", 1)
            name, _, params = key.partition(";")
            name = name.upper()
            if name == "EXDATE":  # peut apparaître plusieurs fois → accumulé
                cur["_EXDATES"].append((params, val))
            else:
                cur[name] = (params, val)

    out = []
    for ev in events:
        params, val = ev["DTSTART"]
        du, h1, t1, date_only = ical_dt(params, val)
        if not du:
            continue
        au, h2, t2 = du, None, None
        if "DTEND" in ev:
            p2, v2 = ev["DTEND"]
            au, h2, t2, end_date_only = ical_dt(p2, v2)
            if au and end_date_only and date_only:
                # DTEND en VALUE=DATE est EXCLUSIF (RFC 5545) → dernier jour = veille
                d = datetime.date.fromisoformat(au) - datetime.timedelta(days=1)
                au = d.isoformat()
        if not au or au < du:
            au = du
        # nocturne : fin au petit matin du lendemain (bal, concert…) → même journée,
        # sinon la vue Agenda affiche l'événement aussi le jour suivant
        if (t1 and t2 and t2 < t1
                and au == (datetime.date.fromisoformat(du) + datetime.timedelta(days=1)).isoformat()):
            au = du
        label = strip_tags(ical_unescape(ev["SUMMARY"][1]))
        desc = strip_tags(ical_unescape(ev.get("DESCRIPTION", ("", ""))[1]))
        lieu_full = strip_tags(ical_unescape(ev.get("LOCATION", ("", ""))[1]))
        lieu = lieu_full.split(",")[0].strip()  # « Salle X, 4 rue…, Ville, 41400 » → « Salle X »
        url = ev.get("URL", ("", ""))[1].strip() or None
        heures = h1 if h1 else None
        if h1 and h2 and h2 != h1:
            heures = f"{h1}–{h2}"  # y compris au != du : horaires quotidiens / nocturne
        # le lieu précis part dans 'h' (affiché en ligne pratique) s'il n'est pas
        # juste la commune répétée
        h_parts = [heures]
        if lieu and slugify(lieu) != slugify(commune):
            h_parts.append(lieu[:80])
        h = " · ".join(x for x in h_parts if x) or None
        # RRULE → occurrences multiples (regroupées en 'periodes' par build_data)
        occurrences = [(du, au)]
        if "RRULE" in ev:
            exdates = {ical_dt(p, v)[0] for p, v in ev["_EXDATES"]} - {None}
            occurrences = rrule_expand(du, au, ev["RRULE"][1], exdates)
        for odu, oau in occurrences:
            out.append({k: v for k, v in {
                "label": label,
                "ville": commune,
                "du": odu, "au": oau,
                "desc": desc or None,
                "url": url,
                "h": h,
                "lieu": lieu_full or None,  # commune déléguée résolue par build_data
            }.items() if v})
    return out


# ---------------------------------------------------------------- RSS

def parse_rss(text, nom):
    """Flux d'actualités → items à extraire (pas de date d'événement inventée)."""
    root = ElementTree.fromstring(text.encode("utf-8"))
    items = []
    # RSS 2.0 : channel/item ; Atom : entry
    nodes = root.findall(".//item") or root.findall(".//{http://www.w3.org/2005/Atom}entry")
    for it in nodes:
        def get(tag):
            el = it.find(tag) if "{" not in it.tag else it.find(f"{{http://www.w3.org/2005/Atom}}{tag}")
            return (el.text or "").strip() if el is not None and el.text else ""
        titre = get("title")
        if not titre:
            continue
        lien = get("link")
        if not lien and "{" in it.tag:  # Atom : link est un attribut
            el = it.find("{http://www.w3.org/2005/Atom}link")
            lien = el.get("href", "") if el is not None else ""
        date_pub = None
        for tag in ("pubDate", "{http://purl.org/dc/elements/1.1/}date", "updated"):
            v = get(tag) if "{" not in tag else ((it.find(tag).text or "").strip()
                                                 if it.find(tag) is not None else "")
            if v:
                try:
                    date_pub = parsedate_to_datetime(v).date().isoformat()
                except (ValueError, TypeError):
                    date_pub = v[:10]
                break
        resume = strip_tags(get("description")
                            or get("{http://purl.org/rss/1.0/modules/content/}encoded"))
        items.append({k: v for k, v in {
            "source": nom,
            "titre": titre,
            "lien": lien or None,
            "date_pub": date_pub,
            "resume": resume[:500] or None,
            "a_extraire": True,
        }.items() if v})
    return items


# ---------------------------------------------------------------- main

def main():
    try:
        import yaml
    except ImportError:
        sys.exit("PyYAML requis (python3.13 -m pip install pyyaml)")
    with open(os.path.join(SOURCES, "mairies.yaml")) as f:
        sources = yaml.safe_load(f) or []
    os.makedirs(CACHE, exist_ok=True)

    # Run précédent : réutilisé source par source en cas de panne passagère, pour ne
    # jamais vider silencieusement la collecte (les événements passés sont filtrés
    # de toute façon par build_data).
    out_path = os.path.join(SOURCES, "collected.json")
    prev = {}
    if os.path.exists(out_path):
        try:
            with open(out_path) as f:
                prev = json.load(f)
        except (json.JSONDecodeError, OSError):
            prev = {}
    prev_events, prev_items = {}, {}
    for e in prev.get("events") or []:
        prev_events.setdefault(e.get("source"), []).append(e)
    for it in prev.get("a_extraire") or []:
        prev_items.setdefault(it.get("source"), []).append(it)

    now = datetime.datetime.now().isoformat(timespec="seconds")
    fraicheur = (datetime.date.today() - datetime.timedelta(days=RSS_MAX_AGE)).isoformat()
    events, a_extraire, snapshots, errors = [], [], [], []
    first = True
    for src in sources:
        nom, mode = src.get("nom", "?"), src.get("mode", "html")
        commune = src.get("commune", "")
        if not first:
            time.sleep(DELAY)
        first = False
        try:
            if src.get("ical"):
                text = fetch(src["ical"])
                evs = parse_ical(text, commune)
                for e in evs:
                    e["source"] = f"mairie:{commune}"
                events.extend(evs)
                log(f"[ical] {nom}: {len(evs)} événements")
            elif mode in ("rss", "rss-js"):
                feed = src.get("rss") or src.get("url")
                if not feed:
                    raise KeyError("ni 'rss' ni 'url' dans mairies.yaml")
                text = fetch(feed)
                items = parse_rss(text, nom)
                frais = [it for it in items if (it.get("date_pub") or "9999") >= fraicheur]
                a_extraire.extend(frais)
                vieux = len(items) - len(frais)
                log(f"[rss ] {nom}: {len(frais)} items à extraire"
                    + (f" ({vieux} de plus de {RSS_MAX_AGE} j ignorés)" if vieux else ""))
            else:  # html → snapshot texte pour extraction hebdo par Claude
                url = src.get("url")
                if not url:
                    raise KeyError("clé 'url' absente dans mairies.yaml")
                page = fetch(url)
                full = html_to_text(page)
                text = full[:SNAPSHOT_MAX]
                tronque = len(full) > SNAPSHOT_MAX
                path = os.path.join(CACHE, f"{slugify(nom)}.txt")
                with open(path, "w") as f:
                    f.write(f"# source: {nom}\n# commune: {commune}\n"
                            f"# url: {url}\n# fetch: {now}\n"
                            + (f"# tronque: oui ({len(full)} car. → {SNAPSHOT_MAX})\n" if tronque else "")
                            + f"---\n{text}\n")
                snapshots.append(os.path.relpath(path, HERE))
                if tronque:
                    log(f"[warn] {nom}: snapshot TRONQUÉ à {SNAPSHOT_MAX} car. (page {len(full)})")
                log(f"[html] {nom}: snapshot {len(text)} car. → {os.path.basename(path)}")
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError,
                ElementTree.ParseError, KeyError, ValueError) as e:
            err = f"{e.__class__.__name__}: {e}"
            log(f"[ERR ] {nom}: {err}")
            entry = {"source": nom, "mode": "ical" if src.get("ical") else mode, "erreur": err}
            key = f"mairie:{commune}" if src.get("ical") else nom
            if src.get("ical") and prev_events.get(key):
                events.extend(prev_events[key])
                entry["reprise_cache"] = (f"{len(prev_events[key])} événements repris "
                                          f"du run {prev.get('fetched', '?')}")
                log(f"[ERR ] {nom}: {entry['reprise_cache']}")
            elif mode in ("rss", "rss-js") and prev_items.get(nom):
                a_extraire.extend(prev_items[nom])
                entry["reprise_cache"] = (f"{len(prev_items[nom])} items repris "
                                          f"du run {prev.get('fetched', '?')}")
                log(f"[ERR ] {nom}: {entry['reprise_cache']}")
            elif mode == "html":
                path = os.path.join(CACHE, f"{slugify(nom)}.txt")
                if os.path.exists(path):
                    # snapshot périmé : conservé sur disque mais PAS listé comme frais
                    entry["snapshot_perime"] = os.path.relpath(path, HERE)
            errors.append(entry)

    with open(out_path, "w") as f:
        json.dump({"fetched": now, "ok": not errors, "errors": errors,
                   "events": events, "a_extraire": a_extraire,
                   "snapshots": snapshots}, f, ensure_ascii=False, indent=1)
    log(f"{out_path}: {len(events)} événements, {len(a_extraire)} à extraire, "
        f"{len(snapshots)} snapshots, {len(errors)} erreurs")


if __name__ == "__main__":
    main()
