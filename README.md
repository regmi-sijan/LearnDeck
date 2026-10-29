# LearnDeck

Turn papers, slide decks, notes, spreadsheets and code into an interactive, drill-down presentation you can learn from one piece at a time.

Repository: https://github.com/regmi-sijan/LearnDeck

---

## What is this

LearnDeck is a small app that runs on your own computer and opens in your browser. You give it a document, a codebase or a link. It turns that material into a **presentation you explore by zooming in**:

1. You start on an **overview slide**: what the material is, why it matters, and 3 to 8 numbered parts in a good order to learn them.
2. Click a part and you get a **deeper slide** about just that part, with its own smaller parts to click.
3. Keep going down (up to 4 levels by default), go back up, step sideways to the next part, or jump to the next slide you haven't seen yet.

The idea is to learn a large or difficult piece of material in small fractions, always knowing where you are in the bigger picture.

### What each slide gives you

- A plain-language explanation grounded in the original material
- Key points (each one is clickable and opens a slide about that point)
- A worked example, code walk-through or step-by-step, when it helps
- An analogy, when it helps
- A quick self-check question with a hidden answer
- A box to ask your own question about that slide
- **Show the original**: the exact pages, slides or lines of code the slide was written from

You can also **highlight any words** on a slide and choose **Go deeper on this** to get a slide about exactly that passage.

### What you can give it

| Input | How it's split into levels |
|---|---|
| PDF (papers, books, lecture notes) | Bookmarks or chapters, then pages. Scanned pages are read with AI vision. Page images appear in the side panel. |
| PowerPoint `.pptx` | Sections, then slides, including speaker notes and tables |
| Word `.docx` | Headings |
| Excel `.xlsx`, `.csv`, `.tsv` | Sheets, with their columns and first rows |
| Code files (Python, JavaScript/TypeScript, Java, C/C++, Go, Rust, Swift and about 60 more) | Classes, functions and blocks, with line numbers |
| A zipped project, a dropped folder, or several code files at once | Folders, then files, then functions, plus a repository map |
| Jupyter notebooks `.ipynb` | Markdown sections with their code and outputs |
| Markdown, plain text, HTML, LaTeX | Headings |
| Images (`.png`, `.jpg`, `.gif`, `.webp`) | Read with AI vision |
| Links | Web articles, direct PDF links, arXiv papers, GitHub repositories and single files, public Google Docs/Slides/Sheets |
| Pasted text | Same as a text file |

### How it works

```
your file or link
      │
      ▼
 app/ingest.py   detects the file type, extracts the text and splits it into small
      │           numbered parts (pages, slides, sections, functions…) plus an outline
      ▼
 app/engine.py   overview: the AI reads the whole document (or a shortened version of a
      │           very large one) and proposes regions, each pointing at specific parts
      │           deeper slide: when you click a region, the AI reads just those parts and
      │           writes the explanation plus narrower sub-regions; long parts are cut into
      │           finer passages so each level gets more specific
      ▼
 app/store.py    saves the document, the slide tree and your progress in data/decks/<id>/
      │
      ▼
 app/server.py   local web server (FastAPI) + the interface in web/
```

Slides are written the first time you open them and then saved, so going back and forth afterwards is instant.

```
LearnDeck/
├── start.sh / start.bat   one-step launchers (macOS/Linux, Windows)
├── run.py                 starts the server and opens the browser
├── requirements.txt       Python dependencies
├── .env.example           every setting, copied to .env on first run
├── app/
│   ├── config.py          settings
│   ├── ingest.py          file and link readers
│   ├── llm.py             Claude / OpenAI / Ollama adapters
│   ├── engine.py          overview and drill-down generation, simple mode
│   ├── store.py           saving decks and progress
│   └── server.py          HTTP API
├── web/                   the interface (index.html, styles.css, app.js)
└── data/                  your decks (created on first run)
```

---

## How to run

### Requirements

- **Python 3.9 or newer** (3.11+ recommended). Check with `python3 --version`. If it's older, install a newer one from [python.org](https://www.python.org/downloads/) or with `brew install python@3.12`.
- An internet connection for installing, for the AI provider (unless you use Ollama), and for the interface's fonts and math/code rendering.
- Optional: an AI key (see below) and [LibreOffice](https://www.libreoffice.org) for old `.ppt`/`.doc`/`.xls` files.

### Start it (macOS / Linux)

```bash
git clone https://github.com/regmi-sijan/LearnDeck.git
cd LearnDeck
./start.sh
```

If you already have the folder (for example `~/Desktop/Projects/LearnDeck`), skip the `git clone` line and just `cd` into it. If the terminal says `permission denied`, run `chmod +x start.sh` once.

The first run creates a virtual environment in `.venv`, installs the dependencies (about a minute) and copies `.env.example` to `.env`. Your browser then opens at **http://127.0.0.1:8765**. Press `Ctrl+C` in the terminal to stop. Later runs start in a few seconds.

Useful options: `./start.sh --port 9000` (different port), `./start.sh --no-browser` (don't open a browser tab).

### Start it (Windows)

Double-click `start.bat`.

### Start it by hand

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env               # Windows: copy .env.example .env
python run.py
```

### Turn on the AI

Without an AI provider, LearnDeck runs in **simple mode**: it shows the material's own structure (chapters, pages, slides, files, functions) as clickable levels, but writes no explanations. To get real slides:

1. Click the **gear icon** (Settings) in the app.
2. Pick a provider and paste a key. It's saved in the `.env` file in this folder.
3. If a slide already failed, press **Try again** on it.

| Provider | What you need | Notes |
|---|---|---|
| Claude (default) | An API key from [console.anthropic.com](https://console.anthropic.com) | Uses `claude-sonnet-5-5`. Switch to `claude-opus-5-5` for the most capable model or `claude-haiku-5-5` for the fastest and cheapest. Reads scanned pages and images. |
| OpenAI | An OpenAI API key | Also works with OpenAI-compatible local servers (LM Studio, vLLM) by setting `OPENAI_BASE_URL`. |
| Ollama | [Ollama](https://ollama.com) installed and a model pulled, e.g. `ollama pull llama3.1` | Free and private, runs on your computer. |

### Use it

1. Drop a file or folder onto the home page, choose files, paste a link, or paste text.
2. Pick who it should explain for: a beginner, someone with the basics, or an expert.
3. Explore:

| Action | How |
|---|---|
| Open a part | Click it, or press `1`–`9` |
| Up a level | `Esc` or `Backspace`, or **Up a level** |
| Previous / next part at this level | `←` `→` |
| Next slide you haven't opened | `N`, or **Next unexplored** |
| Lesson map of everything explored | `M` |
| Original pages, slides or code | `S`, or **Show the original** |
| Ask about this slide | `/` jumps to the question box |
| Dig into a specific sentence | Highlight it, then **Go deeper on this** |
| Get a different explanation | **Rewrite this slide** |

Browser Back and Forward also work, and every slide has its own link you can bookmark. Your decks and progress appear on the home page.

### Settings

All settings live in `.env` (see `.env.example` for the full list). The most useful:

| Setting | Default | What it does |
|---|---|---|
| `LLM_PROVIDER` | `auto` | `auto`, `anthropic`, `openai`, `ollama` or `none` |
| `ANTHROPIC_MODEL` | `claude-sonnet-5-5` | Claude model to use |
| `MAX_DEPTH` | `4` | How many levels you can go below the overview |
| `EXPLAIN_LANGUAGE` | `English` | Language the slides are written in |
| `PREFETCH_CHILDREN` | `0` | Pre-write this many child slides in the background (faster clicking, more API use) |
| `VISION_PAGE_LIMIT` | `40` | Scanned pages or images read with AI vision per document |
| `PORT` | `8765` | Local port |

Restart the app after editing `.env` by hand. Changes made in the Settings panel apply immediately.

### Troubleshooting

- **"Simple mode" in the corner**: no AI key is active. Open Settings and add one.
- **"API key was rejected"**: re-paste the key in Settings, then press **Try again** on the slide.
- **"Model was not found"**: the model name in Settings is wrong or not available on your account.
- **A link fails**: download the file and drop it in instead.
- **Port already in use**: `./start.sh --port 9000`.
- **Start over**: delete decks from the home page, or delete the `data/` folder.
- **API docs** (for developers): http://127.0.0.1:8765/api/docs while the app is running.

---

## Current limitations

### AI quality and cost

- **The AI can be wrong.** Slides are grounded in the original, but explanations, examples and answers can still contain mistakes. Use **Show the original** to check anything important.
- **Each new slide is one AI request.** Opening a slide for the first time takes a few seconds or more, depending on the model. With Claude or OpenAI this uses paid API credits; long documents and the larger models cost more. Prompt caching reduces the cost of later slides in the same document, and saved slides are free to revisit.
- **Small local models (Ollama) give simpler slides** and sometimes return badly formatted answers, which shows as an error you can retry. Their smaller memory means less of the document is read at once (about 35,000 characters with the default settings).
- **Simple mode** (no AI) only shows the material's own structure, and its question box can only find matching passages, not answer questions.

### Large documents

- For the overview, documents up to about 160,000 characters (very roughly 80–100 pages of text) are read in full. Larger ones are read in a **shortened form** (the start of every part), so the overview can miss details buried deep in long sections. Deeper slides read their own parts in full, up to 60,000 characters at a time.
- Lessons go **4 levels** below the overview by default (`MAX_DEPTH`).
- Uploads are limited to 300 MB in total (`MAX_UPLOAD_MB`).

### File types

- **Not supported:** Keynote, Pages and Numbers files, EPUB e-books, audio and video (including YouTube links), and password-protected PDFs.
- **Old Office formats** (`.ppt`, `.doc`, `.xls`) need LibreOffice installed; otherwise save them as `.pptx`, `.docx` or `.xlsx` first.
- **Figures, charts and diagrams** inside PDFs, slides and Word files are not analysed; only their text is read. Exceptions: scanned or image-only PDF pages and picture-only slides are read with AI vision (Claude, OpenAI, or a vision-capable Ollama model), up to 40 per document.
- **Tables and equations in PDFs** come through as plain text, so complex layouts and formulas can be garbled.
- **Spreadsheets:** only the first 250 rows of each sheet (or CSV) are read. LearnDeck explains data; it doesn't calculate over the whole file.
- **PowerPoint slide previews** in the side panel only appear if LibreOffice is installed; otherwise you see the slide's text.

### Code

- Python files are split precisely by classes and functions. Other languages are split by pattern matching, so a boundary can occasionally land in an odd place.
- For zipped projects and folders: up to 400 files are read; files over 400 KB, binary files, lock files and folders like `node_modules`, `.git`, `build` and `dist` are skipped.
- LearnDeck reads code; it never runs it.

### Links

- Pages behind a login, paywalls and private files can't be read. Download them and upload the file instead.
- Sites that build their content with JavaScript may give little or no text.
- Private GitHub repositories and private Google files must be downloaded and uploaded. Public GitHub repository links were not tested in the environment where this was built.

### App and storage

- **Single user, on one computer.** It only listens on your own machine (`127.0.0.1`). There are no accounts, no sync between computers and no sharing.
- **No export.** You can't yet save a lesson as a PDF or PowerPoint file.
- The explanation level (beginner, intermediate, expert) is fixed when a deck is created; upload again to change it.
- **Rewrite this slide** also replaces the deeper slides under it.
- Progress means "slides opened"; there's no quiz scoring or spaced repetition.
- API keys are stored as plain text in `.env` (readable only by your user account). Don't share or commit that file; `.gitignore` already excludes it.
- The interface loads fonts and the math/code renderers from the internet. Offline, it still works but falls back to plainer text formatting.

### Testing so far

- Tested on Linux with Python 3.9 and 3.13, in Chromium at desktop and phone sizes, with every supported file type, simple mode, and the Claude and OpenAI code paths (against a test server). Not yet tested directly on macOS or Windows, with Ollama, or with a real API key end to end.
