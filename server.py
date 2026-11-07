import functools
import getpass
import hmac
import inspect
import json
import logging
import os
import sys
import threading
import time
from collections import OrderedDict
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

import anyio
import anyio.to_thread
from docx import Document
from mcp.server.fastmcp import FastMCP
from openpyxl import load_workbook
from pptx import Presentation
from pypdf import PdfReader
from starlette.responses import JSONResponse

# Réglages du mode HTTP (ignorés en mode stdio).
# 127.0.0.1 = accessible seulement depuis cette machine.
# 0.0.0.0   = accessible depuis le réseau (toujours avec des jetons !).
HOTE = os.environ.get("MCP_HOST", "127.0.0.1")
PORT = int(os.environ.get("MCP_PORT", "8000"))

mcp = FastMCP("explorateur-fichiers", host=HOTE, port=PORT)

# Le SEUL dossier auquel le serveur a accès. Change-le si besoin.
RACINE = (Path.home() / "Documents").resolve()

# Dossiers ignorés pendant les recherches (lents et inutiles).
DOSSIERS_IGNORES = {".git", "node_modules", "venv", ".venv", "__pycache__"}

# Formats lus comme du texte brut.
EXT_TEXTE = {
    ".txt", ".md", ".csv", ".json", ".log", ".py", ".js", ".ts",
    ".html", ".css", ".xml", ".yaml", ".yml", ".ini", ".cfg",
}
# Formats "documents" gérés par des bibliothèques dédiées.
EXT_DOCUMENTS = {".pdf", ".docx", ".xlsx", ".pptx"}
EXT_RECHERCHE = EXT_TEXTE | EXT_DOCUMENTS

TAILLE_MAX_RECHERCHE = 10 * 1024 * 1024  # fichiers > 10 Mo ignorés en recherche
LIMITE_TEXTE_RECHERCHE = 500_000  # caractères analysés au maximum par fichier
CACHE_MAX_CARACTERES = 50_000_000  # taille max du cache de texte (~50 M caractères)

# Plafonds des paramètres envoyés par le client (protège mémoire et contexte du modèle).
MAX_CARACTERES_LECTURE = 50_000  # lire_fichier, lire_pdf, lire_word, lire_powerpoint
MAX_RESULTATS = 200  # rechercher_fichiers, rechercher_dans_contenu
MAX_LIGNES_EXCEL = 1_000  # lire_excel, par feuille

# Nombre maximum d'outils exécutés en même temps (threads).
LIMITE_THREADS = anyio.CapacityLimiter(8)


def borner(valeur: int, mini: int, maxi: int) -> int:
    """Ramène une valeur entre mini et maxi."""
    return max(mini, min(valeur, maxi))


# ----------------------------------------------------------------------
# Journal d'audit : une ligne JSON par appel d'outil
# ----------------------------------------------------------------------
DOSSIER_LOGS = Path(
    os.environ.get("MCP_LOGS_DIR", Path(__file__).resolve().parent / "logs")
)
DOSSIER_LOGS.mkdir(parents=True, exist_ok=True)

journal = logging.getLogger("mcp-fichiers.audit")
journal.setLevel(logging.INFO)
journal.propagate = False  # jamais vers la sortie standard (réservée à MCP)
if not journal.handlers:
    _handler = RotatingFileHandler(
        DOSSIER_LOGS / "audit.jsonl",
        maxBytes=1_000_000,  # rotation à 1 Mo, 5 anciens fichiers conservés
        backupCount=5,
        encoding="utf-8",
    )
    _handler.setFormatter(logging.Formatter("%(message)s"))
    journal.addHandler(_handler)

UTILISATEUR = getpass.getuser()  # utilisateur du système qui lance le serveur


def ecrire_journal(**champs) -> None:
    """Écrit une entrée dans le journal. Ne doit jamais faire planter un outil."""
    try:
        entree = {
            "horodatage": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "utilisateur": UTILISATEUR,
            **champs,
        }
        journal.info(json.dumps(entree, ensure_ascii=False, default=str))
    except Exception:
        pass


def utilisateur_mcp():
    """Nom associé au jeton HTTP de la requête en cours (mode HTTP).
    Renvoie None en mode stdio ou hors d'une requête."""
    try:
        requete = mcp.get_context().request_context.request
    except Exception:
        return None
    if requete is None:
        return None
    return requete.scope.get("state", {}).get("utilisateur_mcp")


def journaliser(fonction):
    """Décorateur : trace chaque appel d'outil (nom, arguments, statut, durée).
    Le CONTENU des fichiers lus n'est jamais enregistré, seulement sa taille."""
    signature = inspect.signature(fonction)

    @functools.wraps(fonction)  # conserve la signature : MCP en a besoin
    async def enveloppe(*args, **kwargs):
        # Lu ici, dans la boucle asynchrone, où le contexte de la requête existe.
        nom_jeton = utilisateur_mcp()
        try:
            arguments = {
                k: (v[:200] if isinstance(v, str) else v)
                for k, v in signature.bind_partial(*args, **kwargs).arguments.items()
            }
        except TypeError:
            arguments = {}

        debut = time.perf_counter()
        statut, erreur, taille = "ok", None, None
        try:
            # L'outil (lecture disque, parsing PDF…) tourne dans un thread :
            # la boucle asynchrone reste libre pour les autres requêtes.
            resultat = await anyio.to_thread.run_sync(
                functools.partial(fonction, *args, **kwargs),
                limiter=LIMITE_THREADS,
            )
            taille = len(resultat) if isinstance(resultat, str) else None
            return resultat
        except Exception as e:
            refuse = str(e).startswith("Accès refusé")
            statut = "refuse" if refuse else "erreur"
            erreur = str(e)[:300]
            raise
        finally:
            ecrire_journal(
                **({"utilisateur_mcp": nom_jeton} if nom_jeton else {}),
                outil=fonction.__name__,
                arguments=arguments,
                statut=statut,
                duree_ms=round((time.perf_counter() - debut) * 1000),
                taille_resultat=taille,
                erreur=erreur,
            )

    return enveloppe


# ----------------------------------------------------------------------
# Fonctions utilitaires (ce ne sont PAS des outils MCP)
# ----------------------------------------------------------------------
def securiser(chemin: str) -> Path:
    """Empêche de sortir du dossier autorisé (ex: ../../)."""
    p = (RACINE / chemin).resolve()
    if not p.is_relative_to(RACINE):
        raise ValueError("Accès refusé : chemin en dehors du dossier autorisé.")
    return p


def normaliser_extension(ext: str) -> str:
    """'pdf', 'PDF' ou '.pdf' -> '.pdf'. Chaîne vide si pas de filtre."""
    ext = ext.strip().lower()
    if ext and not ext.startswith("."):
        ext = "." + ext
    return ext


def parcourir(dossier: Path):
    """Parcourt un dossier récursivement en ignorant les dossiers inutiles
    et les fichiers qui sortiraient du dossier autorisé (liens symboliques)."""
    for racine, sous_dossiers, fichiers in os.walk(dossier):
        sous_dossiers[:] = [d for d in sous_dossiers if d not in DOSSIERS_IGNORES]
        for nom in fichiers:
            p = Path(racine) / nom
            if p.resolve().is_relative_to(RACINE):
                yield p


def _ligne_excel(row) -> str:
    return " | ".join("" if c is None else str(c) for c in row)


def _morceaux(fichier: Path):
    """Produit le texte d'un fichier morceau par morceau, selon son format."""
    ext = fichier.suffix.lower()

    if ext in EXT_TEXTE:
        yield fichier.read_text(encoding="utf-8", errors="ignore")

    elif ext == ".pdf":
        lecteur = PdfReader(str(fichier))
        if lecteur.is_encrypted:
            return
        for page in lecteur.pages:
            yield page.extract_text() or ""

    elif ext == ".docx":
        doc = Document(str(fichier))
        for paragraphe in doc.paragraphs:
            yield paragraphe.text
        for tableau in doc.tables:
            for ligne in tableau.rows:
                yield " | ".join(cellule.text for cellule in ligne.cells)

    elif ext == ".xlsx":
        classeur = load_workbook(str(fichier), read_only=True, data_only=True)
        try:
            for feuille in classeur.worksheets:
                yield f"[Feuille {feuille.title}]"
                for row in feuille.iter_rows(values_only=True):
                    yield _ligne_excel(row)
        finally:
            classeur.close()

    elif ext == ".pptx":
        prs = Presentation(str(fichier))
        for i, diapo in enumerate(prs.slides, start=1):
            yield f"[Diapositive {i}]"
            for forme in diapo.shapes:
                if forme.has_text_frame:
                    yield forme.text_frame.text
                if getattr(forme, "has_table", False) and forme.has_table:
                    for ligne in forme.table.rows:
                        yield " | ".join(c.text for c in ligne.cells)


def extraire_texte(fichier: Path, limite: int = 0) -> str:
    """Extrait le texte d'un fichier. S'arrête dès que `limite` caractères
    sont atteints (0 = pas de limite)."""
    generateur = _morceaux(fichier)
    parties, total = [], 0
    try:
        for morceau in generateur:
            parties.append(morceau)
            total += len(morceau) + 1
            if limite and total >= limite:
                break
    finally:
        generateur.close()
    texte = "\n".join(parties)
    return texte[:limite] if limite else texte


class CacheTexte:
    """Cache LRU du texte extrait, pour la recherche dans le contenu.
    Une entrée est valide tant que la date de modification et la taille du
    fichier n'ont pas changé. None est mis en cache pour les fichiers
    illisibles, afin de ne pas réessayer à chaque recherche."""

    def __init__(self, max_caracteres: int):
        self.max_caracteres = max_caracteres
        self.total = 0
        self.entrees = OrderedDict()  # chemin -> (mtime_ns, taille, texte|None)
        self.verrou = threading.Lock()

    def obtenir(self, fichier: Path, st) -> tuple:
        """Renvoie (trouvé, texte)."""
        with self.verrou:
            entree = self.entrees.get(fichier)
            if entree and entree[0] == st.st_mtime_ns and entree[1] == st.st_size:
                self.entrees.move_to_end(fichier)
                return True, entree[2]
            return False, None

    def ranger(self, fichier: Path, st, texte) -> None:
        taille = len(texte) if texte else 0
        if taille > self.max_caracteres:
            return
        with self.verrou:
            ancienne = self.entrees.pop(fichier, None)
            if ancienne and ancienne[2]:
                self.total -= len(ancienne[2])
            self.entrees[fichier] = (st.st_mtime_ns, st.st_size, texte)
            self.total += taille
            while self.total > self.max_caracteres:
                _, (_, _, vieux) = self.entrees.popitem(last=False)
                if vieux:
                    self.total -= len(vieux)


cache_texte = CacheTexte(CACHE_MAX_CARACTERES)


def texte_pour_recherche(fichier: Path, st):
    """Texte (limité) d'un fichier, depuis le cache si possible.
    Renvoie None si le fichier est illisible."""
    trouve, texte = cache_texte.obtenir(fichier, st)
    if trouve:
        return texte
    try:
        texte = extraire_texte(fichier, limite=LIMITE_TEXTE_RECHERCHE)
    except Exception:
        texte = None
    cache_texte.ranger(fichier, st, texte)
    return texte


def extrait_autour(texte: str, mot: str, marge: int = 80) -> str:
    """Renvoie un court extrait autour de la première occurrence de `mot`."""
    i = texte.lower().find(mot)
    debut = max(0, i - marge)
    fin = min(len(texte), i + len(mot) + marge)
    return texte[debut:fin].replace("\n", " ").strip()


def verifier_fichier(chemin: str, extension: str):
    """Renvoie (fichier, None) si tout va bien, sinon (None, message d'erreur)."""
    fichier = securiser(chemin)
    if not fichier.is_file():
        return None, "Fichier introuvable."
    if fichier.suffix.lower() != extension:
        return None, f"Ce fichier n'est pas un fichier {extension}."
    return fichier, None


# ----------------------------------------------------------------------
# Outils MCP : navigation et recherche
# ----------------------------------------------------------------------
@mcp.tool()
@journaliser
def lister_dossier(chemin: str = "") -> str:
    """Liste le contenu d'un dossier. Le chemin est relatif au dossier autorisé.
    Laisser vide pour lister la racine."""
    dossier = securiser(chemin)
    if not dossier.is_dir():
        return "Ce chemin n'est pas un dossier."
    lignes = []
    for el in sorted(dossier.iterdir()):
        type_ = "[dossier]" if el.is_dir() else "[fichier]"
        lignes.append(f"{type_} {el.name}")
    return "\n".join(lignes) or "Dossier vide."


@mcp.tool()
@journaliser
def rechercher_fichiers(
    motif: str = "",
    extension: str = "",
    chemin: str = "",
    max_resultats: int = 50,
) -> str:
    """Cherche récursivement des fichiers PAR LEUR NOM (pas par leur contenu).
    motif : texte contenu dans le nom, insensible à la casse (ex: 'facture').
    extension : filtre optionnel sur le type de fichier (ex: 'pdf', '.docx').
    On peut donner seulement une extension, seulement un motif, ou les deux.
    chemin : sous-dossier où chercher (vide = tout le dossier autorisé).
    max_resultats : 200 au maximum."""
    max_resultats = borner(max_resultats, 1, MAX_RESULTATS)
    dossier = securiser(chemin)
    if not dossier.is_dir():
        return "Ce chemin n'est pas un dossier."
    motif = motif.strip().lower()
    ext = normaliser_extension(extension)
    if not motif and not ext:
        return "Précise au moins un motif ou une extension."

    resultats = []
    for p in parcourir(dossier):
        if motif and motif not in p.name.lower():
            continue
        if ext and p.suffix.lower() != ext:
            continue
        resultats.append(str(p.relative_to(RACINE)))
        if len(resultats) >= max_resultats:
            break
    return "\n".join(resultats) or "Aucun fichier trouvé."


@mcp.tool()
@journaliser
def rechercher_dans_contenu(
    mot: str,
    extension: str = "",
    chemin: str = "",
    max_resultats: int = 20,
) -> str:
    """Cherche un mot ou une phrase À L'INTÉRIEUR des fichiers (contenu),
    insensible à la casse. Gère : texte, PDF, Word (.docx), Excel (.xlsx),
    PowerPoint (.pptx). Renvoie le chemin et un extrait pour chaque fichier.
    extension : filtre optionnel (ex: 'pdf'). chemin : sous-dossier où chercher.
    Pour chercher dans les NOMS de fichiers, utiliser rechercher_fichiers.
    max_resultats : 200 au maximum."""
    max_resultats = borner(max_resultats, 1, MAX_RESULTATS)
    dossier = securiser(chemin)
    if not dossier.is_dir():
        return "Ce chemin n'est pas un dossier."
    mot = mot.strip().lower()
    if not mot:
        return "Précise le mot à chercher."
    ext = normaliser_extension(extension)

    resultats = []
    analyses = ignores = 0
    for p in parcourir(dossier):
        suffixe = p.suffix.lower()
        if suffixe not in EXT_RECHERCHE or (ext and suffixe != ext):
            continue
        try:
            st = p.stat()
        except OSError:
            ignores += 1
            continue
        if st.st_size > TAILLE_MAX_RECHERCHE:
            ignores += 1
            continue
        texte = texte_pour_recherche(p, st)
        if texte is None:
            ignores += 1
            continue

        analyses += 1
        if mot in texte.lower():
            resultats.append(
                f"{p.relative_to(RACINE)}\n   ...{extrait_autour(texte, mot)}..."
            )
            if len(resultats) >= max_resultats:
                break

    bilan = f"({analyses} fichier(s) analysé(s), {ignores} ignoré(s) : trop gros, illisible ou protégé)"
    if not resultats:
        return f"Aucun fichier ne contient « {mot} ». {bilan}"
    return "\n".join(resultats) + f"\n{bilan}"


# ----------------------------------------------------------------------
# Outils MCP : lecture de fichiers
# ----------------------------------------------------------------------
@mcp.tool()
@journaliser
def lire_fichier(chemin: str, max_caracteres: int = 5000) -> str:
    """Lit le contenu d'un fichier TEXTE (txt, md, csv, json, py, etc.).
    Ne pas utiliser pour PDF, Word, Excel ou PowerPoint : utiliser lire_pdf,
    lire_word, lire_excel ou lire_powerpoint à la place.
    max_caracteres : 50 000 au maximum."""
    max_caracteres = borner(max_caracteres, 1, MAX_CARACTERES_LECTURE)
    fichier = securiser(chemin)
    if not fichier.is_file():
        return "Fichier introuvable."
    try:
        # On ne lit que le début : un fichier de plusieurs Go n'est jamais
        # chargé en entier en mémoire. Un caractère de plus pour savoir s'il
        # reste du texte après la coupure.
        with fichier.open(encoding="utf-8") as f:
            texte = f.read(max_caracteres + 1)
    except UnicodeDecodeError:
        return "Ce fichier n'est pas un fichier texte lisible."
    if len(texte) > max_caracteres:
        return texte[:max_caracteres] + f"\n[... tronqué à {max_caracteres} caractères]"
    return texte


@mcp.tool()
@journaliser
def lire_pdf(
    chemin: str,
    page_debut: int = 1,
    page_fin: int = 0,
    max_caracteres: int = 5000,
) -> str:
    """Lit le texte d'un fichier PDF. Le chemin est relatif au dossier autorisé.
    page_debut et page_fin (numérotées à partir de 1) permettent de lire une
    partie du document ; page_fin=0 signifie jusqu'à la dernière page.
    Le résultat indique le nombre total de pages.
    max_caracteres : 50 000 au maximum."""
    max_caracteres = borner(max_caracteres, 1, MAX_CARACTERES_LECTURE)
    fichier, erreur = verifier_fichier(chemin, ".pdf")
    if erreur:
        return erreur

    try:
        lecteur = PdfReader(str(fichier))
        if lecteur.is_encrypted:
            return "Ce PDF est protégé par un mot de passe."

        total = len(lecteur.pages)
        debut = max(page_debut, 1)
        fin = total if page_fin <= 0 else min(page_fin, total)
        if debut > total:
            return f"Ce PDF n'a que {total} page(s)."

        morceaux = []
        longueur = 0
        for i in range(debut - 1, fin):
            texte = (lecteur.pages[i].extract_text() or "").strip()
            if texte:
                morceaux.append(f"--- Page {i + 1} ---\n{texte}")
                longueur += len(texte)
            if longueur >= max_caracteres:
                break
    except Exception as e:
        return f"Impossible de lire le PDF : {e}"

    if not morceaux:
        return (
            f"Aucun texte extrait ({total} page(s)). "
            "Le PDF est probablement un scan (images) : il faudrait de l'OCR."
        )

    contenu = "\n".join(morceaux)[:max_caracteres]
    return f"[PDF de {total} page(s), pages {debut} à {fin}]\n{contenu}"


@mcp.tool()
@journaliser
def lire_word(chemin: str, max_caracteres: int = 5000) -> str:
    """Lit le texte d'un document Word (.docx), tableaux inclus.
    Les anciens fichiers .doc ne sont pas supportés.
    max_caracteres : 50 000 au maximum."""
    max_caracteres = borner(max_caracteres, 1, MAX_CARACTERES_LECTURE)
    fichier, erreur = verifier_fichier(chemin, ".docx")
    if erreur:
        return erreur
    try:
        texte = extraire_texte(fichier, limite=max_caracteres)
    except Exception as e:
        return f"Impossible de lire le document Word : {e}"
    return texte or "Document vide (aucun texte trouvé)."


@mcp.tool()
@journaliser
def lire_excel(
    chemin: str,
    feuille: str = "",
    max_lignes: int = 50,
) -> str:
    """Lit un classeur Excel (.xlsx). Sans 'feuille', liste les feuilles et
    lit toutes les feuilles ; avec 'feuille', lit seulement celle-ci.
    max_lignes limite le nombre de lignes lues par feuille.
    Les formules sont remplacées par leur dernière valeur calculée.
    Les anciens fichiers .xls ne sont pas supportés.
    max_lignes : 1 000 au maximum."""
    max_lignes = borner(max_lignes, 1, MAX_LIGNES_EXCEL)
    fichier, erreur = verifier_fichier(chemin, ".xlsx")
    if erreur:
        return erreur
    try:
        classeur = load_workbook(str(fichier), read_only=True, data_only=True)
        try:
            noms = classeur.sheetnames
            if feuille and feuille not in noms:
                return f"Feuille introuvable. Feuilles disponibles : {noms}"
            cibles = [feuille] if feuille else noms

            blocs = [f"[Feuilles du classeur : {noms}]"]
            for nom in cibles:
                lignes = []
                for i, row in enumerate(classeur[nom].iter_rows(values_only=True)):
                    if i >= max_lignes:
                        lignes.append(f"... (limité à {max_lignes} lignes)")
                        break
                    lignes.append(_ligne_excel(row))
                blocs.append(f"--- Feuille « {nom} » ---\n" + "\n".join(lignes))
        finally:
            classeur.close()
    except Exception as e:
        return f"Impossible de lire le classeur Excel : {e}"
    return "\n".join(blocs)


@mcp.tool()
@journaliser
def lire_powerpoint(chemin: str, max_caracteres: int = 5000) -> str:
    """Lit le texte d'une présentation PowerPoint (.pptx), diapositive par
    diapositive. Les anciens fichiers .ppt ne sont pas supportés.
    max_caracteres : 50 000 au maximum."""
    max_caracteres = borner(max_caracteres, 1, MAX_CARACTERES_LECTURE)
    fichier, erreur = verifier_fichier(chemin, ".pptx")
    if erreur:
        return erreur
    try:
        texte = extraire_texte(fichier, limite=max_caracteres)
    except Exception as e:
        return f"Impossible de lire la présentation : {e}"
    return texte or "Présentation vide (aucun texte trouvé)."


# ----------------------------------------------------------------------
# Mode HTTP : authentification par jeton + lancement
# ----------------------------------------------------------------------
class AuthMiddleware:
    """Middleware ASGI : exige 'Authorization: Bearer <jeton>' sur chaque
    requête HTTP. Les tentatives refusées sont écrites dans le journal."""

    def __init__(self, app, jetons: dict):
        self.app = app
        self.jetons = jetons  # {nom: jeton}

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":  # lifespan, etc. : on laisse passer
            await self.app(scope, receive, send)
            return

        entetes = {k.lower(): v for k, v in scope["headers"]}
        valeur = entetes.get(b"authorization", b"").decode("latin-1")
        jeton = valeur[7:].strip() if valeur.lower().startswith("bearer ") else ""

        # Comparaison à temps constant, sur tous les jetons connus.
        nom = None
        for n, j in self.jetons.items():
            if hmac.compare_digest(j.encode(), jeton.encode()):
                nom = n

        if nom is None:
            client = scope.get("client")
            ecrire_journal(
                evenement="auth_refusee",
                ip=client[0] if client else None,
                chemin_http=scope.get("path"),
            )
            reponse = JSONResponse(
                {"erreur": "Non autorisé"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
            await reponse(scope, receive, send)
            return

        # Transmis aux outils (via request.scope) pour le journal d'audit.
        scope.setdefault("state", {})["utilisateur_mcp"] = nom
        await self.app(scope, receive, send)


def charger_jetons() -> dict:
    """Lit MCP_TOKENS au format 'alice:jeton1,bob:jeton2'."""
    jetons = {}
    for element in os.environ.get("MCP_TOKENS", "").split(","):
        if ":" in element:
            nom, jeton = element.split(":", 1)
            if nom.strip() and jeton.strip():
                jetons[nom.strip()] = jeton.strip()
    return jetons


def lancer_http() -> None:
    import uvicorn

    jetons = charger_jetons()
    if not jetons:
        sys.exit(
            "Mode HTTP refusé : aucun jeton défini.\n"
            "Définis MCP_TOKENS, par exemple : export MCP_TOKENS=\"alice:UN_JETON_LONG\""
        )

    app = mcp.streamable_http_app()
    app.add_middleware(AuthMiddleware, jetons=jetons)
    print(
        f"Serveur MCP HTTP sur http://{HOTE}:{PORT}/mcp "
        f"({len(jetons)} jeton(s) autorisé(s))",
        file=sys.stderr,
    )
    uvicorn.run(app, host=HOTE, port=PORT)


if __name__ == "__main__":
    transport = os.environ.get("MCP_TRANSPORT", "stdio").lower()
    ecrire_journal(evenement="demarrage", racine=str(RACINE), transport=transport)
    if transport == "http":
        lancer_http()
    else:
        mcp.run()