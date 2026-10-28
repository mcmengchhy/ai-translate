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
               ▼ (Gemini AI / Free Engine + Khmer Normalizer)
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
* Models like **Google Gemini 2.0 / 1.5 Flash**, Claude, and GPT-4o have absorbed multilingual parallel corpora and understand idiomatic Khmer semantics.
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
│   │   ├── engines/             # Pluggable engines (Gemini, OpenAI, Free)
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
  * **Gemini 2.0 / 1.5 Flash**: Contextual nuances, streaming support, and tone adjustments.
  * **Free Instant Engine**: Works out-of-the-box with **zero API key required**.
  * **OpenAI / Ollama**: Compatible with local LLMs (e.g. `qwen2.5:7b`, `sailor2`).
* **5 Khmer Registers / Tones**: Standard (ទូទៅ), Formal (ផ្លូវការ), Polite (គួរសម), Casual (សាមញ្ញ), Literary (អក្សរសិល្ប៍).
* **Subtitles & Document Translator**: Upload `.srt` or `.vtt` movie subtitle files, `.txt`, or `.json` files. Translates lines while preserving timestamps and structure, then exports back to `.srt` with 1 click.
* **Text-to-Speech (TTS)**: Listen to pronunciation for both source and translated text.
* **Khmer NLP Tools**: Standalone Unicode fixer, numeral transliterator (`០-៩` <-> `0-9`), and ZWSP injection.

---

## 6. Quick Start

### Prerequisites
* macOS / Linux / Windows with Python 3.10+ (Recommended: Python 3.12)
* Modern web browser

### Running the App
Run the included startup script:
```bash
./run.sh
```

Or manually:
```bash
# 1. Activate virtual environment
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
  "engine_used": "gemini",
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

To enable high-nuance contextual translation with **Google Gemini**:
1. Get a free API key from [Google AI Studio](https://aistudio.google.com/apikey).
2. Either:
   * Click the **⚙ Settings** button in the web app UI and paste your key (saved locally in your browser).
   * Or copy `.env.example` to `.env` and set `GEMINI_API_KEY=AIzaSy...`.
