#!/usr/bin/env python3
"""Lit les issues envoyées par Modder Sims4 : listes de mods (fusionnées dans le catalogue) et
rapports de bug (résumés en commentaire).

Lancé par .github/workflows/reception.yml. Variables lues : GITHUB_EVENT_PATH, GITHUB_TOKEN,
GITHUB_REPOSITORY, et CATALOGUE (dossier du catalogue, « catalogue » par défaut).
"""

import base64
import binascii
import copy
import json
import os
import re
import sys
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import zlib
from datetime import datetime, timedelta, timezone

DOSSIER_SCRIPTS = os.path.dirname(os.path.abspath(__file__))
if DOSSIER_SCRIPTS not in sys.path:
    sys.path.insert(0, DOSSIER_SCRIPTS)

import page  # noqa: E402

TAILLE_CORPS_MAX = 65536
TAILLE_JSON_MAX = 2 * 1024 * 1024
MODS_MAX = 5000
NOM_MAX = 200
GENRE_MAX = 60
URL_MAX = 500
LIENS_MAX = 20
LISTES_PAR_JOUR = 3
RATTRAPAGE_MAX = 10

MARQUEUR = re.compile(r"<!--\s*modder-([a-z]+)\s+(\d+)\s*-->")
EMPREINTE = re.compile(r"^[0-9a-f]{64}$")
BLOC_JSON = re.compile(r"```json[ \t]*\r?\n(.*?)\r?\n[ \t]*```", re.S)
BLOC_COMPRESSE = re.compile(r"Liste compress[^\n]*\n\s*```[^\n]*\n(.*?)```", re.S)
SUSPECT = re.compile(r"^\s*\d+[.)]\s+\*\*(.+?)\*\*\s*\(score\s*(\d+)\)\s*:?\s*(.*)$", re.I)
ERREUR = re.compile(r"\b[\w.]*(?:Error|Exception)\b\s*:")
CLOTURE = re.compile(r"^\s*(`{3,}|~{3,})")

# Paramètres de pistage retirés des adresses.
PISTAGE = re.compile(
    r"^(utm_\w*|fbclid|gclid|gbraid|wbraid|dclid|msclkid|yclid|mc_cid|mc_eid|igshid|_ga|_gl|ref|ref_src|si)$",
    re.I,
)

# Mots qui signalent une traduction, un add-on ou un correctif d'un autre mod.
MOTS_LIES = {
    "traduction", "translation", "translated", "fr", "french", "francais", "francaise", "vf",
    "addon", "addons", "add", "patch", "compat", "compatibility", "compatibilite",
}

# Sites où le premier dossier de l'adresse est la page du créateur.
PAGES_PROFIL = {
    "patreon.com", "ko-fi.com", "x.com", "twitter.com", "github.com", "buymeacoffee.com",
    "deviantart.com", "instagram.com", "youtube.com",
}
SOUS_DOMAINES_PROFIL = ("tumblr.com", "itch.io", "blogspot.com", "wordpress.com")
CHEMINS_GENERIQUES = {
    "posts", "post", "c", "cw", "p", "home", "search", "download", "downloads", "file", "files",
    "mods", "sims4", "status", "watch", "shop", "explore", "join", "checkout",
}


class FormatInconnu(Exception):
    """Le corps de l'issue ne contient pas de liste lisible."""


# ---------- Petits outils ----------

def maintenant_utc():
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(date):
    return date.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def lire_date(texte):
    if not isinstance(texte, str):
        return None
    try:
        return datetime.fromisoformat(texte.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def noms_etiquettes(issue):
    noms = set()
    for e in issue.get("labels") or []:
        nom = e.get("name") if isinstance(e, dict) else e
        if isinstance(nom, str):
            noms.add(nom.lower())
    return noms


def nettoyer(texte):
    """Retire les caractères de contrôle et d'inversion de sens, et resserre les espaces."""
    garde = []
    for c in texte:
        if unicodedata.category(c) in ("Cc", "Cs") or "\u202a" <= c <= "\u202e" or "\u2066" <= c <= "\u2069":
            garde.append(" ")
        else:
            garde.append(c)
    return " ".join("".join(garde).split())


def mots(texte):
    t = unicodedata.normalize("NFKD", (texte or "").lower())
    t = "".join(c for c in t if not unicodedata.combining(c))
    return re.findall(r"[a-z0-9]+", t)


def pluriel(n, un, plusieurs):
    return f"{n} {un if n <= 1 else plusieurs}"


def code_en_ligne(texte, maxi=200):
    t = nettoyer(str(texte)).replace("`", "'")[:maxi]
    return f"`{t}`" if t else ""


# ---------- Adresses et refus ----------

def normaliser_url(url):
    """Adresse http(s) normalisée (hôte en minuscules, sans www., sans pistage ni ancre), ou None."""
    if not isinstance(url, str):
        return None
    url = url.strip()
    if not url or len(url) > URL_MAX or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in url):
        return None
    try:
        parts = urllib.parse.urlsplit(url)
        port = parts.port
        hote = (parts.hostname or "").rstrip(".")
        hote = hote.encode("idna").decode("ascii").lower() if hote else ""
    except (ValueError, UnicodeError):
        return None
    schema = parts.scheme.lower()
    if schema not in ("http", "https"):
        return None
    if not re.fullmatch(r"[a-z0-9-]+(\.[a-z0-9-]+)*", hote):
        return None
    if hote.startswith("www."):
        hote = hote[4:]
    if port and not (schema == "http" and port == 80) and not (schema == "https" and port == 443):
        hote = f"{hote}:{port}"
    chemin = urllib.parse.quote(parts.path, safe="/%:@!$&'()*+,;=-._~").rstrip("/")
    params = [(k, v) for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True) if not PISTAGE.match(k)]
    requete = urllib.parse.urlencode(sorted(params))
    return urllib.parse.urlunsplit((schema, hote, chemin, requete, ""))


def lire_refus(texte):
    """Lit les règles de refus.txt : (motifs compilés, domaines)."""
    motifs, domaines = [], []
    for ligne in texte.splitlines():
        ligne = ligne.strip()
        if not ligne or ligne.startswith("#"):
            continue
        sorte, _, valeur = ligne.partition(":")
        valeur = valeur.strip()
        if not valeur:
            continue
        if sorte.strip().lower() == "motif":
            motifs.append(re.compile(valeur, re.I))
        elif sorte.strip().lower() == "domaine":
            domaines.append(valeur.lower().removeprefix("www."))
    return motifs, domaines


def charger_refus(chemin=None):
    chemin = chemin or os.path.join(DOSSIER_SCRIPTS, "refus.txt")
    try:
        with open(chemin, encoding="utf-8") as f:
            return lire_refus(f.read())
    except FileNotFoundError:
        return [], []


def est_refuse(texte, refus, url=False):
    motifs, domaines = refus
    if url:
        hote = (urllib.parse.urlsplit(texte).hostname or "").lower()
        if any(hote == d or hote.endswith("." + d) for d in domaines):
            return True
    return any(m.search(texte) for m in motifs)


# ---------- Lecture d'une liste ----------

def decompresser(texte):
    """Base64 puis deflate brut, avec une limite de taille à la sortie."""
    brut = "".join(texte.split()).replace("-", "+").replace("_", "/")
    brut += "=" * (-len(brut) % 4)
    try:
        donnees = base64.b64decode(brut, validate=True)
    except (binascii.Error, ValueError) as e:
        raise FormatInconnu("base64 illisible") from e
    d = zlib.decompressobj(-15)
    try:
        sortie = d.decompress(donnees, TAILLE_JSON_MAX + 1)
    except zlib.error as e:
        raise FormatInconnu("deflate illisible") from e
    if len(sortie) > TAILLE_JSON_MAX or d.unconsumed_tail:
        raise FormatInconnu("liste trop grosse")
    if not d.eof:
        raise FormatInconnu("liste coupée")
    try:
        return sortie.decode("utf-8")
    except UnicodeDecodeError as e:
        raise FormatInconnu("texte illisible") from e


def extraire_liste(corps):
    """Trouve le JSON de la liste dans le corps de l'issue, en clair ou compressé."""
    if len(corps) > TAILLE_CORPS_MAX:
        raise FormatInconnu("corps trop long")
    m = BLOC_JSON.search(corps)
    if m:
        texte = m.group(1)
        if len(texte.encode("utf-8")) > TAILLE_JSON_MAX:
            raise FormatInconnu("liste trop grosse")
    else:
        m = BLOC_COMPRESSE.search(corps)
        if not m:
            raise FormatInconnu("aucune liste")
        texte = decompresser(m.group(1))
    try:
        return json.loads(texte)
    except ValueError as e:
        raise FormatInconnu("JSON illisible") from e


def valider_mod(brut, refus):
    """Rend (mod, None) si l'entrée est bonne, sinon (None, "invalide" ou "refuse")."""
    if not isinstance(brut, dict):
        return None, "invalide"
    nom = brut.get("nom")
    if not isinstance(nom, str):
        return None, "invalide"
    nom = nettoyer(nom)
    if not nom or len(nom) > NOM_MAX:
        return None, "invalide"

    createur = brut.get("createur")
    if createur is not None:
        if not isinstance(createur, str):
            return None, "invalide"
        createur = nettoyer(createur) or None
        if createur and len(createur) > NOM_MAX:
            return None, "invalide"

    empreinte = brut.get("empreinte")
    if not isinstance(empreinte, str) or not EMPREINTE.match(empreinte.strip().lower()):
        return None, "invalide"
    empreinte = empreinte.strip().lower()

    genre = brut.get("genre")
    if genre is not None and not isinstance(genre, str):
        return None, "invalide"
    genre = nettoyer(genre or "")[:GENRE_MAX]

    source = brut.get("source")
    if source is not None and not isinstance(source, str):
        return None, "invalide"
    source = normaliser_url(source)

    liens_bruts = brut.get("liens")
    if liens_bruts is None:
        liens_bruts = []
    if not isinstance(liens_bruts, list):
        return None, "invalide"
    liens = []
    for lien in liens_bruts:
        lien = normaliser_url(lien)
        if lien and lien not in liens and len(liens) < LIENS_MAX:
            liens.append(lien)

    if est_refuse(nom, refus) or any(est_refuse(u, refus, url=True) for u in [source, *liens] if u):
        return None, "refuse"
    return {"empreinte": empreinte, "nom": nom, "createur": createur, "source": source, "genre": genre, "liens": liens}, None


def valider_liste(donnees, refus):
    """Vérifie le schéma de la liste. Rend (mods valides sans doublon, entrées invalides, entrées refusées)."""
    if not isinstance(donnees, dict):
        raise FormatInconnu("la liste n'est pas un objet")
    if type(donnees.get("format")) is not int or donnees["format"] != 1:
        raise FormatInconnu("format de liste inconnu")
    for champ in ("appli", "version"):
        valeur = donnees.get(champ)
        if valeur is not None and (not isinstance(valeur, str) or len(valeur) > 100):
            raise FormatInconnu(f"champ {champ} invalide")
    mods = donnees.get("mods")
    if not isinstance(mods, list):
        raise FormatInconnu("pas de tableau mods")
    if len(mods) > MODS_MAX:
        raise FormatInconnu("trop de mods")

    vus, valides, invalides, refuses = set(), [], 0, 0
    for brut in mods:
        mod, raison = valider_mod(brut, refus)
        if raison == "invalide":
            invalides += 1
        elif raison == "refuse":
            refuses += 1
        elif mod["empreinte"] not in vus:
            vus.add(mod["empreinte"])
            valides.append(mod)
    return valides, invalides, refuses


# ---------- Catalogue ----------

def catalogue_vide(date=None):
    return {"format": 1, "mis_a_jour": iso(date or maintenant_utc()), "mods": [], "createurs": []}


def charger_catalogue(chemin):
    try:
        with open(chemin, encoding="utf-8") as f:
            donnees = json.load(f)
    except FileNotFoundError:
        return catalogue_vide()
    mods = [m for m in donnees.get("mods") or [] if isinstance(m, dict) and isinstance(m.get("empreinte"), str)]
    return {"format": 1, "mis_a_jour": donnees.get("mis_a_jour"), "mods": mods, "createurs": donnees.get("createurs") or []}


def texte_catalogue(catalogue):
    return json.dumps(catalogue, ensure_ascii=False, indent=1, sort_keys=True) + "\n"


def ecrire_fichier(chemin, texte):
    provisoire = chemin + ".tmp"
    with open(provisoire, "w", encoding="utf-8", newline="\n") as f:
        f.write(texte)
    os.replace(provisoire, chemin)


def relier(mods):
    """Remplit relie_a, qui rattache une traduction, un add-on ou un correctif à son mod principal.

    Un mod B est rattaché au mod A si :
    - A et B ont la même source, ou le même créateur ;
    - les mots du nom de A se suivent dans le nom de B, et B a plus de mots que A ;
    - il reste dans le nom de B un mot comme traduction, translation, fr, french, addon,
      add-on, patch ou compat (voir MOTS_LIES).
    Si plusieurs A conviennent, on garde celui qui a le nom le plus long. Une chaîne
    (traduction d'un add-on) est ramenée au tout premier mod principal.
    """
    proches = {}
    for m in mods:
        for cle in (("s", m.get("source")), ("c", (m.get("createur") or "").casefold())):
            if cle[1]:
                proches.setdefault(cle, []).append(m)
    noms = {m["empreinte"]: mots(m["nom"]) for m in mods}
    parent = {}
    for b in mods:
        mb = noms[b["empreinte"]]
        if not MOTS_LIES.intersection(mb):
            continue
        candidats = {}
        for cle in (("s", b.get("source")), ("c", (b.get("createur") or "").casefold())):
            for a in proches.get(cle, []) if cle[1] else []:
                candidats[a["empreinte"]] = a
        meilleur = None
        for a in candidats.values():
            ma = noms[a["empreinte"]]
            if a is b or not ma or len(ma) >= len(mb):
                continue
            for i in range(len(mb) - len(ma) + 1):
                if mb[i:i + len(ma)] == ma:
                    reste = mb[:i] + mb[i + len(ma):]
                    if MOTS_LIES.intersection(reste) and (meilleur is None or len(ma) > len(noms[meilleur["empreinte"]])):
                        meilleur = a
                    break
        if meilleur is not None:
            parent[b["empreinte"]] = meilleur["empreinte"]
    for m in mods:
        racine = parent.get(m["empreinte"])
        # Les noms raccourcissent à chaque étape : la boucle s'arrête forcément.
        while racine in parent:
            racine = parent[racine]
        m["relie_a"] = racine


def page_createur(url):
    """Page de créateur devinée à partir d'une adresse (Patreon, Tumblr, CurseForge…), ou None."""
    if not url:
        return None
    parts = urllib.parse.urlsplit(url)
    hote = parts.hostname or ""
    dossiers = [d for d in parts.path.split("/") if d]
    if hote == "curseforge.com" and len(dossiers) >= 2 and dossiers[0] == "members":
        return f"https://curseforge.com/members/{dossiers[1]}"
    if any(hote.endswith("." + d) for d in SOUS_DOMAINES_PROFIL):
        return f"https://{hote}"
    if hote in PAGES_PROFIL and dossiers and dossiers[0].lower() not in CHEMINS_GENERIQUES:
        if hote == "youtube.com" and not dossiers[0].startswith("@"):
            return None
        return f"https://{hote}/{dossiers[0]}"
    return None


def lister_createurs(mods):
    groupes = {}
    for m in mods:
        if not m.get("createur"):
            continue
        g = groupes.setdefault(m["createur"].casefold(), {"nom": m["createur"], "pages": set(), "mods": 0})
        g["mods"] += 1
        for url in [m.get("source"), *(m.get("liens") or [])]:
            p = page_createur(url)
            if p:
                g["pages"].add(p)
    sortie = [{"nom": g["nom"], "pages": sorted(g["pages"])[:10], "mods": g["mods"]} for g in groupes.values()]
    return sorted(sortie, key=lambda c: (c["nom"].casefold(), c["nom"]))


def finaliser(catalogue):
    """Recalcule les rattachements et les créateurs, puis trie tout pour des diffs propres."""
    mods = catalogue["mods"]
    for m in mods:
        m.setdefault("createur", None)
        m.setdefault("source", None)
        m.setdefault("genre", "")
        m["liens"] = list(m.get("liens") or [])
        m["vu"] = int(m.get("vu") or 1)
    mods.sort(key=lambda m: ((m.get("createur") or "").casefold(), m["nom"].casefold(), m["empreinte"]))
    relier(mods)
    catalogue["createurs"] = lister_createurs(mods)


def fusionner(catalogue, mods):
    """Ajoute les mods reçus. Un mod déjà connu (même empreinte) voit son compteur vu monter et
    ses champs vides complétés. Rend le nombre de nouveaux mods."""
    index = {m["empreinte"]: m for m in catalogue["mods"]}
    nouveaux = 0
    for mod in mods:
        connu = index.get(mod["empreinte"])
        if connu is None:
            entree = dict(mod, vu=1, relie_a=None)
            catalogue["mods"].append(entree)
            index[mod["empreinte"]] = entree
            nouveaux += 1
            continue
        connu["vu"] = int(connu.get("vu") or 0) + 1
        for champ in ("source", "createur", "genre"):
            if not connu.get(champ) and mod.get(champ):
                connu[champ] = mod[champ]
        liens = list(connu.get("liens") or [])
        for lien in mod["liens"]:
            if lien not in liens and len(liens) < LIENS_MAX:
                liens.append(lien)
        connu["liens"] = liens
    finaliser(catalogue)
    return nouveaux


# ---------- Limite par auteur ----------

def depasse_limite(issues, numero, reference, limite=LISTES_PAR_JOUR):
    """Vrai si l'auteur a déjà envoyé `limite` listes dans les 24 h avant `reference`.
    `issues` vient de l'API (issues de cet auteur avec l'étiquette liste)."""
    debut = reference - timedelta(hours=24)
    compte = 0
    for i in issues:
        if i.get("pull_request") or i.get("number") == numero:
            continue
        if numero and isinstance(i.get("number"), int) and i["number"] > numero:
            continue
        if "liste" not in noms_etiquettes(i):
            continue
        cree = lire_date(i.get("created_at"))
        if cree is None or cree < debut or cree > reference:
            continue
        compte += 1
    return compte >= limite


# ---------- Rapports de bug ----------

def sections(corps):
    """Découpe le Markdown par titres (###), sans tenir compte des titres dans les blocs de code."""
    trouvees, courante, cloture = {}, None, None
    for ligne in corps.splitlines():
        m = CLOTURE.match(ligne)
        if cloture:
            if m and m.group(1)[0] == cloture[0] and len(m.group(1)) >= len(cloture) and not ligne.strip()[len(m.group(1)):].strip():
                cloture = None
        elif m:
            cloture = m.group(1)
        else:
            titre = re.match(r"^\s*#{2,4}\s+(.*?)\s*#*\s*$", ligne)
            if titre:
                courante = " ".join(mots(titre.group(1)))
                trouvees.setdefault(courante, [])
                continue
        if courante is not None:
            trouvees[courante].append(ligne)
    return trouvees


def resumer_bug(corps):
    """Rend (mods suspects [(nom, score, indice)], première ligne d'erreur des journaux ou None)."""
    parties = sections(corps)
    suspects, journaux = [], None
    for titre, lignes in parties.items():
        if titre.startswith("mods suspects"):
            for ligne in lignes:
                m = SUSPECT.match(ligne)
                if m:
                    suspects.append((nettoyer(m.group(1)), int(m.group(2)), nettoyer(m.group(3))))
        elif titre.startswith("journaux") and journaux is None:
            journaux = lignes
    if journaux is None:
        journaux = corps.splitlines()
    erreur = None
    for ligne in journaux:
        ligne = re.sub(r"<[^>]+>", "", ligne).strip()
        if ERREUR.search(ligne):
            erreur = nettoyer(ligne)[:300]
            break
    return suspects, erreur


def message_bug(suspects, erreur):
    lignes = ["Merci pour le signalement ! Ce qui ressort du rapport :", ""]
    if suspects:
        lignes += ["**Mods suspects**", ""]
        for i, (nom, score, indice) in enumerate(suspects[:5], 1):
            ligne = f"{i}. {code_en_ligne(nom)} (score {score})"
            if indice:
                ligne += f" : {code_en_ligne(indice)}"
            lignes.append(ligne)
        lignes.append("")
    if erreur:
        longueur = max((len(r) for r in re.findall(r"`+", erreur)), default=0)
        cloture = "`" * max(3, longueur + 1)
        lignes += ["**Première erreur dans les journaux**", "", f"{cloture}text", erreur, cloture, ""]
    if not suspects and not erreur:
        lignes += ["Je n'ai trouvé ni mod suspect ni erreur dans les journaux.", ""]
    lignes.append("L'issue reste ouverte, je regarde ça dès que possible.")
    return "\n".join(lignes)


# ---------- Messages des listes ----------

def message_merci(recus, nouveaux, ignores, adresse=None):
    texte = f"Merci pour ta liste ! J'ai bien reçu {pluriel(recus, 'mod', 'mods')}"
    if recus == 0:
        texte = "Merci pour ta liste ! Aucun mod n'a pu être ajouté au catalogue."
    elif nouveaux == 0:
        texte += ", ils étaient déjà tous dans le catalogue."
    else:
        texte += f", dont {pluriel(nouveaux, 'nouveau', 'nouveaux')} pour le catalogue."
    if ignores:
        if ignores == 1:
            texte += " Une entrée n'a pas été retenue."
        else:
            texte += f" {ignores} entrées n'ont pas été retenues."
    if adresse:
        texte += f"\n\nLe catalogue : {adresse}"
    return texte


MESSAGE_LIMITE = (
    "Merci pour ta liste ! Tu en as déjà envoyé 3 ces dernières 24 heures, alors celle-ci n'est pas "
    "ajoutée au catalogue. Tu pourras en envoyer une nouvelle demain."
)


# ---------- Traitement ----------

def type_issue(issue):
    """liste, bug ou inconnu : d'abord par le marqueur, sinon par l'étiquette ou le titre."""
    corps = issue.get("body") or ""
    m = MARQUEUR.search(corps)
    if m:
        if m.group(2) == "1" and m.group(1) in ("liste", "bug"):
            return m.group(1)
        return "inconnu"
    etiquettes = noms_etiquettes(issue)
    titre = (issue.get("title") or "").strip().lower()
    if "liste" in etiquettes or titre.startswith("liste de mods"):
        return "liste"
    if "bug" in etiquettes or re.match(r"bug\s*:", titre):
        return "bug"
    return "inconnu"


def traiter_liste(issue, api, catalogue, refus, maintenant):
    numero = issue["number"]
    login = (issue.get("user") or {}).get("login") or ""
    reference = lire_date(issue.get("created_at")) or maintenant
    if login and depasse_limite(api.issues_auteur(login, reference - timedelta(hours=24)), numero, reference):
        api.ajouter_etiquettes(numero, ["liste", "limite"])
        api.commenter(numero, MESSAGE_LIMITE)
        api.fermer(numero, "not_planned")
        print(f"#{numero} : limite atteinte pour {login}")
        return False
    try:
        mods, invalides, refuses = valider_liste(extraire_liste(issue.get("body") or ""), refus)
        if not mods and invalides and not refuses:
            raise FormatInconnu("aucune entrée valide")
    except FormatInconnu as e:
        api.ajouter_etiquettes(numero, ["liste", "format-inconnu"])
        print(f"#{numero} : liste illisible ({e})")
        return False
    nouveaux = fusionner(catalogue, mods) if mods else 0
    api.ajouter_etiquettes(numero, ["liste"])
    api.commenter(numero, message_merci(len(mods), nouveaux, invalides + refuses, getattr(api, "page", None)))
    api.fermer(numero, "completed")
    print(f"#{numero} : {len(mods)} mods, {nouveaux} nouveaux, {invalides} invalides, {refuses} refusés")
    return bool(mods)


def traiter_bug(issue, api):
    numero = issue["number"]
    suspects, erreur = resumer_bug(issue.get("body") or "")
    api.ajouter_etiquettes(numero, ["bug"])
    api.commenter(numero, message_bug(suspects, erreur))
    print(f"#{numero} : bug, {len(suspects)} suspects")


def traiter_issue(issue, api, catalogue, refus, maintenant):
    """Traite une issue. Rend True si le catalogue a changé."""
    numero = issue["number"]
    if len(issue.get("body") or "") > TAILLE_CORPS_MAX:
        api.ajouter_etiquettes(numero, ["format-inconnu"])
        print(f"#{numero} : corps trop long")
        return False
    sorte = type_issue(issue)
    if sorte == "liste":
        return traiter_liste(issue, api, catalogue, refus, maintenant)
    if sorte == "bug":
        traiter_bug(issue, api)
        return False
    api.ajouter_etiquettes(numero, ["format-inconnu"])
    print(f"#{numero} : format inconnu")
    return False


def a_rattraper(issue, deja):
    """Listes restées ouvertes, par exemple quand GitHub a annulé une exécution en attente."""
    if issue.get("pull_request") or issue.get("number") in deja:
        return False
    if (issue.get("user") or {}).get("type") == "Bot":
        return False
    return not noms_etiquettes(issue) & {"limite", "format-inconnu"}


def traiter_evenement(evenement, api, dossier, maintenant=None, refus=None):
    """Traite l'issue de l'événement, puis les listes restées ouvertes. Écrit le catalogue et la
    page s'ils ont changé. Rend le message de commit, ou None si rien n'a changé."""
    maintenant = maintenant or maintenant_utc()
    refus = refus if refus is not None else charger_refus()
    issue = evenement.get("issue") or {}
    if not isinstance(issue.get("number"), int) or (issue.get("user") or {}).get("type") == "Bot":
        return None

    chemin = os.path.join(dossier, "catalogue.json")
    catalogue = charger_catalogue(chemin)
    fusionnees = []
    if traiter_issue(issue, api, catalogue, refus, maintenant):
        fusionnees.append(issue["number"])

    try:
        ouvertes = api.issues_ouvertes("liste")
    except Exception as e:  # le rattrapage ne doit pas faire échouer la liste du jour
        print(f"Rattrapage impossible : {e}", file=sys.stderr)
        ouvertes = []
    deja = {issue["number"]}
    for autre in [i for i in ouvertes if a_rattraper(i, deja)][:RATTRAPAGE_MAX]:
        sauvegarde = copy.deepcopy(catalogue)
        try:
            if traiter_issue(autre, api, catalogue, refus, maintenant):
                fusionnees.append(autre["number"])
        except Exception as e:
            catalogue = sauvegarde
            print(f"#{autre.get('number')} non traitée : {e}", file=sys.stderr)

    if not fusionnees:
        return None
    catalogue["mis_a_jour"] = iso(maintenant)
    os.makedirs(dossier, exist_ok=True)
    ecrire_fichier(chemin, texte_catalogue(catalogue))
    ecrire_fichier(os.path.join(dossier, "index.html"), page.generer(catalogue))
    numeros = ", ".join(f"#{n}" for n in sorted(fusionnees))
    return f"Catalogue : {'liste' if len(fusionnees) == 1 else 'listes'} {numeros}"


# ---------- API GitHub ----------

class Github:
    """Appels REST au dépôt, avec le jeton du runner."""

    def __init__(self, depot, jeton):
        self.depot = depot
        self.jeton = jeton
        proprio, _, nom = depot.partition("/")
        self.page = f"https://{proprio.lower()}.github.io/{nom}/"

    def appel(self, methode, chemin, donnees=None):
        corps = json.dumps(donnees).encode("utf-8") if donnees is not None else None
        requete = urllib.request.Request(
            f"https://api.github.com/repos/{self.depot}{chemin}",
            data=corps,
            method=methode,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.jeton}",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "catalogue-modder-sims4",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(requete, timeout=30) as reponse:
                texte = reponse.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"{methode} {chemin} : HTTP {e.code}") from e
        return json.loads(texte) if texte else None

    def ajouter_etiquettes(self, numero, etiquettes):
        self.appel("POST", f"/issues/{numero}/labels", {"labels": list(etiquettes)})

    def commenter(self, numero, texte):
        self.appel("POST", f"/issues/{numero}/comments", {"body": texte})

    def fermer(self, numero, raison="completed"):
        self.appel("PATCH", f"/issues/{numero}", {"state": "closed", "state_reason": raison})

    def issues_auteur(self, login, depuis):
        requete = urllib.parse.urlencode({
            "creator": login, "labels": "liste", "state": "all", "since": iso(depuis),
            "sort": "created", "direction": "desc", "per_page": 100,
        })
        return self.appel("GET", f"/issues?{requete}") or []

    def issues_ouvertes(self, etiquette):
        requete = urllib.parse.urlencode({
            "labels": etiquette, "state": "open", "sort": "created", "direction": "asc", "per_page": 30,
        })
        return self.appel("GET", f"/issues?{requete}") or []


def main():
    with open(os.environ["GITHUB_EVENT_PATH"], encoding="utf-8") as f:
        evenement = json.load(f)
    api = Github(os.environ["GITHUB_REPOSITORY"], os.environ["GITHUB_TOKEN"])
    message = traiter_evenement(evenement, api, os.environ.get("CATALOGUE", "catalogue"))
    sortie = os.environ.get("GITHUB_OUTPUT")
    if message and sortie:
        with open(sortie, "a", encoding="utf-8") as f:
            f.write(f"message={message}\n")


if __name__ == "__main__":
    main()
