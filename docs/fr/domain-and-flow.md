# Domaine et flux

Ce document décrit les règles stables du produit et leur traduction dans le
code. Il ne dépend pas du fournisseur ou du modèle d'inférence.

## Flux de données

```text
Instagram
  -> reel / sync_run / sync_observation
  -> media
  -> transcript + screen_text
  -> extraction
       - fiche du reel
       - mentions d'entités avec preuves
       - index de répertoire et recettes
  -> candidate
  -> candidate_name_resolution
  -> catalogue et recherche
  -> agent externe en lecture seule
```

Les étapes sont idempotentes. Une étape ne remplace son résultat que lorsqu'un
résultat complet et compatible est disponible. Un état `running` sans résultat
autoritaire est repris depuis zéro au prochain démarrage.

## Deux destinations métier

Une **entité catalogue** est un lieu, restaurant, hébergement, boutique, marque,
produit ou service que l'on peut retrouver ou acheter. Le reel est une preuve,
pas un avis personnel.

Une **entrée de répertoire** est un contenu à revoir : recette, exercice, leçon,
méthode, guide ou inspiration. Elle appartient au reel et peut contenir plusieurs
recettes. Les recettes sont recherchées par `cuisine` et `cuisine_family`.

Un même reel peut avoir les deux dimensions. La présence d'un nom propre ne
suffit pas à le classer en catalogue : on regarde ce que l'on revient réellement
faire, visiter ou acheter.

## Règles d'inférence

1. Les faits doivent être présents dans la caption, le compte tagué, l'OCR ou la
   transcription. L'écrit prime sur la transcription phonétique.
2. Une mention catalogue doit avoir une preuve courte et localisable. Une adresse
   de contexte, un ingrédient, un mouvement ou un mot de vocabulaire n'est pas une
   entité catalogue.
3. La découverte des noms et leur enrichissement sont séparés : l'enrichissement
   ne peut ni ajouter, ni supprimer, ni corriger un nom découvert.
4. Les valeurs contrôlées (`type`, `scale`, facets, mode, content kind) sont
   bornées par le schéma. Une valeur inconnue est conservée comme tag libre ou
   signalée, jamais inventée silencieusement.
5. Une extraction ne supprime jamais le feedback personnel, l'historique, une
   correction manuelle ou un feedback personnel.

## Canonicalisation et doublons

La canonicalisation est une projection locale, déterministe et réversible.

- `observed_name` reste toujours la forme produite avec ses preuves.
- `canonical_name` n'est rempli que si une forme identique apparaît littéralement
  dans une source écrite prioritaire ou dans un candidat local attesté.
- La similarité de chaînes et la ressemblance phonétique ne corrigent rien.
- En cas de doute, on conserve le candidat et on s'abstient. Deux noms proches
  peuvent coexister ; le coût d'un doublon est inférieur au coût d'une correction
  inventée.
- La recherche peut normaliser casse, accents et espaces via `normalised_key`,
  sans réécrire les données sources.

Le code correspondant se trouve dans `domain/canonicalization.py`,
`pipeline/catalogue/resolve.py` et `domain/reel_library.py`. La table de décision est
`candidate_name_resolution`.

## Médias

Le MP4 original reste local et intact. La validation vidéo enregistre son hash et
ses caractéristiques ; le proxy et le poster sont des dérivés remplaçables.
L’archivage distant reste optionnel et désactivé par défaut.

L’ASR local utilise Whisper `large-v3` sur CUDA par défaut. Le réglage
`REELS_ASR_DEVICE=cpu` active le mode CPU `int8` pour une machine sans GPU.

## Modules principaux

| Responsabilité | Modules |
|---|---|
| Base | `storage/database.py`, `storage/schema.sql` |
| Capture et reprise | `pipeline/capture/`, `pipeline/sync_manifest.py`, `domain/reel_library.py` |
| Caption, ASR, OCR | `pipeline/enrich/` |
| Extraction et matérialisation | `pipeline/extract/` |
| Résolution catalogue | `pipeline/catalogue/`, `domain/canonicalization.py` |
| Répertoire et recherche | `domain/repertoire.py`, `agent/search.py` |
| Feedback personnel | `domain/feedback.py` |
| Médias | `adapters/video_archive.py`, `adapters/archive_store.py` |
| Interfaces | `interfaces/cli.py`, `interfaces/web/` |
