# 🇰🇭 Khmer AI Video Dubber & Auto-Translator (អ្នកបកប្រែ និងបញ្ចូលសំឡេងជាភាសាខ្មែរ)

An end-to-end AI system that downloads videos from **Bilibili**, **YouTube**, or local files, transcribes the speech, translates dialogue into natural Khmer, generates realistic **Khmer Neural Voiceovers**, and mixes the audio onto the video with cinema-style ducking and subtitles.

---

## 🎬 Video Dubbing Pipeline Overview

```
[Bilibili / YouTube / Local Video]
               │
               ▼ (yt-dlp / upload)
       [Video & Audio Extract]
               │
               ▼ (Faster-Whisper STT)
     [Timestamped Dialogue Segments]
               │
               ▼ (OpenAI / Ollama / Free Engine + Khmer Normalizer)
     [Translated Khmer Dialogue Cues]
               │
               ▼ (Edge-TTS Neural Voice: Piseth 👨 / Sreymom 👩)
      [Synthesized Khmer Speech Clips]
               │
               ▼ (FFmpeg Audio Tempo Fitting & Cinema Ducking)
[Final Dubbed Video (.MP4) + Khmer Subtitles (.SRT)]
```

---

## 📖 Table of Contents
1. [The State of Khmer Machine Translation](#the-state-of-khmer-machine-translation)
2. [Why Open-Source Projects Struggle with Khmer](#why-open-source-projects-struggle-with-khmer)
3. [How Modern AI Successfully Solves It](#how-modern-ai-successfully-solves-it)
4. [System Architecture](#system-architecture)
5. [Key Features](#key-features)
6. [Quick Start](#quick-start)
7. [API Endpoints Reference](#api-endpoints-reference)
8. [Configuration & Keys](#configuration--keys)
9. [Job Recovery and Processing Speed](#9-job-recovery-and-processing-speed)

---

## 1. The State of Khmer Machine Translation

Khmer is an Austroasiatic language written in an **abugida script** derived from the ancient Brahmi script. Unlike English or romance languages, developing natural language processing (NLP) and machine translation for Khmer faces unique challenges:

* **No Inter-Word Spaces**: Khmer does not use spaces between words. Spaces are used as punctuation (similar to commas or clause separators). Without proper tokenization or subword segmentation, naive translation models misidentify word boundaries.
* **Complex Consonant Subscripts (ជើងអក្សរ - Coeng)**: Khmer characters stack vertically using a special subscript marker (`U+17D2` / `្`). A single syllable can consist of a base consonant, up to two subscript consonants, a dependent vowel, and diacritics.
* **Sociolinguistic Registers (Tones & Politeness)**: Khmer has distinct registers:
  * **Formal / Official (ផ្លូវការ)**: Legal contracts, news, government documents.
  * **Polite / Respectful (គួរសម)**: Used with elders, customers, and guests.
  * **Casual / Spoken (សាមញ្ញ)**: Everyday conversational dialogue.
  * **Royal & Monastic (រាជសព្ទ / សង្ឃសព្ទ)**: Sacred vocabulary reserved for royalty and monks.

---

## 2. Why Open-Source Projects Struggle with Khmer

Many earlier GitHub projects for Khmer translation remain incomplete or perform poorly due to:

1. **MarianMT / OPUS-MT Shortcomings**: Traditional MarianMT models trained on small datasets (like OPUS/Tatoeba) often produce hallucinated words or broken grammar due to vocabulary sparsity.
2. **Unicode Ordering Bugs**: In Khmer Unicode, typing a dependent vowel before a subscript coeng causes visual rendering glitches (dotted circles ◌). Many scrapers fail to normalize text before training.
3. **Broken Web Layouts**: Without Zero-Width Spaces (`\u200b` / ZWSP) injected at syllable or word boundaries, modern browsers cannot break lines naturally, causing text to overflow or cut off abruptly.

---

## 3. How Modern AI Successfully Solves It

Our system combines the two most effective strategies proven in cutting-edge research:

### Strategy A: Multilingual LLMs with Context & Tone Prompting
* OpenAI-compatible models and local Ollama models provide contextual translation for Khmer dialogue.
* We employ **custom system prompts** engineered to instruct the model to maintain Chuon Nath orthography standard, use Khmer punctuation (`។` and `៕`), and match the requested register (Standard, Formal, Polite, Casual, or Literary).

### Strategy B: Meta's NLLB-200 (No Language Left Behind)
* Meta's **NLLB-200** (`facebook/nllb-200-distilled-600M` or `lyfeyvutha/nllb_350M_en_km_v10`) supports `khm_Khm` using SentencePiece subword tokenization, allowing offline translation without external API calls.

### Strategy C: Native Khmer Linguistic Engine
* **Unicode Normalizer**: Automatically swaps improperly ordered `[Vowel + Coeng]` into standard `[Base + Coeng + Vowel]`, and deduplicates consecutive diacritics.
* **Numeral Converter**: Seamlessly transliterates between Khmer digits (`០-៩`) and Western Arabic digits (`0-9`).
* **ZWSP Injector**: Inserts zero-width spaces for responsive line-wrapping on web and mobile screens.

---

## 4. System Architecture

```
ai-translator/
├── backend/
│   ├── app/
│   │   ├── main.py              # FastAPI app & static file server
│   │   ├── api/                 # REST endpoints & Pydantic models
│   │   ├── core/                # Config, environment, system prompts
│   │   ├── engines/             # Pluggable engines (OpenAI / Ollama, Free)
│   │   └── khmer/               # Normalizer, Numeral converter, ZWSP segmenter
├── frontend/
│   ├── index.html               # Modern glassmorphism UI
│   ├── css/styles.css           # Premium dark theme, Kantumruy Pro Khmer typography
│   └── js/                      # App logic, live translator, SRT subtitle parser
├── requirements.txt             # Python 3.12 dependencies
├── run.sh                       # One-click startup script
└── README.md
```

---

## 5. Key Features

* **Instant Translation**: English <-> Khmer and 8+ major languages (Chinese, French, Thai, Vietnamese, Japanese, Korean, German, Spanish).
* **Multi-Engine Support**:
  * **OpenAI-compatible AI**: Contextual nuances, streaming support, and tone adjustments.
  * **Free Instant Engine**: Works out-of-the-box with **zero API key required**.
  * **OpenAI / Ollama**: Compatible with local LLMs (e.g. `qwen2.5:7b`, `sailor2`).
* **5 Khmer Registers / Tones**: Standard (ទូទៅ), Formal (ផ្លូវការ), Polite (គួរសម), Casual (សាមញ្ញ), Literary (អក្សរសិល្ប៍).
* **Subtitles & Document Translator**: Upload `.srt` or `.vtt` movie subtitle files, `.txt`, or `.json` files. Translates lines while preserving timestamps and structure, then exports back to `.srt` with 1 click.
* **Text-to-Speech (TTS)**: Listen to pronunciation for both source and translated text.
* **Automatic Cue Recovery**: A failed translation no longer blocks the other voices. Failed cues retry individually while successful cues continue; generated voices are checkpointed and only missing, failed, changed, or old original-speech fallback cues are regenerated. Voice downloads share a configurable limit of four across all jobs (`TTS_MAX_CONCURRENT_REQUESTS`), start at least 0.2 seconds apart (`TTS_REQUEST_INTERVAL`), and use shared cooldowns after errors. Downloads overlap with two local audio-enhancement tasks using the same EQ and loudness settings. Translation recovery has a three-minute limit and voice requests have timeouts. If Khmer audio remains unavailable, the job reports the affected cues and retains completed translations and voices. New video renders stop before exporting instead of substituting original Chinese speech. Open the saved job and use its **Retry Khmer voices** button to continue without repeating successful clips. It includes missing audio with no error flag, checks the latest saved state, and shows progress instead of starting another worker when the task is already running. **Check Khmer voices** reports when no retry is needed. Old exports containing original speech remain available, but visual-only updates cannot copy them until their voice failures are repaired.
* **Continue Saved Jobs**: **Re-Render** runs in the background from the saved source and dialogue, automatically repairing failed cues without re-uploading or transcribing again. **Translate Missing** remains available for targeted review. Short Latin spellings that match the Chinese source pronunciation are retained with a review note.
* **Stable Long Jobs**: `./run.sh` runs without development auto-reload so editing Python files will not interrupt a render. Use `DEV_RELOAD=1 ./run.sh` only when developing without active generation jobs.
* **Automatic Processing Cache Cleanup**: Before accepting a new video upload, URL, or split request, the server removes unused mixed/fitted WAV intermediates and stale extracted PCM files. Cleanup also runs at startup and after processing. Active jobs and working folders are protected, as are files referenced by saved jobs. Source videos, reusable voice MP3s, subtitles, and finished exports are retained. Unrecognized orphan cache folders have a one-hour grace period; unreadable job metadata skips cleanup safely. Re-rendering recreates the removed intermediates.
* **Video Branding & Subtitle Blur**: Expand **Logo, subtitle blur & Khmer captions** before a new job or in the studio. Enable a 40×40 px logo at the top right, select CineKhmer AI / KhmerDub AI, or upload a PNG, JPEG, or WebP logo. Logos sit 12 px from the edges. Optional blur covers an adjustable rectangular subtitle area throughout the video; it does not detect individual Chinese characters.
* **Corner Text / Bilibili Blur**: In **Logo, corner blur, subtitle blur & Khmer captions**, enable **Blur corner text / Bilibili banner**. The top-left preset starts at 1% left, 2% top, 20% width, and 8% height to cover the example banner; select any of the four corners or adjust the percentages. A source-video preview outlines the selected region (placement only). For multiple uploads the preview uses the first file and the same percentages apply to each video. The corner blur works independently of the subtitle blur and runs before new Khmer captions and your own logo. Saved jobs restore their region and **Re-Render Video** creates a new export; visual-only updates retain the completed audio. The area stays fixed throughout the video, so check other scenes for moving banners. Blurring changes appearance only and does not grant reuse rights or prevent copyright claims. API render settings: `blur_corner`, `corner_blur_x`, `corner_blur_y`, `corner_blur_width`, and `corner_blur_height` (coordinates and dimensions from 0 to 1, fitting inside the frame).
* **Readable Khmer Subtitles**: Balanced lines preserve Khmer combining clusters; long cues use timed pages, and short cues can use available pauses for reading. Burned captions use a bundled Noto Sans Khmer font with proper shaping, white text, and black outlines. Blur is applied before the Khmer captions, so the new captions stay sharp.
* **Original Dialogue → Khmer**: Both engines translate the original dialogue directly into Khmer. English is an optional review reference and never the source for Khmer generation. OpenAI-compatible models receive speech, subtitles, and neighboring scene context together. The Free Engine translates each source cue independently, preferring written subtitles except when they are a matching fragment of longer speech. Substantial speech/subtitle disagreements are flagged for review; uncertain source text is not silently corrected. **Polish Khmer** retranslates saved dialogue directly, including old English-pivot results, without uploading or transcribing again. Changed translations invalidate their old voices; unchanged voices remain reusable. Missing English does not block Khmer generation. Review dialogue before exporting.
  Direct translation does not guarantee accurate idioms or repair misheard Chinese. A comparison on saved job `14140290` still found literal errors from the free endpoint, and its output did not consistently match the Google Translate website. Review a sample before regenerating a full video's voices; use a contextual provider or corrected source dialogue when necessary.
* **Khmer NLP Tools**: Standalone Unicode fixer, numeral transliterator (`០-៩` <-> `0-9`), and ZWSP injection.

---

## 6. Quick Start

### Prerequisites
* macOS / Linux / Windows with Python 3.10+ (Recommended: Python 3.12)
* Modern web browser
* FFmpeg and FFprobe on your PATH (required for video dubbing and splitting)

### Running the App
Run the included startup script:
```bash
./run.sh
```

Or manually:
```bash
# 1. Activate virtual environment
python3 -m venv .venv
source .venv/bin/activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Start server
uvicorn backend.app.main:app --host 0.0.0.0 --port 8000 --reload
```

Open your browser at: **`http://localhost:8000`**

---

## 7. API Endpoints Reference

### Translate Text
```http
POST /api/translate
Content-Type: application/json

{
  "text": "Good morning! Welcome to Cambodia.",
  "source_lang": "en",
  "target_lang": "km",
  "tone": "standard",
  "inject_zwsp": false,
  "convert_numbers": null
}
```

**Response:**
```json
{
  "translated_text": "អរុណសួស្តី! សូមស្វាគមន៍មកកាន់ប្រទេសកម្ពុជា។",
  "source_lang": "en",
  "target_lang": "km",
  "engine_used": "openai",
  "tone": "standard",
  "characters_count": 45
}
```

### Khmer Linguistic Tools
```http
POST /api/tools
Content-Type: application/json

{
  "text": "ឆ្នាំ ២០២៦ តម្លៃ ១៥០ ដុល្លារ",
  "action": "to_arabic_num"
}
```

Actions:
* `normalize`: Reorder subscript coengs and remove duplicate diacritics.
* `to_arabic_num`: Convert Khmer numerals (`០-៩`) to `0-9`.
* `to_khmer_num`: Convert Arabic numbers (`0-9`) to Khmer `០-៩`.
* `add_zwsp`: Inject Zero-Width Spaces (`\u200b`) between syllable boundaries.
* `strip_zwsp`: Remove Zero-Width Spaces.

---

## 8. Configuration & Keys

The application works immediately with the **Free Instant Engine** without any configuration.

For contextual AI translation, open **Settings** and configure **OpenAI / Local Ollama**:
* For OpenAI-compatible providers, enter the API key, optional base URL, and model.
* For Ollama, enter `http://localhost:11434/v1` and an installed model name. A local server does not require an API key.
* **Auto** selects your configured provider and otherwise uses the Free Engine. **Free** always uses the free translation service.

These settings apply to text, subtitle translation, video dubbing, and dialogue polishing. Browser preferences stay in local storage. The Free Engine and Edge-TTS require internet access; Whisper may download its model on first use.

Video effect preferences are remembered in the browser and saved with rendered jobs. Re-render an existing job to apply changed visual options. When dialogue, timing, voice, and audio balance are unchanged, visual-only exports copy the finished audio track and create a new video file. Supported Macs use the hardware H.264 encoder; other systems use software encoding. The bundled Khmer font is distributed under its [SIL Open Font License](frontend/assets/fonts/OFL.txt); `SUBTITLE_FONT_PATH` can point to another Khmer TTF font.

Alternatively, copy `.env.example` to `.env` and configure `OPENAI_API_KEY`, `OPENAI_BASE_URL`, `OPENAI_MODEL`, and `DEFAULT_ENGINE` (`auto`, `openai`, or `free`).

Run the regression checks from the project root:
```bash
.venv/bin/python -m unittest discover -s tests -v
node --test tests/frontend.test.cjs tests/processing.test.cjs
```
Video integration checks require FFmpeg and FFprobe and use temporary synthetic media. Provider requests are mocked, so tests do not consume API credits.

---

## 9. Job Recovery and Processing Speed

**Status: implemented.**

### Manual translation with Google Translate

Choose **Manual — copy text to Google Translate** in the video configuration (the default for new browser submissions). The server prepares the video, extracts speech and subtitles, saves the original dialogue and timing, then stops at **Waiting for your Khmer translation**. Waiting tasks release the video worker, remain available after restart, and do not contact Ollama, an automatic translation service, or voice synthesis. Choose **Automatic — use the selected engine** to use the existing automatic pipeline instead. The server API retains its automatic default for compatibility.

Open **Processing details** on a Video Jobs card. Copy the numbered original dialogue a batch at a time, or download the current batch / full transcript as UTF-8 `.txt`. Open Google Translate, paste the source, select Khmer, and paste its output back into the matching batch; a translated batch can also be loaded from `.txt`. Keep each bracketed cue number, such as `[12]`, before its translated sentence. The importer accepts Khmer digits and wrapped lines, checks that every expected cue appears exactly once, and rejects missing numbers, extra numbers, unchanged Chinese text, or an outdated source transcript before changing saved dialogue. It does not guess alignment by line order. Review each batch and save it. Imported text does not undergo automatic dialogue polishing. Unsaved text is kept in the current browser tab.

Pasting Khmer text or loading a translated `.txt` automatically formats it in the browser: multiple inline markers are separated into one cue per line, extra blank lines and repeated spaces are removed, Khmer cue digits such as `[១១៨០]` become `[1180]`, and cues follow the original dialogue order. **Format & sort** also formats text already in the box. It changes spacing and markers without rewriting Khmer words. Missing cues are reported without inventing or renumbering translations; duplicate and unexpected cue numbers leave the pasted text unchanged. Formatting does not save or send a translation request.

Once all cues have translations, select **Generate Khmer voices & video**. This continues from the saved dialogue without calling an automatic translation provider; incomplete manual jobs cannot silently fall back to one. Changed dialogue invalidates its old voice clips, timing and source text stay intact, and a new output filename preserves previous exports. Existing saved jobs can download their original text too: pause an active task first, then use **Use manual translation** to continue manually. Speech recognition/OCR and video rendering still need local RAM, and voice synthesis needs internet access. The Google Translate website receives text only when you paste it there yourself.

API equivalents: `manual_translation: true` on URL submissions or `manual_translation=true` in upload forms; `GET /api/video/job/{job_id}/transcript.txt` (optional `?batch=0`); `GET /api/video/job/{job_id}/manual-translation?batch=0`; `POST /api/video/job/{job_id}/manual-mode`; and `POST /api/video/job/{job_id}/manual-translation` with `batch`, `source_fingerprint` from the GET response, and numbered Khmer `text`. Resume with `POST /api/video/job/{job_id}/resume` after all manual translations are saved.

### Automatic dialogue and multiple uploads

Video dubbing defaults to **Natural Khmer Dialogue** (conversational tone). AI translation uses a shared Khmer style guide for native word order, context-sensitive forms of address, consistent names, natural sentence endings, and a final meaning check against the source. Formal, polite, and standard tones remain available. The Free Engine uses exact, standalone mappings for selected common expressions; other dialogue still uses its translation service, so it does not receive the AI model's scene-aware rewriting. Use **Polish Khmer** in a saved job to translate its original dialogue again with your selected engine, review it, then re-render the audio and video. Existing finished dialogue and voice checkpoints are retained until you request a retranslation.

In **Upload Videos**, select or drop more than two videos and click **Add Videos to Queue**. There is no two-video selection limit. The browser uploads up to three files at a time; each video becomes an independent job. Failed uploads remain selected for retry while accepted videos continue processing. When attaching subtitles to multiple videos, match each subtitle's filename to its video (for example, `episode01.srt` and `episode01.mp4`). Videos without a sidecar use the existing speech and subtitle detection.

**Video Jobs** uses separate task cards in a responsive three-column desktop grid and shows the running count separately from the queue. It shows every active job and the 30 most recent saved jobs, with separate progress, Open, Resume, and available download links. Refreshing the page restores the list; adding more videos does not cancel existing jobs. Identical uploads reuse saved jobs, and single-video submissions still offer an explicit new run.

Cards distinguish **Missing text** from missing **Khmer voices** and show the saved voice count. A failed task with complete translations offers **Retry N voices**, retaining successful clips. Punctuation-only entries such as `...` are missing dialogue, not valid spoken translations; the manual workspace lists them with their source text and requires actual words before voice generation.

Waiting cards offer **Fix N cues & continue**, which opens the first incomplete text batch and highlights a placeholder without changing drafts. Once all Khmer text is saved, **Continue: voices & video** resumes the same task in manual mode and reuses saved voice clips.

The **Missing cues board** at the top of Processing details collects missing or invalid saved translations from **all text batches**. Select **Copy all missing cues**, translate those numbered original lines, paste the Khmer into the board input, then select **Update missing translations**. Keep the original cue IDs, including gaps such as `[127]`, `[129]`, and `[355]`. The update changes only the missing cues and retains all other saved text, timing, and successful voices. Duplicate, unknown, omitted, Chinese, punctuation-only, and stale corrections are rejected with the pasted draft retained. Existing unsaved batch edits are kept. The board does not start voice generation automatically. It also works while older server workers finish their current videos.

The current API provides `GET /api/video/job/{job_id}/missing-translations` and a matching `POST` accepting numbered `text`, `source_fingerprint`, and `missing_fingerprint` from the GET response. The server validates the entire repair before applying any correction. Active tasks must be paused before updating translations.

Voice recovery retries saved failures one at a time. A fresh batch can use parallel downloads, but its recovery pass runs serially after a 15-second cooldown. Successful clips are reused. A provider outage can still leave a task waiting for another retry; a complete translation alone does not guarantee that the speech service returned audio.

Each card includes **Original audio − / % / +** controls in one-percent steps and a **Sound effects** selector: Off, generated soft ambience, a short generated whoosh at the start and every 30 seconds, or both. Changes save to that video and apply when it reaches final rendering; Khmer narration remains at full volume. During final rendering, controls are locked until it finishes or is paused. Completed videos offer **Re-render audio**, which reuses saved dialogue and voices and creates a new export. Set original audio to **0%** to remove the original soundtrack. Generated effects do not use music recordings; reducing original audio to 2% or adding effects does not remove copyright obligations. API: `PATCH /api/video/job/{job_id}/audio-mix` with `original_volume` (integer 0–100), `sound_effect` (`none`, `ambience`, `transitions`, `both`), and optional `effect_volume` (integer 0–100; default 8).

### Quick clip editor

Open **Quick clip editor** in the top navigation, or visit `/static/clip-editor.html`. Upload a clip directly, choose your edits, and click **Render edited clip**. No dubbing job, transcription, translation, or voice generation is required.

* Flip left/right (the default), top/bottom, both directions, or leave the picture unchanged.
* Change playback speed from **0.25× to 4×**. Audio changes tempo with the picture while preserving pitch. The output keeps the source frame rate, up to 120 fps, and holds the last frame through the output's final fraction of a frame.
* Choose from **16 color presets**: Original, Vivid, Warm, Cool, Black & white, Sepia, Cinematic, Vintage, Soft, High contrast, Muted, Bright, Dark, Golden hour, Teal & orange, and Fade. The local preview shows flip, speed, and approximate color; the rendered MP4 is the final result.
* Add generated **soft ambience**, **whooshes** at the opening and every 30 seconds of output, or both. Separate 0–100% controls set clip audio and effect volume. Setting clip audio to 0% mutes it. Silent clips work too.

The browser remembers editing settings for the next upload. Rendering runs in the background and shares the final-render worker with other videos. Completed MP4 previews and downloads appear under **Your edited clips** and remain available after refreshing. Original uploads and saved dubbing jobs are kept. Unsupported or damaged videos show a render error, and unfinished outputs cannot be downloaded.

API: `POST /api/video/clip-edits` accepts multipart `file`, `flip` (`none`, `horizontal`, `vertical`, `both`; default `horizontal`), `speed` (0.25–4; default 1), `color_filter` (`normal`, `vivid`, `warm`, `cool`, `grayscale`, `sepia`, `cinematic`, `vintage`, `soft`, `high_contrast`, `muted`, `bright`, `dark`, `golden_hour`, `teal_orange`, `fade`), `sound_effect` (`none`, `ambience`, `transitions`, `both`), `original_volume` (0–100; default 100), and `effect_volume` (0–100; default 8). It returns HTTP 202 with an export ID. Poll `/api/video/exports/{export_id}` and download the completed MP4 from `/api/video/exports/{export_id}/files/0/video`. `GET /api/video/exports?kind=clip-edit` lists recent clip edits; a ZIP with the MP4 and settings manifest is available at `/api/video/exports/{export_id}/download`.

These are editing controls; flipping, filtering, and changing speed do not grant rights to reuse copyrighted footage.

### Batch Khmer exports and replacement video

Open **Batch Khmer export & replacement video** above Video Jobs, or **Export / replace video** on a job card. The workspace is also available at `/static/reuse.html`.

* Select up to **100 ready jobs** and choose **Export selected jobs (.zip)**. Each numbered folder contains a stereo 48 kHz PCM `khmer.wav` and UTF-8 `khmer.srt`; `manifest.json` maps folders to job titles and durations. Narration uses saved Khmer voice clips only, excluding the original soundtrack and added background effects. It preserves the saved cue anchors, fits overlapping speech using the existing scheduler, and regenerates subtitles for that exported timeline. It streams audio to disk rather than allocating the entire timeline in RAM.
* For **Render replacement video**, choose one ready job and upload your own or appropriately licensed footage. Choose **Loop** or **Hold its last frame** for short footage; longer footage is trimmed to the narration duration. Optional burned Khmer captions use the same timeline as the SRT. Both the original source soundtrack and the replacement video's soundtrack are excluded.
* Exports run in the background and appear in **Recent exports**, with individual WAV/SRT/MP4 downloads and a ZIP. Export records and finished files are stored under `storage/exports/` and survive page refreshes and server restarts. Interrupted workers are marked interrupted on startup; create the export again to retry. Jobs, dialogue, voices, source videos, and previous exports are kept. Replacement renders are separate outputs and do not replace a job's existing video.
* Tasks must be idle or paused, with complete Khmer translations and current, readable voice clips. Generate or retry missing voices in the editor first. Original-speech fallback clips are rejected. If voice files change while an export is queued, it fails with a request to create a new export, rather than combining mismatched audio and captions. Exports share the final-render worker limit with normal videos.

API: `GET /api/video/export-jobs` lists all saved jobs and readiness. `POST /api/video/exports` accepts `{"job_ids":["job-id"]}`. `POST /api/video/job/{job_id}/replacement-video` accepts multipart `file`, `ending` (`loop` or `freeze`), and `burn_subtitles` (default true). Both return HTTP 202 with an export ID. Poll `GET /api/video/exports/{export_id}`, list recent exports with `GET /api/video/exports`, and download the completed bundle from `GET /api/video/exports/{export_id}/download`. Individual files use `/api/video/exports/{export_id}/files/{index}/{kind}` with zero-based `index` and `kind` equal to `audio`, `subtitles`, or `video`.

Replacing footage does not automatically clear rights to translated dialogue; use material you have permission to translate and publish.

**Pause** stops a running or queued task while retaining completed dialogue and voice clips. Native media operations finish releasing their files before the task becomes **Paused**. **Resume** continues saved work using the selected provider. Paused tasks remain paused after a server restart and do not occupy video workers; pausing one batch clip lets its siblings continue.

Expand **Processing steps** on a task to inspect its five checkpoints: video preparation, transcription, Khmer translation, Khmer voices, and final video. Pause the task before clearing a saved step. Only the latest saved or partial step can be cleared: to redo step 3 after finishing step 4, clear step 4, then step 3, then Resume. If step 5 also exists, clear it first. Earlier steps stay saved, source videos and previous exports are retained, and regenerated exports receive new filenames. Clearing transcription forces a fresh speech/subtitle pass for that job without invalidating another job's cached transcript. API equivalents: `POST /api/video/job/{job_id}/pause`, `POST /api/video/job/{job_id}/resume`, and `DELETE /api/video/job/{job_id}/steps/{step}`, where `step` is `prepare`, `transcription`, `translation`, `voices`, or `render`.

**Remove** stops that task and deletes its saved history without deleting uploaded videos or finished exports. Removed clips are detached from saved batch recovery. Native transcription and rendering workers release their files before removal finishes. Once all active tasks are removed, **Clear tasks & cache** resets task and batch history, transcripts, generated voice caches, previews, PCM files, and render intermediates. Source media, attached subtitles, split clips, finished videos, export archives, logos, and provider settings are retained. API equivalents: `DELETE /api/video/job/{job_id}` and `DELETE /api/video/jobs`.

The server processes one video task at a time by default and queues additional videos. This reduces overlap between memory-heavy transcription, translation, and rendering stages, especially when using a local AI model. New uploads, URL jobs, saved-dialogue resumes, visual updates, and batch clips share this worker limit. Set `VIDEO_MAX_CONCURRENT_JOBS` in `.env` and restart to adjust this limit; increasing it allows videos to overlap and can use more RAM. Transcription/OCR and rendering retain their separate shared limits of one worker each. The limit does not unload cached models or change Ollama's own parallel-request setting. `GET /api/video/jobs` returns compact job summaries and queue counts without the full dialogue or provider credentials. Queued jobs retain their saved progress and follow the existing restart/resume behavior.

Observed case: progress appeared to fall from about **60% to 25%** after stopping and restarting the server and submitting the same video again. Inspection found two separate jobs: the earlier job `2d23ab6a` had saved **71%** progress, **2,175 dialogue cues**, and **230 generated voices**; the new upload job `14140290` was at **26%**, reading on-screen subtitles again. The earlier work remained saved, but the new run repeated the pipeline from the beginning.

Stopping the server interrupts background processing. On the next startup, saved queued or processing jobs are marked **Interrupted** with their original progress and dialogue retained. **Resume saved work** continues the same job; it does not automatically launch tasks at startup.

Implemented improvements:

- Repeated video uploads are detected by SHA-256 content, including renamed files. Repeat URLs are detected by their submitted URL. The UI offers **Open saved job**, **Resume saved work** for interrupted or failed jobs, or an explicit **Start a new run**. Settings differences are disclosed instead of silently ignoring them. Legacy uploads are recognized by hashing same-sized saved sources. Unneeded copies from rejected repeat uploads are removed.
  Repeat submissions return a successful response with the saved job ID, so a page left open during a server update can still open the existing job instead of reporting an upload failure. Refresh the page to load the latest duplicate-choice controls.
- Repeated clicks while a submission is pending send one request, and queue registration is serialized. A running job cannot be resumed twice. Selecting a different job invalidates old polling replies, and the job ID stays visible. Browser refreshes restore the last tracked job.
- Saved dialogue and generated voices are reused on resume. Unchanged editor cues retain the server's latest voice metadata, so an older browser snapshot cannot discard a newly generated clip or restore a cleared voice checkpoint. Pause running jobs before saving dialogue edits. Changed Khmer text, speaker, or voice controls invalidate only the affected cue's audio. Speech recognition is checkpointed before subtitle OCR. Complete transcripts are cached by media content, language, subtitle content, and cache version; an explicit new run can reuse transcription without repeating Whisper or OCR. Changing subtitle content refreshes fusion while retaining the speech checkpoint. An interruption during OCR currently repeats OCR, but preserves completed speech recognition.
- Speech recognition reports processed audio seconds, followed by OCR cue counters. A resumed job retains its progress until the current stage catches up.
- Progress polls request a small summary and download full dialogue once processing finishes. Live counters update immediately; durable progress checkpoints are throttled to once every three seconds, with immediate saves at stage boundaries. Job files are compact and replaced atomically to avoid partial writes.
- The Free Engine reuses HTTP connections across a dialogue job, reducing repeated connection setup without increasing provider concurrency.

These changes reduce repeated processing and overhead; first-time speech recognition, OCR, voice synthesis, and video encoding still depend on the video length, hardware, and provider response times. A repeat upload still transfers the file before the server can verify its content.

### Overnight batches

`POST /api/video/batch` accepts up to eight existing split-clip filenames plus engine, voice, and rendering options. Jobs start together while shared limits allow one speech/OCR worker, one video renderer, six free-translation requests, and four voice requests by default. Saved dialogue is reused, and batch exports use new filenames to retain previous rendered videos. English reference requests are omitted for free-engine batches to reduce processing time; Khmer still comes directly from the original dialogue.

`GET /api/video/batch/{batch_id}` reports progress for every clip. On completion, videos, subtitles, a processing report, and a ZIP are saved automatically under `storage/exports/{batch_id}/`. Download the ZIP with `GET /api/video/batch/{batch_id}/download`. Clips whose entire audio track is digital silence are preserved unchanged and explicitly labelled `source_only`; no Khmer speech is invented. Interrupted batches resume their unfinished jobs when the server starts again. Keep the computer awake and the server running while a batch processes.

The Free Engine spaces requests across jobs and applies shared cooldowns when the service returns HTTP 429/503, honoring `Retry-After`. `scripts/watch_batch.py` can monitor a batch, retry failed parts, verify ZIP integrity, and save videos, subtitles, and the archive to a chosen download folder. Run it under macOS `caffeinate -i` to prevent idle sleep until downloads finish.

Acceptance checks cover concurrent duplicate submissions, renamed identical uploads, different files sharing a filename, explicit new runs, interrupted jobs before and after transcription, saved voice reuse, stale polling responses, cache invalidation, and interrupted metadata writes. Tests use temporary media and mocked providers.
