# Qualité de l'extraction

L'extraction transforme les sources d'un reel en faits structurés. Le fournisseur
et le modèle sont des paramètres d'exécution ; ils ne changent pas les règles du
domaine.

## Trois lectures

1. **Fiche** : sujet, mode (`repertoire` ou `recommandation`), actionnabilité,
   tags et points de relecture.
2. **Découverte** : noms directement recommandés et preuves exactes.
3. **Enrichissement** : type et métadonnées des seuls noms découverts.

Les recettes suivent la fiche puis l'index recette. Le résultat matérialisé reste
une seule extraction par reel, avec les tentatives et leur provenance conservées.

## Ce qui compte comme réussite

Le jeu aveugle `tests/fixtures/qwen_extraction_blind_v1.json` mesure le rappel,
les faux positifs sur les cas exhaustifs, le type de contenu, le mode,
l'actionnabilité, les recettes, les termes attendus et le temps par reel. Les
corrections personnelles ne servent jamais d'annotation implicite.

```bash
uv run reels regression
uv run reels regression --json
```

Une comparaison de modèles doit garder le même holdout, les mêmes sources et le
même prompt. Elle ne doit pas rejouer tout le corpus. Le code de sortie est non
nul lorsqu'une attente du jeu est manquée.

## Reprise et provenance

Une extraction est rejouée si elle est absente, échouée, ou incompatible avec le
modèle, le prompt ou le contexte courant. Les tentatives enregistrent le modèle,
les paramètres, les réponses brutes, la durée et l'erreur éventuelle.

Une sortie de modèle ne devient pas une vérité par elle-même : les faits doivent
rester reliés à une source et les décisions incertaines doivent s'abstenir.
