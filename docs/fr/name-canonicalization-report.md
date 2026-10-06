# Règles de noms et de doublons

La canonicalisation sert à rendre la recherche tolérante sans réécrire les
faits extraits. Elle est locale, explicable et réversible.

## Données conservées

Pour chaque candidat, le système conserve :

- le `observed_name` tel qu'extrait ;
- les preuves (`evidence_json`) et leur source ;
- un `canonical_name` éventuellement vide ;
- une décision `resolved` ou `abstained`, avec méthode et explication.

Le catalogue peut afficher le nom canonique lorsqu'il est résolu, mais le nom
observé et ses alias restent disponibles.

## Décision

Une résolution est autorisée uniquement si la forme proposée apparaît
littéralement dans une caption, un compte tagué, un OCR fiable ou un candidat
local déjà attesté. La casse, les accents et les espaces sont normalisés pour la
recherche par `normalised_key`, jamais dans la preuve source.

La distance de caractères, la traduction et la ressemblance phonétique ne sont
pas des preuves. Dans ces cas, le système s'abstient et garde les deux formes.
Un doublon proche est acceptable ; une correction inventée ne l'est pas.

L'adaptateur d'autorité de lieux est une extension opt-in. Aucune requête
externe n'est nécessaire au fonctionnement courant.

## Implémentation

- Normalisation : `src/domain/canonicalization.py`
- Application au catalogue : `src/pipeline/catalogue/resolve.py`
- Index de recherche : `src/domain/reel_library.py` et `src/agent/search.py`
- Décisions persistées : table `candidate_name_resolution` dans `storage/schema.sql`

Les tests couvrent la priorité de l'écrit sur l'ASR, la résolution par preuve
locale et l'abstention en l'absence de preuve.
