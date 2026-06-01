# DECISIONS — Agentic-SEO-Skill (BHUNA)

Append-only. Format : date + contexte + decision + consequences.

---

## 2026-05-31 — Chemins de sortie audit_runner (post-audit lecomptoirdelaplage)

**Contexte.** Pendant l'audit lecomptoirdelaplage.com, `audit_runner.py`
ecrivait `02-BHUNA-actions.json` et `02-BHUNA-actions.md` dans le CWD
(racine du repo BHUNA) au lieu du dossier audit, parce que les flags
`--actions-json`, `--markdown`, `--action-plan`, `--html` etaient relatifs
par defaut et `os.path.abspath()` les resolvait en CWD. Le consolidateur
seo-skills-custom les attendait dans le dossier audit, l'autre session
a du les deplacer a la main.

Deuxieme probleme : `--action-plan` default `02-BHUNA-actions.md` entrait
en COLLISION avec le `.md` canonique sorti par `write_actions_files` (qui
porte le meme nom). Les deux .md s'ecrasaient.

**Decisions.**

1. Auto-derive : si l'un des 4 flags de sortie est laisse a sa valeur
   par defaut (un simple nom de fichier sans repertoire), on l'ecrit
   dans le meme dossier que `--json` (qui lui est correctement passe en
   chemin absolu vers le dossier audit). L'utilisateur peut toujours
   overrider en passant un chemin absolu explicite.

2. Renommer le default `--action-plan` en `02-BHUNA-action-plan.md` pour
   eviter la collision avec le `.md` canonique. Les builders existants
   continuent de fonctionner avec un `--action-plan` explicite.

**Consequences.**
- Le pipeline post-BHUNA (consolidator, payload Notion) trouve les
  fichiers attendus au bon endroit sans intervention manuelle.
- Plus de fichiers orphelins dans le CWD du repo BHUNA apres un audit
  prospect.
- Si un utilisateur avait scripte avec `--action-plan` explicite
  pointant vers `02-BHUNA-actions.md`, son script continue de marcher
  (override possible). Pas de breaking change.

---

## 2026-06-01 — Flesch + tokenisation FR : maillon faible du score business (post-revue MVP)

**Contexte.** Apres retrait de internal_links et link_profile de
BUSINESS_FOCUSED_SCRIPT_WHITELIST (commit ea9dd8f) et fix du score
business pour ne compter que les categories executees (commit 9a5a158),
le score business repose desormais sur 3 categories : onpage,
readability, duplicate_content. Deux d'entre elles (readability,
duplicate_content) ont des biais FR connus :

- readability.py : formule Flesch calibree pour l'anglais (constantes
  206.835, 1.015, 84.6), tokenisation `[a-zA-Z]` qui rate les accents.
  Un site francais bien ecrit ressort typiquement Flesch 40-60 alors
  qu'un site anglais equivalent serait 60-80.
- duplicate_content.py : tokenisation `[a-z]` sans accents, biaise la
  comparaison de templates produits FR.

**Mitigation immediate (en attendant adaptation FR).** Pattern
`content\s+readability\s+is\s+difficult` ajoute aux DROP_FINDING_PATTERNS
de bhuna_actions_builder.py. Le finding ne fuite plus dans la roadmap
prospect. Le score readability reste utilise en interne pour le score
business, mais on ne demande plus au prospect d'agir sur une mesure
biaisee.

**Backlog priorite haute.** Adaptation Flesch FR :
1. Soit utiliser une formule Flesch FR connue (Kandel et Moles 1958,
   constantes ajustees), soit basculer sur LIX/Gunning Fog qui sont
   plus stables cross-langue.
2. Tokenisation : regex `[a-zA-Zaaaceeeeiioouuuy...]+` ou normalisation
   NFD + filtre Unicode letters via `unicodedata.category(c) == 'Ll'`.
3. Recalibrer les seuils business (40, 30) sur un corpus de 10-15
   sites e-commerce FR connus pour ajuster les bornes.
4. Verifier qu'apres adaptation, retirer le pattern DROP du finding
   "readability is difficult" pour le re-exposer au prospect.

**Pourquoi priorite haute.** Tant que ce chantier n'est pas fait, le
score business prospect repose a 1/3 sur une mesure biaisee, et un
site FR proprement redige peut sortir 60-70/100 sans raison technique.
