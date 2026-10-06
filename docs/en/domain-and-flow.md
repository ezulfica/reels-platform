# Domain and flow

This document describes stable product rules and their implementation. They do
not depend on an inference provider or model.

## Data flow

```text
Instagram → reel / sync history → local media → transcript + screen text
          → extraction → candidates → name resolution
          → catalogue and search → external read-only agent
```

Stages are idempotent. A stage replaces a result only when its new result is
complete and compatible. A `running` stage without an authoritative result
restarts from zero on the next run.

## Business destinations

A **catalogue entity** is a place, restaurant, accommodation, shop, brand,
product or service that can be visited or bought. A reel is evidence, not a
personal review.

A **repertoire entry** is content to revisit: recipe, exercise, lesson, method,
guide or inspiration. It belongs to a reel and can include multiple recipes,
searchable by `cuisine` and `cuisine_family`.

One reel can have both dimensions. A proper name alone does not make a catalogue
entity: classification follows the action the user may actually take.

## Inference rules

1. Facts must appear in the caption, tagged account, OCR or transcript. Written
   evidence outranks phonetic transcription.
2. A catalogue mention needs short, locatable evidence. Context addresses,
   ingredients, movements and vocabulary are not catalogue entities.
3. Name discovery and enrichment are separate. Enrichment cannot add, remove or
   correct a discovered name.
4. Controlled values are bounded by the schema. Unknown values remain free tags
   or are reported; they are never silently invented.
5. Re-extraction never deletes personal feedback, history or manual corrections.

## Canonicalisation and duplicates

Canonicalisation is a local, deterministic and reversible projection. The
observed name and evidence remain intact. A canonical name is set only when the
same form is literally present in priority written evidence or a locally
attested candidate. String similarity and phonetics never correct a name.
Search normalises case, accents and spaces through `normalised_key` without
rewriting source data.

## Media

The original MP4 remains local and intact. Validation records its hash and
characteristics; proxies, posters and first-frame previews are replaceable
derivatives. Remote archiving is optional and disabled by default. Local ASR
uses Whisper `large-v3` on CUDA by default; `REELS_ASR_DEVICE=cpu` enables CPU
mode.
