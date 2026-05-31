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
