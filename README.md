# mcp-explorateur-fichiers

[![Tests](https://github.com/Sollres/mcp-explorateur-fichiers/actions/workflows/tests.yml/badge.svg)](https://github.com/Sollres/mcp-explorateur-fichiers/actions/workflows/tests.yml)

Serveur MCP en Python qui permet à un modèle d'IA d'explorer, de rechercher et de lire vos fichiers locaux (PDF, Word, Excel, PowerPoint, texte), avec accès limité à un dossier, authentification par jeton et journal d'audit. Fonctionne avec un modèle local via Ollama.

## Fonctionnalités

| Outil | Rôle |
|---|---|
| `lister_dossier` | Liste le contenu d'un dossier |
| `rechercher_fichiers` | Cherche des fichiers par leur nom et/ou leur extension, éventuellement limités aux N derniers jours |
| `fichiers_recents` | Liste les fichiers modifiés récemment, du plus récent au plus ancien, avec date et taille (filtres : période, nom, extension, taille) |
| `rechercher_dans_contenu` | Cherche un mot à l'intérieur des fichiers (texte, PDF, Word, Excel, PowerPoint), avec un extrait autour du mot trouvé |
| `lire_fichier` | Lit un fichier texte (txt, md, csv, json, py…) |
| `lire_pdf` | Lit un PDF, éventuellement sur une plage de pages |
| `lire_word` | Lit un document `.docx`, tableaux inclus |
| `lire_excel` | Lit un classeur `.xlsx`, toutes les feuilles ou une seule |
| `lire_powerpoint` | Lit une présentation `.pptx`, diapositive par diapositive |

## Sécurité

- **Dossier unique** : le serveur n'a accès qu'à `~/Documents`. Les chemins du type `../..` et les liens symboliques qui en sortent sont refusés.
- **Authentification** (mode HTTP) : chaque requête doit porter un jeton `Authorization: Bearer …`, comparé à temps constant.
- **Journal d'audit** : chaque appel d'outil est enregistré dans `logs/audit.jsonl` (outil, arguments, statut, durée, nom associé au jeton). Le contenu des fichiers n'est jamais enregistré.
- **Limites** : les paramètres envoyés par le client sont plafonnés (50 000 caractères lus, 200 résultats, 1 000 lignes Excel), et les fichiers de plus de 10 Mo sont ignorés pendant les recherches.

## Installation

Python 3.11 recommandé.

```bash
pip install -r requirements.txt
```

Pour utiliser le client, il faut aussi [Ollama](https://ollama.com) et le modèle :

```bash
ollama pull qwen2.5:7b
```

## Utilisation

### Interface web (recommandé)

```bash
python interface.py
```

L'interface s'ouvre dans le navigateur sur http://127.0.0.1:8001. On y voit, pour chaque question, les outils appelés par le modèle (un clic affiche leur résultat), puis la réponse. Elle suit le thème clair ou sombre du système et fonctionne aussi sur un écran étroit. Le bouton **Outils** liste les outils disponibles, avec leurs paramètres, la description lue par le modèle et un bouton « Essayer » qui pré-remplit une question d'exemple.

Elle n'écoute que sur `127.0.0.1` et refuse les requêtes qui ne viennent pas de la machine. Pour l'utiliser avec un serveur MCP distant :

```bash
MCP_URL=http://127.0.0.1:8000/mcp MCP_TOKEN=JETON_1 python interface.py
```

### Terminal, mode local (stdio)

Le client lance le serveur lui-même :

```bash
python client.py
```

Exemples de questions : « Trouve mes fichiers docx dans Collins », « Quels fichiers parlent de facture ? », « Quels PDF ai-je reçus cette semaine ? ».
La commande `/lister <chemin>` appelle directement l'outil, sans passer par le modèle. `quit` pour sortir.

### Terminal, mode HTTP (serveur distant)

Générer un jeton par utilisateur :

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Lancer le serveur :

```bash
export MCP_TOKENS="alice:JETON_1,bob:JETON_2"
MCP_TRANSPORT=http python server.py
```

Se connecter depuis un autre terminal :

```bash
export MCP_TOKEN="JETON_1"
python client.py http://127.0.0.1:8000/mcp
```

### Variables d'environnement

| Variable | Défaut | Rôle |
|---|---|---|
| `MCP_TRANSPORT` | `stdio` | `stdio` ou `http` |
| `MCP_HOST` | `127.0.0.1` | Adresse d'écoute en HTTP (`0.0.0.0` pour le réseau) |
| `MCP_PORT` | `8000` | Port d'écoute en HTTP |
| `MCP_TOKENS` | (aucun) | Jetons autorisés côté serveur, format `nom:jeton,nom2:jeton2` |
| `MCP_TOKEN` | (aucun) | Jeton utilisé par le client en HTTP |
| `MCP_LOGS_DIR` | `./logs` | Dossier du journal d'audit |
| `OLLAMA_MODELE` | `qwen2.5:7b` | Modèle Ollama utilisé par le client et l'interface |
| `INTERFACE_PORT` | `8001` | Port de l'interface web |
| `MCP_URL` | (aucun) | Serveur MCP distant pour l'interface (sinon serveur local) |
| `INTERFACE_SANS_NAVIGATEUR` | (aucun) | Si défini, l'interface n'ouvre pas le navigateur au démarrage |

> ⚠️ En mode `0.0.0.0`, le trafic n'est pas chiffré. Placez un reverse proxy HTTPS (Caddy, nginx) ou un tunnel (Tailscale, SSH) devant le serveur.

## Vérifier le journal

```bash
tail -n 20 logs/audit.jsonl
```

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

Les tests travaillent dans des dossiers temporaires : ils ne lisent jamais `~/Documents` et n'écrivent jamais dans `logs/`. Ils couvrent la sécurité des chemins, la lecture de chaque format, la recherche et son cache, les plafonds, le journal d'audit, l'exécution en parallèle, l'authentification HTTP (dont un test de bout en bout avec un vrai serveur), le client (historique, messages d'erreur) et l'interface web (protections, flux d'événements, conversations).

Pour sauter le test de bout en bout : `pytest -m "not lent"`.

Les tests tournent aussi automatiquement sur GitHub Actions à chaque push et pull request (Python 3.11 et 3.12, Linux et macOS).
