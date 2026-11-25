"""Tests de reception.py et page.py, sans réseau : l'API GitHub est remplacée par une fausse classe."""

import base64
import json
import os
import tempfile
import unittest
import zlib
from datetime import datetime, timedelta, timezone

import page
import reception

MAINTENANT = datetime(2026, 11, 25, 12, 0, tzinfo=timezone.utc)
REFUS = reception.charger_refus()


def empreinte(n):
    return f"{n:064x}"


def mod(n, nom, createur=None, source=None, genre="Script", liens=None):
    return {"nom": nom, "createur": createur, "source": source, "empreinte": empreinte(n), "genre": genre, "liens": liens or []}


def liste(*mods):
    return {"format": 1, "appli": "Modder Sims4", "version": "0.3.0", "mods": list(mods)}


def corps_json(donnees):
    return "<!-- modder-liste 1 -->\n```json\n" + json.dumps(donnees, indent=2, ensure_ascii=False) + "\n```\n"


def corps_compresse(donnees):
    c = zlib.compressobj(9, zlib.DEFLATED, -15)
    brut = c.compress(json.dumps(donnees).encode("utf-8")) + c.flush()
    b64 = base64.b64encode(brut).decode("ascii")
    lignes = "\n".join(b64[i:i + 76] for i in range(0, len(b64), 76))
    return f"<!-- modder-liste 1 -->\nListe compressée (deflate puis base64) :\n\n```\n{lignes}\n```\n"


def issue(numero, corps, titre="Liste de mods", login="sim", type_="User", etiquettes=(), cree=MAINTENANT):
    return {
        "number": numero, "title": titre, "body": corps, "created_at": reception.iso(cree),
        "user": {"login": login, "type": type_}, "labels": [{"name": e} for e in etiquettes],
    }


class FausseApi:
    page = "https://exemple.invalid/catalogue/"

    def __init__(self, auteur=(), ouvertes=()):
        self.appels = []
        self.auteur = list(auteur)
        self.ouvertes = list(ouvertes)

    def ajouter_etiquettes(self, numero, etiquettes):
        self.appels.append(("etiquettes", numero, tuple(etiquettes)))

    def commenter(self, numero, texte):
        self.appels.append(("commentaire", numero, texte))

    def fermer(self, numero, raison="completed"):
        self.appels.append(("fermer", numero, raison))

    def issues_auteur(self, login, depuis):
        return self.auteur

    def issues_ouvertes(self, etiquette):
        return self.ouvertes

    def de(self, sorte, numero=None):
        return [a for a in self.appels if a[0] == sorte and (numero is None or a[1] == numero)]


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dossier = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def envoyer(self, iss, api=None):
        api = api or FausseApi()
        message = reception.traiter_evenement({"action": "opened", "issue": iss}, api, self.dossier, MAINTENANT, REFUS)
        return api, message

    def catalogue(self):
        with open(os.path.join(self.dossier, "catalogue.json"), encoding="utf-8") as f:
            return json.load(f)

    def mods(self):
        return {m["empreinte"]: m for m in self.catalogue()["mods"]}


class Listes(Base):
    def test_liste_json(self):
        donnees = liste(mod(1, "MC Command Center", "Deaderpool", "https://www.Patreon.com/deaderpool?utm_source=x"), mod(2, "Petit CC", genre="CC"))
        api, message = self.envoyer(issue(5, corps_json(donnees)))
        self.assertEqual(message, "Catalogue : liste #5")
        cat = self.catalogue()
        self.assertEqual(cat["format"], 1)
        self.assertEqual(cat["mis_a_jour"], "2026-11-25T12:00:00Z")
        self.assertEqual(len(cat["mods"]), 2)
        mccc = self.mods()[empreinte(1)]
        self.assertEqual(mccc["vu"], 1)
        self.assertIsNone(mccc["relie_a"])
        self.assertEqual(mccc["source"], "https://patreon.com/deaderpool")
        self.assertEqual(cat["createurs"], [{"nom": "Deaderpool", "pages": ["https://patreon.com/deaderpool"], "mods": 1}])
        self.assertTrue(os.path.exists(os.path.join(self.dossier, "index.html")))
        self.assertEqual(api.de("etiquettes"), [("etiquettes", 5, ("liste",))])
        self.assertIn("2 mods, dont 2 nouveaux", api.de("commentaire")[0][2])
        self.assertEqual(api.de("fermer"), [("fermer", 5, "completed")])

    def test_liste_compressee(self):
        donnees = liste(*[mod(i, f"Mod {i}", "Créatrice") for i in range(1, 40)])
        api, message = self.envoyer(issue(6, corps_compresse(donnees)))
        self.assertIsNotNone(message)
        self.assertEqual(len(self.catalogue()["mods"]), 39)
        self.assertIn("39 mods", api.de("commentaire")[0][2])

    def test_compresse_trop_gros(self):
        c = zlib.compressobj(9, zlib.DEFLATED, -15)
        brut = c.compress(b" " * (3 * 1024 * 1024)) + c.flush()
        with self.assertRaises(reception.FormatInconnu):
            reception.decompresser(base64.b64encode(brut).decode("ascii"))

    def test_doublons_et_vu(self):
        self.envoyer(issue(7, corps_json(liste(mod(1, "Mod A", "Anna", liens=["https://a.exemple/1"]), mod(1, "Mod A", "Anna")))))
        self.assertEqual(self.mods()[empreinte(1)]["vu"], 1)
        api, _ = self.envoyer(issue(8, corps_json(liste(
            mod(1, "Mod A", None, "https://site.exemple/mod-a", liens=["https://a.exemple/1", "https://a.exemple/2"]),
            mod(3, "Mod A", "Anna", "https://site.exemple/mod-a"),
        ))))
        mods = self.mods()
        a = mods[empreinte(1)]
        self.assertEqual(a["vu"], 2)
        self.assertEqual(a["createur"], "Anna")
        self.assertEqual(a["source"], "https://site.exemple/mod-a")
        self.assertEqual(a["liens"], ["https://a.exemple/1", "https://a.exemple/2"])
        # Même source, autre empreinte : entrée distincte rattachée à la même source.
        self.assertEqual(mods[empreinte(3)]["source"], a["source"])
        self.assertEqual(len(mods), 2)
        self.assertIn("dont 1 nouveau pour", api.de("commentaire")[0][2])

    def test_urls_normalisees(self):
        n = reception.normaliser_url
        self.assertEqual(n("HTTPS://WWW.CurseForge.com/sims4/mods/truc/?utm_source=a&id=3&fbclid=z#haut"), "https://curseforge.com/sims4/mods/truc?id=3")
        self.assertEqual(n("http://Exemple.com:80/"), "http://exemple.com")
        self.assertEqual(n("https://exemple.com:8443/a"), "https://exemple.com:8443/a")
        self.assertEqual(n("https://exemple.com/a?b=2&a=1&ref=x"), "https://exemple.com/a?a=1&b=2")
        for mauvaise in ("javascript:alert(1)", "ftp://exemple.com/a", "https://", "https://exe mple.com", "data:text/html,x", None, 3):
            self.assertIsNone(n(mauvaise), mauvaise)

    def test_refus(self):
        donnees = liste(
            mod(1, "Bon mod", "Anna"),
            mod(2, "Super DLC Unlocker", "X"),
            mod(3, "Autre", "Y", "https://telechargement.exemple/warez/sims4"),
            mod(4, "Encore", "Z", liens=["https://interdit.exemple/page"]),
        )
        motifs, _ = REFUS
        refus = (motifs, ["interdit.exemple"])
        api = FausseApi()
        message = reception.traiter_evenement({"issue": issue(9, corps_json(donnees))}, api, self.dossier, MAINTENANT, refus)
        self.assertIsNotNone(message)
        self.assertEqual(list(self.mods()), [empreinte(1)])
        self.assertIn("3 entrées n'ont pas été retenues", api.de("commentaire")[0][2])
        self.assertEqual(reception.lire_refus("# rien\ndomaine: www.Exemple.org\nmotif: abc")[1], ["exemple.org"])

    def test_schema_invalide(self):
        for donnees in ({"format": 2, "mods": []}, {"format": True, "mods": []}, {"format": 1, "mods": {}}, [1, 2]):
            api, message = self.envoyer(issue(10, corps_json(donnees)))
            self.assertIsNone(message)
            self.assertEqual(api.de("etiquettes"), [("etiquettes", 10, ("liste", "format-inconnu"))])
            self.assertEqual(api.de("fermer"), [])
        self.assertFalse(os.path.exists(os.path.join(self.dossier, "catalogue.json")))
        with self.assertRaises(reception.FormatInconnu):
            reception.valider_liste({"format": 1, "mods": [{}] * 5001}, REFUS)
        # Les entrées invalides sont ignorées une à une.
        mauvaises = [{"nom": "Sans empreinte"}, mod(2, "x" * 201), dict(mod(3, "Courte"), empreinte="abc"), mod(4, "Bonne")]
        mods, invalides, refuses = reception.valider_liste(liste(*mauvaises), REFUS)
        self.assertEqual([m["nom"] for m in mods], ["Bonne"])
        self.assertEqual((invalides, refuses), (3, 0))

    def test_relie_a(self):
        donnees = liste(
            mod(1, "WickedWhims", "TURBODRIVER", "https://wickedwhimsmod.com"),
            mod(2, "WickedWhims Traduction FR", "Une traductrice", "https://wickedwhimsmod.com"),
            mod(3, "Better Exceptions", "TwistedMexi"),
            mod(4, "Better Exceptions Add-on Compat", "TwistedMexi"),
            mod(5, "WickedWhims French", "Quelqu'un d'autre"),
            mod(6, "Better Exceptions Plus", "TwistedMexi"),
        )
        self.envoyer(issue(11, corps_json(donnees)))
        mods = self.mods()
        self.assertEqual(mods[empreinte(2)]["relie_a"], empreinte(1))
        self.assertEqual(mods[empreinte(4)]["relie_a"], empreinte(3))
        self.assertIsNone(mods[empreinte(5)]["relie_a"])
        self.assertIsNone(mods[empreinte(6)]["relie_a"])
        self.assertIsNone(mods[empreinte(1)]["relie_a"])

    def test_limite_fonction(self):
        def ancienne(n, heures, etiquettes=("liste",)):
            return issue(n, "", etiquettes=etiquettes, cree=MAINTENANT - timedelta(hours=heures))
        trois = [ancienne(1, 1), ancienne(2, 5), ancienne(3, 23)]
        self.assertTrue(reception.depasse_limite(trois, 20, MAINTENANT))
        self.assertFalse(reception.depasse_limite(trois[:2], 20, MAINTENANT))
        self.assertFalse(reception.depasse_limite(trois[:2] + [ancienne(4, 25)], 20, MAINTENANT))
        self.assertFalse(reception.depasse_limite(trois[:2] + [ancienne(5, 2, ("bug",))], 20, MAINTENANT))
        self.assertFalse(reception.depasse_limite(trois[:2] + [ancienne(20, 0)], 20, MAINTENANT))

    def test_limite_traitement(self):
        anciennes = [issue(n, "", etiquettes=("liste",), cree=MAINTENANT - timedelta(hours=n)) for n in (1, 2, 3)]
        api = FausseApi(auteur=anciennes)
        api, message = self.envoyer(issue(12, corps_json(liste(mod(1, "Mod")))), api)
        self.assertIsNone(message)
        self.assertEqual(api.de("etiquettes"), [("etiquettes", 12, ("liste", "limite"))])
        self.assertIn("24 heures", api.de("commentaire")[0][2])
        self.assertEqual(api.de("fermer"), [("fermer", 12, "not_planned")])
        self.assertFalse(os.path.exists(os.path.join(self.dossier, "catalogue.json")))

    def test_rattrapage(self):
        restee = issue(13, corps_json(liste(mod(7, "Restée ouverte"))), etiquettes=("liste",))
        ignoree = issue(14, corps_json(liste(mod(8, "Déjà refusée"))), etiquettes=("liste", "limite"))
        api, message = self.envoyer(issue(15, corps_json(liste(mod(1, "Mod du jour")))), FausseApi(ouvertes=[restee, ignoree]))
        self.assertEqual(message, "Catalogue : listes #13, #15")
        self.assertEqual(set(self.mods()), {empreinte(1), empreinte(7)})


class AutresIssues(Base):
    BUG = (
        "<!-- modder-bug 1 -->\n### Ce qui s'est passé\n\nLe jeu plante au chargement.\n\n"
        "### Configuration\n\n- Modder Sims4 0.3.0\n\n### Mods suspects\n\n"
        "1. **MC Command Center** (score 16) : cité 4 fois dans lastException\n"
        "2. **Vieux @mod** (score 3) : date de 2019\n\n### Journaux\n\n"
        "<details><summary>lastException.txt (il y a 2 min)</summary>\n\n````text\n"
        "### pas un titre\nTraceback (most recent call last):\n  File \"x.py\", line 3\n"
        "AttributeError: 'NoneType' object has no attribute 'sim_info'\n````\n\n</details>\n"
    )

    def test_bug(self):
        api, message = self.envoyer(issue(20, self.BUG, titre="Bug : plantage"))
        self.assertIsNone(message)
        self.assertEqual(api.de("etiquettes"), [("etiquettes", 20, ("bug",))])
        texte = api.de("commentaire")[0][2]
        self.assertIn("`MC Command Center` (score 16)", texte)
        self.assertIn("`Vieux @mod`", texte)
        self.assertIn("AttributeError: 'NoneType' object has no attribute 'sim_info'", texte)
        self.assertEqual(api.de("fermer"), [])
        suspects, erreur = reception.resumer_bug(self.BUG)
        self.assertEqual(suspects[0], ("MC Command Center", 16, "cité 4 fois dans lastException"))
        self.assertTrue(erreur.startswith("AttributeError:"))

    def test_format_inconnu(self):
        api, message = self.envoyer(issue(21, "Bonjour, comment on installe ?", titre="Question"))
        self.assertIsNone(message)
        self.assertEqual(api.appels, [("etiquettes", 21, ("format-inconnu",))])
        api, _ = self.envoyer(issue(22, "<!-- modder-liste 2 -->\n```json\n{}\n```"))
        self.assertEqual(api.appels, [("etiquettes", 22, ("format-inconnu",))])

    def test_corps_trop_long(self):
        api, message = self.envoyer(issue(23, "<!-- modder-liste 1 -->\n" + "x" * 70000))
        self.assertIsNone(message)
        self.assertEqual(api.appels, [("etiquettes", 23, ("format-inconnu",))])

    def test_bot_ignore(self):
        api, message = self.envoyer(issue(24, corps_json(liste(mod(1, "Mod"))), type_="Bot"))
        self.assertIsNone(message)
        self.assertEqual(api.appels, [])

    def test_type_par_titre(self):
        self.assertEqual(reception.type_issue(issue(1, "rien", titre="Bug : écran noir")), "bug")
        self.assertEqual(reception.type_issue(issue(1, "rien", titre="Autre", etiquettes=("liste",))), "liste")


class Page(unittest.TestCase):
    def test_echappement(self):
        cat = reception.catalogue_vide(MAINTENANT)
        reception.fusionner(cat, [
            {"empreinte": empreinte(1), "nom": "<script>alert(1)</script>", "createur": '"><img src=x onerror=alert(2)>',
             "source": "javascript:alert(3)", "genre": "<b>gras</b>", "liens": []},
        ])
        texte = page.generer(cat)
        self.assertNotIn("<script>alert", texte)
        self.assertNotIn("<img src=x", texte)
        self.assertNotIn("<b>gras", texte)
        self.assertNotIn('href="javascript:', texte)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", texte)
        self.assertIn("&quot;&gt;&lt;img src=x onerror=alert(2)&gt;", texte)
        self.assertIn("https://www.curseforge.com/sims4/search?search=%3Cscript%3E", texte)
        # Le script de la page ne construit jamais de HTML à partir des données.
        for interdit in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write"):
            self.assertNotIn(interdit, texte)
        self.assertIn("fetch(\"catalogue.json\"", texte)
        self.assertNotIn("<script src", texte)

    def test_page_vide(self):
        texte = page.generer(reception.catalogue_vide(MAINTENANT))
        self.assertIn("0 mod, 0 créateur, mis à jour le 25/11/2026", texte)
        self.assertIn("/releases/latest", texte)


if __name__ == "__main__":
    unittest.main()
