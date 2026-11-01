# Small transcription evaluation

This is a reproducible sanity check, not a representative accuracy benchmark. On November 1,
2026, two short original prompts were synthesized using the installed macOS Samantha (English,
160 words/minute) and Carmit (Hebrew, 150 words/minute) voices. Audio was converted to 16 kHz,
mono, 16-bit PCM WAV and submitted through the Docker service's public API. No human recordings
or copyrighted source passages are included. Reference texts are in [references](references/).

## Measured results

The [full report](synthetic-small.json) preserves file hashes, reference and recognized text,
timestamps, selected model, word errors, and timing. These are actual inference results.

| Sample | Duration | Word errors / reference words | WER | Pipeline time | Upload-to-result time |
| --- | --- | --- | --- | --- | --- |
| English | 6.68 s | 0 / 19 | 0.0% | 2.16 s | 6.16 s |
| Hebrew | 8.11 s | 2 / 16 | 12.5% | 2.80 s | 6.14 s |
| Combined | — | 2 / 35 | 5.7% | — | — |

The Hebrew substitutions were `הקלטה` → `הכלטה` and `ונשתה` → `ונסתה`. The English sample
matched after normalization. Both were run once; results were not selected from repeated attempts.

Configuration: `Systran/faster-whisper-small`, revision
`536b0662742c02347bc0e980a01041f333bce120`, CPU int8, beam size 5, speech mode, explicit language.
The Docker VM was ARM64, with eight virtual CPUs and approximately 7.75 GiB available memory,
running LinuxKit kernel `7.0.14-linuxkit`; the worker retained its two-CPU/6 GiB container caps.
Model weights were already cached; these were new jobs rather than cached transcript results.
Pipeline time excludes model loading, while
upload-to-result time includes upload, queueing, model loading, and one-second polling intervals.
Repeated identical submissions can reuse results, so their wall times are not inference timings.

## Reproduce or use your own recordings

Start the service using the repository's Docker instructions. Put recordings in `data/evaluation/`
(ignored by Git), with the names `english.wav` and `hebrew.wav`. Record the reference prompts
yourself, or on macOS synthesize equivalent prompts using the installed voices:

```bash
mkdir -p data/evaluation
say -v Samantha -r 160 --file-format=WAVE --data-format=LEI16@16000 \
  -f docs/evaluation/references/english.txt -o data/evaluation/english.wav
say -v Carmit -r 150 --file-format=WAVE --data-format=LEI16@16000 \
  -f docs/evaluation/references/hebrew.txt -o data/evaluation/hebrew.wav
uv run --frozen python scripts/smoke_media.py \
  --reference-dir docs/evaluation/references \
  --report data/evaluation/report.json \
  en=data/evaluation/english.wav he=data/evaluation/hebrew.wav
```

Other operating systems can use any supported recordings and matching UTF-8 reference files.
For a recording named `interview.mp3`, place its expected words in `<reference-dir>/interview.txt`.
References must be nonempty and are checked before uploads. Give samples distinct stems.
The report contains transcript and reference text; only publish recordings/results you intend
to share. Audio files are not committed. Voice versions and encoding can change generated audio
and scores; hashes in the report identify the exact evaluated media.

## Scoring and limitations

Word error rate is the minimum number of word substitutions, deletions, and insertions divided
by reference word count. Lower is better, and insertions can make WER exceed 100%. Combined WER
uses total errors divided by total reference words, not an average of sample percentages.

Scoring applies Unicode NFKC, case folding, and combining-mark removal (including Hebrew niqqud),
then splits on punctuation and whitespace. Digits are kept; numbers are not expanded to words.
This is a simple documented normalization, not a language-specific linguistic scorer.

Two clean synthetic clips cannot establish performance on human speech, accents, background
noise, music, long recordings, automatic language detection, or code-switching. The larger model
was not evaluated here. There is no accuracy acceptance threshold or confidence interval.
Before making quality claims, add representative human recordings with independently checked
references and report each language and recording condition separately.
